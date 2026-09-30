"""Compaction: merge a curated partition's small files into one.

Live ingest writes one ``part-<posted>.parquet`` per posting and delivery day, so a 5-minute
product accumulates ~300 files per partition per day. Every point-in-time read opens every
file of the days it covers, and on S3 each file is a request, so small files cost both
latency and money. Compaction rewrites them as one ``merged-<compacted>.parquet``.

Guarantees:

- Only ``curated/`` is touched; raw postings are never read, moved or deleted.
- Rows are never changed or dropped, duplicates inside a posting included. If an earlier run
  was interrupted after writing its merged file, the sources it already holds are recognised
  (every one of their rows is in the newest merged file) and removed instead of merged twice.
- Files younger than ``MIN_AGE`` (by write time) are left alone: ingest may still be writing
  that partition, and a backfill writes old postings now.
- The merged file is written before any source is deleted, so an interruption can leave extra
  files but never lose rows.
- A partition whose files disagree on schema is skipped and logged, never coerced.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

import pyarrow as pa

from ercot_lake.contract import BUSINESS_KEY, CURATED_PREFIX, merged_key
from ercot_lake.timeutil import now_utc
from ingest.config import Product, Settings
from ingest.lake import Lake

log = logging.getLogger(__name__)

MIN_AGE = timedelta(minutes=60)
_PARTITION = re.compile(
    rf"^{CURATED_PREFIX}/(?P<product>[^/]+)/date=(?P<day>\d{{4}}-\d{{2}}-\d{{2}})/"
)
_FILE = re.compile(r"/(part|merged)-\d{8}T\d{6}Z\.parquet$")


@dataclass
class CompactionSummary:
    product: str
    partitions_merged: int = 0
    files_in: int = 0
    rows: int = 0
    leftovers_removed: int = 0
    skipped: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "product": self.product,
            "partitions_merged": self.partitions_merged,
            "files_in": self.files_in,
            "rows": self.rows,
            "leftovers_removed": self.leftovers_removed,
            "skipped": self.skipped,
        }


def compact_product(
    product: Product, lake: Lake, *, now: datetime, min_age: timedelta = MIN_AGE
) -> CompactionSummary:
    """Merge every partition of one product that has more than one old enough file."""
    summary = CompactionSummary(product.key)
    partitions: dict[str, list[str]] = {}
    for key, modified in lake.list_files(f"{CURATED_PREFIX}/{product.key}/"):
        m = _PARTITION.match(key)
        if m is None or not _FILE.search(key) or now - modified < min_age:
            continue
        partitions.setdefault(m["day"], []).append(key)
    for day, keys in sorted(partitions.items()):
        if len(keys) < 2:
            continue  # one file is already compact; rewriting it would only change its name
        _merge(product, lake, day, keys, now=now, summary=summary)
    return summary


def _merge(  # noqa: PLR0913  (keyword-only after the partition)
    product: Product,
    lake: Lake,
    day: str,
    keys: list[str],
    *,
    now: datetime,
    summary: CompactionSummary,
) -> None:
    tables = {k: lake.read_table(k) for k in keys}
    schema = next(iter(tables.values())).schema
    if any(not t.schema.equals(schema) for t in tables.values()):
        log.warning("%s %s: files disagree on schema; left as they are", product.key, day)
        summary.skipped.append(day)
        return
    leftovers = _already_merged(tables)
    to_merge = [t for k, t in tables.items() if k not in leftovers]
    merged = pa.concat_tables(to_merge)
    # Sorted by business key then posting: neighbouring rows compress well, and a reader's
    # dedupe scans them in order.
    columns = (*BUSINESS_KEY[product.table], "posted_at", "ingested_at")
    order: list[tuple[str, Literal["ascending", "descending"]]] = [
        (c, "ascending") for c in columns
    ]
    merged = merged.sort_by(order)
    target = merged_key(product.key, datetime.fromisoformat(day).date(), now)
    lake.write_table(target, merged)
    for key in keys:
        if key != target:  # a same-second rerun overwrote its own merged file
            lake.delete(key)
    summary.partitions_merged += 1
    summary.files_in += len(keys)
    summary.rows += merged.num_rows
    summary.leftovers_removed += len(leftovers)
    log.info("%s %s: %d files, %d rows -> %s", product.key, day, len(keys), merged.num_rows, target)


def _already_merged(tables: dict[str, pa.Table]) -> set[str]:
    """Sources an interrupted run already merged: every row of theirs (with multiplicity) is in
    the newest merged file, which a completed merge would have deleted them for."""
    merged = sorted(k for k in tables if "/merged-" in k)
    if not merged:
        return set()
    newest = merged[-1]
    held = Counter(_rows(tables[newest]))
    out = set()
    for key, table in tables.items():
        if key == newest:
            continue
        rows = Counter(_rows(table))
        if all(held[r] >= n for r, n in rows.items()):
            out.add(key)
    return out


def _rows(table: pa.Table) -> list[tuple[Any, ...]]:
    return [tuple(r.values()) for r in table.to_pylist()]


def compact(
    settings: Settings, lake: Lake, *, now: datetime | None = None
) -> list[CompactionSummary]:
    """Compact every configured product."""
    when = now or now_utc()
    return [compact_product(p, lake, now=when) for p in settings.products.values()]
