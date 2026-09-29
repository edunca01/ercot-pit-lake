"""Shared by the sample fetchers: the trimming rule, the ERCOT client, and where files go.

A sample must show what the lake collects, not whatever happens to come first. ERCOT lists
resource nodes before hubs and zones, so "the first N rows" of a price report holds no
HB_NORTH at all. The rule, applied to a posting (archive) or a fetched window (API):

- keep the first ``INTERVALS`` intervals, in source order (an interval is the product's
  declared time columns plus its repeated-hour flag);
- price tables: every ``HB_``/``LZ_`` row of those intervals (all types, so LZ and LZEW),
  plus the first ``RESOURCE_NODES`` other settlement points of each interval;
- every other table: every row of those intervals (all AS types, all series, every
  forecast model, so the in-use filter has rows to drop).
"""

from __future__ import annotations

import csv
import io
import json
import logging
from collections.abc import Sequence
from typing import Any

from ingest.config import REPO_ROOT, TIME_COLUMNS, Product, load_credentials, settings
from ingest.ercot_api import ErcotClient
from ingest.transforms import Source, TransformSpec

SAMPLES_DIR = REPO_ROOT / "samples"
MANIFEST = SAMPLES_DIR / "manifest.json"  # where each sample came from, for the tests
DISCOVER_DIR = REPO_ROOT / "data" / "discover"  # gitignored: raw probes for new products

INTERVALS = 2
RESOURCE_NODES = 5
_HUBS_AND_ZONES = ("HB_", "LZ_")

RULE = (
    f"first {INTERVALS} intervals; price tables: all HB_/LZ_ rows + "
    f"{RESOURCE_NODES} resource nodes per interval; others: all rows of those intervals"
)


def client() -> ErcotClient:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    return ErcotClient(settings().ercot, load_credentials())


def _positions(product: Product, source: Source, header: Sequence[str]) -> dict[str, int]:
    """Canonical column -> index in this source's rows."""
    columns = TransformSpec.for_product(product).columns_for(source)
    return {columns[h]: i for i, h in enumerate(header) if h in columns}


def interval_keys(
    product: Product, source: Source, header: Sequence[str], rows: Sequence[Sequence[Any]]
) -> list[tuple[str, ...]]:
    """The interval each row belongs to, as declared for this product."""
    pos = _positions(product, source, header)
    cols = [*TIME_COLUMNS[product.transform.time], "dst_flag"]
    idx = [pos[c] for c in cols if c in pos]
    return [tuple(str(r[i]).strip() for i in idx) for r in rows]


def select(
    product: Product, source: Source, header: Sequence[str], rows: Sequence[Sequence[Any]]
) -> list[int]:
    """Indices of the rows the rule keeps, in their original order."""
    keys = interval_keys(product, source, header, rows)
    wanted = list(dict.fromkeys(keys))[:INTERVALS]
    point = _positions(product, source, header).get("settlement_point")
    nodes_seen: dict[tuple[str, ...], int] = {}
    keep = []
    for i, (row, key) in enumerate(zip(rows, keys, strict=True)):
        if key not in wanted:
            continue
        if product.table == "spp" and point is not None:
            name = str(row[point]).strip()
            if not name.startswith(_HUBS_AND_ZONES):
                if nodes_seen.get(key, 0) >= RESOURCE_NODES:
                    continue
                nodes_seen[key] = nodes_seen.get(key, 0) + 1
        keep.append(i)
    return keep


def complete_intervals(
    product: Product, source: Source, header: Sequence[str], rows: Sequence[Sequence[Any]]
) -> bool:
    """True once the rows reach past the intervals the rule keeps, so those are whole."""
    return len(set(interval_keys(product, source, header, rows))) > INTERVALS


def trim_csv(product: Product, text: str) -> str:
    """Header plus the kept data lines, byte for byte as ERCOT wrote them."""
    lines = text.splitlines(keepends=True)
    parsed = list(csv.reader(io.StringIO(text)))
    header, rows = [h.strip() for h in parsed[0]], parsed[1:]
    data_lines = [ln for ln in lines[1:] if ln.strip()]
    rows = [r for r in rows if any(c.strip() for c in r)]
    if len(rows) != len(data_lines):
        msg = f"{product.key}: CSV has multi-line cells; cannot trim line by line"
        raise ValueError(msg)
    return lines[0] + "".join(data_lines[i] for i in select(product, "archive", header, rows))


def record(product: str, source: Source, entry: dict[str, Any]) -> None:
    """Note where a sample came from (posting time, document, query) in samples/manifest.json."""
    manifest: dict[str, dict[str, Any]] = (
        json.loads(MANIFEST.read_text()) if MANIFEST.exists() else {}
    )
    manifest.setdefault(product, {})[source] = entry
    MANIFEST.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
