"""Backfill sources: monthly bundles, the archive listing, the yearly workbook; and the rule
that one posting yields the same curated rows whichever way it arrives."""

from __future__ import annotations

import io
import zipfile
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import openpyxl
import pyarrow.parquet as pq

from ercot_lake.contract import raw_key
from ercot_lake.timeutil import CT, utc_to_ct
from ingest.config import LakeConfig, Settings
from ingest.ercot_api import ArchiveDoc
from ingest.lake import Lake
from ingest.run import (
    Doc,
    Run,
    Window,
    archive_docs,
    bundle_docs,
    bundle_members,
    hist_docs,
    month_span,
    run_product,
)
from ingest.state import LocalStateStore


def _posting_zip(stamp_ct: datetime, name: str, csv: str, doc_id: int = 0) -> tuple[str, bytes]:
    """(member name inside a bundle, posting zip) as ERCOT names them."""
    inner = f"cdr.00012329.0000000000000000.{stamp_ct:%Y%m%d}.{stamp_ct:%H%M%S}.{name}.csv"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(inner, csv)
    return f"{doc_id or abs(hash(inner)) % 10**9}.{inner[:-4]}_csv.zip", buf.getvalue()


def _bundle(*postings: tuple[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, blob in postings:
            zf.writestr(name, blob)
    return buf.getvalue()


class FakeClient:
    """The slice of ErcotClient the document sources use."""

    def __init__(
        self,
        bundles: dict[str, bytes],
        archive: list[ArchiveDoc],
        blobs: dict[int, bytes] | None = None,
    ) -> None:
        self.bundles, self.archive, self.blobs = bundles, archive, blobs or {}
        self.calls: list[str] = []

    def list_bundles(self, product_key: str) -> dict[str, Any]:
        return {
            "bundles": [{"docId": -i, "friendlyName": f"X_{m}"} for i, m in enumerate(self.bundles)]
        }

    def download_bundle(self, product_key: str, doc_id: int) -> bytes:
        month = list(self.bundles)[-doc_id]
        self.calls.append(f"bundle:{month}")
        return self.bundles[month]

    def iter_archive_docs(
        self, archive_id: str, post_from: datetime, post_to: datetime
    ) -> Iterator[ArchiveDoc]:
        self.calls.append(f"archive:{archive_id}:{post_from:%Y-%m-%d}..{post_to:%Y-%m-%d}")
        return iter([d for d in self.archive if post_from < d.posted_at <= post_to])

    def download_archive(self, archive_id: str, doc_id: int) -> bytes:
        return self.blobs[doc_id]


# -- month arithmetic and bundles ----------------------------------------------------------


def test_month_span_is_central_time() -> None:
    w = Window(datetime(2025, 12, 5, tzinfo=UTC), datetime(2026, 2, 1, tzinfo=UTC))
    # 2026-02-01 00:00Z is still 2026-01-31 18:00 CT: no February
    assert month_span(w) == ["2025-12", "2026-01"]
    w = Window(datetime(2025, 12, 5, tzinfo=UTC), datetime(2026, 2, 1, 6, tzinfo=UTC))
    assert month_span(w) == ["2025-12", "2026-01", "2026-02"]


def test_month_end_evening_comes_from_that_months_bundle(cfg: Settings) -> None:
    """ERCOT cuts bundles at CT midnight: a posting at 2026-01-01 03:00Z (Dec 31 21:00 CT)
    belongs to the December bundle even though it is January in UTC."""
    p = cfg.product("np4-188-cd")
    late = _posting_zip(datetime(2025, 12, 31, 21, 0, 0), "DAMCPCNP4188", "h\n1")
    client = FakeClient({"2025-12": _bundle(late), "2026-01": _bundle()}, archive=[])
    window = Window(datetime(2026, 1, 1, 0, tzinfo=UTC), datetime(2026, 1, 1, 6, tzinfo=UTC))
    docs = list(bundle_docs(client, p, window))  # type: ignore[arg-type]
    assert [d.posted_at for d in docs] == [datetime(2025, 12, 31, 21, tzinfo=CT).astimezone(UTC)]
    assert client.calls == ["bundle:2025-12"]


def test_bundle_members_become_docs_in_order_within_window(cfg: Settings) -> None:
    p = cfg.product("np4-188-cd")
    a = _posting_zip(datetime(2025, 12, 2, 12, 32, 20), "DAMCPCNP4188", "h\n1")
    b = _posting_zip(datetime(2025, 12, 6, 12, 33, 1), "DAMCPCNP4188", "h\n2")
    c = _posting_zip(datetime(2025, 12, 4, 12, 30, 0), "DAMCPCNP4188", "h\n3")
    client = FakeClient({"2025-12": _bundle(a, b, c)}, archive=[])
    window = Window(
        datetime(2025, 12, 3, tzinfo=CT).astimezone(UTC), datetime(2026, 1, 1, tzinfo=UTC)
    )
    docs = list(bundle_docs(client, p, window))  # type: ignore[arg-type]
    assert [d.posted_at for d in docs] == [
        datetime(2025, 12, 4, 12, 30, tzinfo=CT).astimezone(UTC),
        datetime(2025, 12, 6, 12, 33, 1, tzinfo=CT).astimezone(UTC),
    ]
    # each Doc loads the inner posting zip, exactly what live ingest downloads
    assert docs[0].load()[:2] == b"PK"
    assert client.calls == ["bundle:2025-12"]


def test_months_without_a_bundle_fall_back_to_the_archive(cfg: Settings) -> None:
    p = cfg.product("np4-188-cd")
    client = FakeClient({"2025-12": _bundle()}, archive=[])
    window = Window(datetime(2025, 12, 20, tzinfo=UTC), datetime(2026, 1, 10, tzinfo=UTC))
    list(bundle_docs(client, p, window))  # type: ignore[arg-type]
    assert client.calls == ["bundle:2025-12", "archive:NP4-188-CD:2026-01-01..2026-01-10"]


def test_bundle_stamps_in_the_repeated_hour_follow_doc_id_order() -> None:
    """A bundle's member names carry CT stamps without a DST flag. Doc IDs grow with posting
    order, so the second 01:30:20 on the fall-back night is the later instant."""
    first = _posting_zip(datetime(2026, 11, 1, 1, 30, 20), "LAMBDA", "h\n1", doc_id=101)
    second = _posting_zip(datetime(2026, 11, 1, 1, 30, 20), "LAMBDA", "h\n2", doc_id=102)
    members = bundle_members(_bundle(second, first))  # zip order must not matter
    assert [(posted, name.split(".", 1)[0]) for posted, name, _ in members] == [
        (datetime(2026, 11, 1, 6, 30, 20, tzinfo=UTC), "101"),
        (datetime(2026, 11, 1, 7, 30, 20, tzinfo=UTC), "102"),
    ]


def test_bundle_members_without_a_stamp_are_skipped() -> None:
    good = _posting_zip(datetime(2026, 6, 1, 0, 2, 1), "A", "h\n1", doc_id=7)
    blob = _bundle(good, ("readme.zip", b"PK"), ("notes.txt", b"x"))
    assert [n for _, n, _ in bundle_members(blob)] == [good[0]]


# -- live and backfill write the same thing -----------------------------------------------


def test_one_posting_live_or_from_a_bundle_gives_identical_curated_rows(
    tmp_path: Path, cfg: Settings
) -> None:
    p = cfg.product("np6-905-cd")
    posted = datetime(2026, 9, 4, 16, 17, 1, tzinfo=UTC)
    csv = (
        "DeliveryDate,DeliveryHour,DeliveryInterval,SettlementPointName,SettlementPointType,"
        "SettlementPointPrice,DSTFlag\n09/04/2026,12,1,HB_NORTH,HU,32.0,N\n"
        "09/04/2026,12,1,LZ_HOUSTON,LZEW,33.5,N\n"
    )
    member, blob = _posting_zip(utc_to_ct(posted).replace(tzinfo=None), "SPPHLZNP6905", csv, 55)
    window = Window(posted - timedelta(hours=1), posted)
    live_client = FakeClient({}, [ArchiveDoc("NP6-905-CD", 55, posted, "x")], {55: blob})
    backfill_client = FakeClient({"2026-09": _bundle((member, blob))}, archive=[])

    tables = []
    for name, docs in (
        ("live", archive_docs(live_client, p, window)),  # type: ignore[arg-type]
        ("backfill", bundle_docs(backfill_client, p, window)),  # type: ignore[arg-type]
    ):
        lake = Lake(LakeConfig(root=str(tmp_path / name)))
        state = LocalStateStore(tmp_path / f"{name}-state")
        run_product(Run(p, lake, state, docs, window, update_watermark=name == "live"))
        (key,) = lake.list_keys("curated")
        assert lake.exists(raw_key(p.key, posted))
        tables.append((key, pq.read_table(Path(lake.root) / key)))

    (live_key, live), (back_key, back) = tables
    assert live_key == back_key
    assert live.schema == back.schema
    assert live.drop_columns(["ingested_at"]).equals(back.drop_columns(["ingested_at"]))


# -- the yearly RT clearing-price workbook -------------------------------------------------

XLSX_NAME = "rpt.00025570.0000000000000000.20260830.100041.HIST_15Min_RTM_MCPC_2026.xlsx"


def _workbook() -> bytes:
    wb = openpyxl.Workbook()
    info = wb.active
    assert info is not None
    info.title = "Report Info"
    for month, days in (("Jul", [30, 31]), ("Aug", [1, 2])):
        ws = wb.create_sheet(month)
        ws.append(
            [
                "Delivery Date",
                "Delivery Hour",
                "Delivery Interval",
                "AS Type",
                "MCPC",
                "Repeated Hour Flag",
            ]
        )
        m = 7 if month == "Jul" else 8
        for d in days:
            for hour in (1, 2):
                for interval in (1, 2, 3, 4):
                    for as_type in ("REGUP", "REGDN", "RRS", "ECRS", "NSPIN"):
                        ws.append([datetime(2026, m, d), hour, interval, as_type, 1.5, "N"])
    buf = io.BytesIO()
    wb.save(buf)
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w") as zf:
        zf.writestr(XLSX_NAME, buf.getvalue())
    return zbuf.getvalue()


def test_workbook_fills_only_the_requested_delivery_days(tmp_path: Path, cfg: Settings) -> None:
    p = cfg.product("np6-331-cd")
    lake = Lake(LakeConfig(root=str(tmp_path / "lake")))
    posted = datetime(2026, 8, 30, 15, 0, 41, tzinfo=UTC)
    blob = _workbook()
    client = FakeClient({}, [ArchiveDoc("NP6-796-ER", 9, posted, "HIST")], {9: blob})
    window = Window(datetime(2026, 8, 29, tzinfo=UTC), datetime(2026, 8, 31, tzinfo=UTC))
    docs = list(hist_docs(client, p, window))  # type: ignore[arg-type]
    assert client.calls == ["archive:NP6-796-ER:2026-08-29..2026-08-31"]
    summary = run_product(
        Run(
            product=p,
            lake=lake,
            state=LocalStateStore(tmp_path / "state"),
            docs=docs,
            window=window,
            update_watermark=False,
            delivery_range=(date(2026, 7, 31), date(2026, 8, 1)),
        )
    )
    assert summary.status == "ok"
    assert summary.partitions == {"2026-07-31", "2026-08-01"}
    assert summary.rows_written == 2 * 2 * 4 * 5  # days x hours x intervals x AS types
    assert lake.exists(raw_key(p.key, posted))  # raw is the workbook zip as received
    t = pq.read_table(Path(lake.root) / lake.list_keys("curated/np6-331-cd/date=2026-08-01")[0])
    assert set(t.column("posted_at").to_pylist()) == {posted}
    assert not lake.list_keys("curated/np6-331-cd/date=2026-07-30")


def test_hist_docs_needs_a_fallback_archive(cfg: Settings) -> None:
    import pytest

    window = Window(datetime(2026, 8, 29, tzinfo=UTC), datetime(2026, 8, 31, tzinfo=UTC))
    with pytest.raises(ValueError, match="no fallback_archive_id"):
        list(hist_docs(FakeClient({}, []), cfg.product("np6-905-cd"), window))  # type: ignore[arg-type]


def test_docs_are_lazy(cfg: Settings) -> None:
    posted = datetime(2026, 9, 4, 16, 17, 1, tzinfo=UTC)
    client = FakeClient({}, [ArchiveDoc("NP6-905-CD", 1, posted, "")], {})
    (d,) = archive_docs(client, cfg.product("np6-905-cd"), Window(posted - timedelta(1), posted))  # type: ignore[arg-type]
    assert isinstance(d, Doc)
    assert d.name == "1"  # no friendly name: the doc id
