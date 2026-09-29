"""The ingest loop, shared by live ingest and backfill.

    read watermark -> list documents newer than it -> for each: raw, transform, curated,
    advance watermark -> write manifest -> return metrics

A document is one ERCOT posting. The loop does not know where documents come from: ``docs`` is
any iterable of :class:`Doc`, so backfill from monthly bundles or the yearly workbook runs the
same code as the scheduled run and writes identical partitions.
"""

from __future__ import annotations

import io
import logging
import time
import zipfile
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc

from ercot_lake.contract import SCHEMA_VERSION, curated_key, manifest_key, raw_key
from ercot_lake.timeutil import CT, delivery_date_ct, now_utc, utc_to_ct
from ingest.archive import iter_csv_members, posted_local_from_name
from ingest.config import Product
from ingest.ercot_api import DocumentNotReadyError, ErcotClient, resolve_repeated_hour
from ingest.lake import Lake
from ingest.state import StateStore, Watermark
from ingest.transforms import TransformSpec, transform

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Doc:
    """A posting to ingest. ``load`` fetches the zip lazily so listing stays cheap."""

    posted_at: datetime
    name: str
    load: Callable[[], bytes]


@dataclass(frozen=True)
class Window:
    post_from: datetime
    post_to: datetime


@dataclass
class RunSummary:
    product: str
    started_at: datetime
    window: Window
    docs: int = 0
    rows_written: int = 0
    partitions: set[str] = field(default_factory=set)
    last_posted_at: datetime | None = None
    latest_interval_start: datetime | None = None
    duration_s: float = 0.0
    status: str = "ok"
    error: str | None = None
    deferred: str | None = None  # first doc listed but not yet downloadable; next run resumes

    def as_dict(self) -> dict[str, Any]:
        return {
            "product": self.product,
            "started_at": self.started_at.isoformat(),
            "window": {
                "from": self.window.post_from.isoformat(),
                "to": self.window.post_to.isoformat(),
            },
            "docs": self.docs,
            "rows_written": self.rows_written,
            "partitions": sorted(self.partitions),
            "last_posted_at": self.last_posted_at.isoformat() if self.last_posted_at else None,
            "latest_interval_start": (
                self.latest_interval_start.isoformat() if self.latest_interval_start else None
            ),
            "duration_s": round(self.duration_s, 3),
            "status": self.status,
            "error": self.error,
            "deferred": self.deferred,
        }


def resolve_window(
    product: Product, wm: Watermark, *, now: datetime, explicit: Window | None
) -> Window:
    if explicit is not None:
        return explicit
    start = wm.last_posted_at or (now - timedelta(hours=product.initial_lookback_hours))
    return Window(start, now)


# -- document sources ----------------------------------------------------------------------


def archive_docs(client: ErcotClient, product: Product, window: Window) -> Iterator[Doc]:
    """Live source: the product's archive listing over the window."""
    for d in client.iter_archive_docs(product.archive_id, window.post_from, window.post_to):
        yield Doc(
            posted_at=d.posted_at,
            name=d.friendly_name or str(d.doc_id),
            load=lambda d=d: client.download_archive(d.archive_id, d.doc_id),  # type: ignore[misc]
        )


def month_span(window: Window) -> list[str]:
    """``YYYY-MM`` for every *Central-time* calendar month the posting window touches, oldest
    first. ERCOT cuts its monthly bundles at CT midnight, not UTC."""
    months: list[str] = []
    a, b = utc_to_ct(window.post_from), utc_to_ct(window.post_to)
    y, m = a.year, a.month
    while (y, m) <= (b.year, b.month):
        months.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return months


def month_edges_utc(month: str) -> tuple[datetime, datetime]:
    """[start, end) of a CT calendar month, as UTC."""
    y, m = (int(x) for x in month.split("-"))
    start = datetime(y, m, 1, tzinfo=CT)
    end = datetime(y + (m == 12), 1 if m == 12 else m + 1, 1, tzinfo=CT)
    return start.astimezone(UTC), end.astimezone(UTC)


def bundle_members(blob: bytes) -> list[tuple[datetime, str, Callable[[], bytes]]]:
    """A monthly bundle is a zip of per-posting zips named ``<docId>.cdr....<stamp>....zip``.

    Members are read on demand (a 5-minute product's month is ~9k members, ~1 GB decompressed),
    so only the compressed bundle stays in memory. The stamps are CT without a DST flag, so the
    repeated fall-back hour is told apart in doc-ID order, exactly as for the archive listing.
    """
    zf = zipfile.ZipFile(io.BytesIO(blob))
    listed: list[tuple[int, datetime, str]] = []
    infos = {}
    for pos, info in enumerate(zf.infolist()):
        if info.is_dir() or not info.filename.lower().endswith(".zip"):
            continue
        local = posted_local_from_name(info.filename)
        if local is None:
            log.warning("bundle member without a posting stamp skipped: %s", info.filename)
            continue
        head = info.filename.split(".", 1)[0]
        order = int(head) if head.isdigit() else pos  # doc IDs grow with posting order
        listed.append((order, local, info.filename))
        infos[info.filename] = info
    out = [
        (posted, name, lambda info=infos[name]: zf.read(info))
        for _, posted, name in resolve_repeated_hour(listed)
    ]
    out.sort(key=lambda t: t[0])
    return out  # type: ignore[return-value]


def bundle_docs(client: ErcotClient, product: Product, window: Window) -> Iterator[Doc]:
    """Backfill source: ERCOT's monthly bundles, one posting = one Doc, exactly as live ingest
    sees them. Months without a bundle (the current month, and occasional gaps) fall back to
    the archive listing for that month's slice of the window."""
    listing = client.list_bundles(product.key)
    by_month = {
        b["friendlyName"].rsplit("_", 1)[-1]: b
        for b in listing.get("bundles", [])
        if "_" in b.get("friendlyName", "")
    }
    for month in month_span(window):
        m_start, m_end = month_edges_utc(month)
        lo, hi = max(window.post_from, m_start), min(window.post_to, m_end)
        if lo >= hi:
            continue
        bundle = by_month.get(month)
        if bundle is None:
            log.info("%s: no bundle for %s, using the archive listing", product.key, month)
            yield from archive_docs(client, product, Window(lo, hi))
            continue
        log.info("%s: downloading bundle %s", product.key, bundle["friendlyName"])
        members = bundle_members(client.download_bundle(product.key, bundle["docId"]))
        log.info("%s: bundle %s holds %d postings", product.key, month, len(members))
        for posted, name, load in members:
            if lo < posted <= hi:
                yield Doc(posted_at=posted, name=name, load=load)


def hist_docs(client: ErcotClient, product: Product, window: Window) -> Iterator[Doc]:
    """History source: the product's fallback archive, a periodically re-posted cumulative
    workbook. Each posting is one Doc; pair with ``Run.delivery_range`` to take only the
    delivery days that no regular posting covers."""
    if product.fallback_archive_id is None:
        msg = f"{product.key} has no fallback_archive_id"
        raise ValueError(msg)
    archive_id = product.fallback_archive_id
    for d in client.iter_archive_docs(archive_id, window.post_from, window.post_to):
        yield Doc(
            posted_at=d.posted_at,
            name=d.friendly_name or str(d.doc_id),
            load=lambda d=d: client.download_archive(d.archive_id, d.doc_id),  # type: ignore[misc]
        )


# -- one posting ---------------------------------------------------------------------------


def _filter_delivery_range(table: pa.Table, rng: tuple[date, date]) -> pa.Table:
    starts: list[Any] = table.column("interval_start").to_pylist()
    keep = pa.array([rng[0] <= delivery_date_ct(ts) <= rng[1] for ts in starts], type=pa.bool_())
    return table.filter(keep)


def _split_by_delivery_date(table: pa.Table) -> dict[str, pa.Table]:
    starts: list[Any] = table.column("interval_start").to_pylist()
    labels = [str(delivery_date_ct(ts)) for ts in starts]
    dates = pa.array(labels, type=pa.string())
    return {d: table.filter(pc.equal(dates, pa.scalar(d))) for d in sorted(set(labels))}


@dataclass(frozen=True)
class DocResult:
    rows: int
    partitions: set[str]
    latest_interval_start: datetime | None


def ingest_doc(
    product: Product,
    lake: Lake,
    doc: Doc,
    *,
    ingested_at: datetime,
    delivery_range: tuple[date, date] | None = None,
) -> DocResult:
    """Raw -> curated for one posting. ``delivery_range`` keeps only those delivery days
    (used with cumulative history workbooks so earlier days keep their original postings)."""
    blob = doc.load()
    lake.write_bytes(raw_key(product.key, doc.posted_at), blob)  # raw lands before parsing

    spec = TransformSpec.for_product(product)
    rows, partitions, latest = 0, set(), None
    for member in iter_csv_members(blob):
        # the filename stamp is CT without a DST flag; the listed time decides the fold
        posted_at = member.posted_at(doc.posted_at)
        table = transform(
            spec, "archive", member.text, posted_at=posted_at, ingested_at=ingested_at
        )
        if delivery_range is not None:
            table = _filter_delivery_range(table, delivery_range)
        if table.num_rows == 0:
            if delivery_range is None:
                log.warning("%s: empty CSV member %s", product.key, member.name)
            continue
        for ddate, part in _split_by_delivery_date(table).items():
            lake.write_table(curated_key(product.key, date.fromisoformat(ddate), posted_at), part)
            partitions.add(ddate)
            rows += part.num_rows
        m: datetime = pc.max(table.column("interval_start")).as_py()
        latest = m if latest is None else max(latest, m)
    return DocResult(rows, partitions, latest)


# -- the loop ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Run:
    """Everything one ingest run needs; ``docs`` is any document source."""

    product: Product
    lake: Lake
    state: StateStore
    docs: Iterable[Doc]
    window: Window
    update_watermark: bool = True
    delivery_range: tuple[date, date] | None = None


def run_product(run: Run) -> RunSummary:
    """Ingest every doc in order. The watermark advances after each successful doc, so a
    failure leaves it at the last good one and the next run resumes there."""
    product, lake, state, window = run.product, run.lake, run.state, run.window
    t0 = time.monotonic()
    summary = RunSummary(product=product.key, started_at=now_utc(), window=window)
    wm = state.get(product.key)
    log.info(
        "%s: window %s -> %s (watermark %s)",
        product.key,
        window.post_from.isoformat(),
        window.post_to.isoformat(),
        wm.last_posted_at,
    )
    try:
        for doc in run.docs:
            try:
                res = ingest_doc(
                    product,
                    lake,
                    doc,
                    ingested_at=now_utc(),
                    delivery_range=run.delivery_range,
                )
            except DocumentNotReadyError as exc:
                # Docs are oldest-first; a later one cannot be taken before this one without
                # leaving a hole under the watermark. Stop here, watermark untouched.
                summary.deferred = doc.name
                log.warning("%s: %s; deferring to the next run", product.key, exc)
                break
            summary.docs += 1
            summary.rows_written += res.rows
            summary.partitions |= res.partitions
            summary.last_posted_at = doc.posted_at
            latest = res.latest_interval_start
            if latest and (
                summary.latest_interval_start is None or latest > summary.latest_interval_start
            ):
                summary.latest_interval_start = latest
            if run.update_watermark:
                wm = _ok(wm, last_posted_at=doc.posted_at)
                state.put(wm)
            log.info(
                "%s: %s posted=%s rows=%d partitions=%s",
                product.key,
                doc.name,
                doc.posted_at.isoformat(),
                res.rows,
                sorted(res.partitions),
            )
    except Exception as exc:
        summary.status, summary.error = "error", f"{type(exc).__name__}: {exc}"
        summary.duration_s = time.monotonic() - t0
        state.put(
            wm.model_copy(
                update={
                    "last_run_at": now_utc(),
                    "last_status": "error",
                    "last_error": summary.error,
                }
            )
        )
        log.error("%s: failed after %d docs: %s", product.key, summary.docs, summary.error)
        raise
    summary.duration_s = time.monotonic() - t0
    if run.update_watermark:
        state.put(_ok(wm))
        # The manifest is "the last successful scheduled run", which freshness reads. A
        # backfill over old postings must not overwrite it with an old last_posted_at.
        _write_manifest(lake, product, summary)
    log.info(
        "%s: done docs=%d rows=%d duration=%.1fs",
        product.key,
        summary.docs,
        summary.rows_written,
        summary.duration_s,
    )
    return summary


def _ok(wm: Watermark, **update: Any) -> Watermark:
    return wm.model_copy(
        update={"last_run_at": now_utc(), "last_status": "ok", "last_error": None, **update}
    )


def _write_manifest(lake: Lake, product: Product, summary: RunSummary) -> None:
    """manifests/<product>/latest.json: last successful run; data fields survive empty runs."""
    key = manifest_key(product.key)
    manifest: dict[str, Any] = {**summary.as_dict(), "schema_version": SCHEMA_VERSION}
    if summary.docs == 0 and lake.exists(key):
        prev = lake.read_json(key)
        for k in ("last_posted_at", "latest_interval_start", "partitions"):
            manifest[k] = prev.get(k)
    lake.write_json(key, manifest)
