"""The ingest loop against an in-memory document source: raw, curated, watermark, manifest."""

from __future__ import annotations

import io
import zipfile
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from ercot_lake.contract import manifest_key
from ercot_lake.timeutil import utc_to_ct
from ingest.config import LakeConfig, Product, Settings
from ingest.ercot_api import DocumentNotReadyError
from ingest.lake import Lake
from ingest.run import Doc, Run, Window, resolve_window, run_product
from ingest.state import LocalStateStore, Watermark
from ingest.transforms import SchemaDriftError

T0 = datetime(2026, 9, 4, 16, 2, 1, tzinfo=UTC)  # 11:02:01 CDT
CSV_HEADER = (
    "DeliveryDate,DeliveryHour,DeliveryInterval,SettlementPointName,SettlementPointType,"
    "SettlementPointPrice,DSTFlag\n"
)

Env = tuple[Product, Lake, LocalStateStore]


def posting(posted_at: datetime, rows: str, *, name: str = "SPPHLZNP6905") -> bytes:
    """An ERCOT posting zip: one CSV named with the posting time in CT, as ERCOT names it."""
    ct = utc_to_ct(posted_at)
    fname = f"cdr.00012301.0000000000000000.{ct:%Y%m%d}.{ct:%H%M%S}.{name}.csv"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(fname, CSV_HEADER + rows)
    return buf.getvalue()


def doc(posted_at: datetime, rows: str, *, name: str = "SPPHLZNP6905") -> Doc:
    blob = posting(posted_at, rows, name=name)
    return Doc(posted_at=posted_at, name=name, load=lambda: blob)


@pytest.fixture
def env(tmp_path: Path, cfg: Settings) -> Env:
    lake = Lake(LakeConfig(root=str(tmp_path / "lake")))
    return cfg.product("np6-905-cd"), lake, LocalStateStore(tmp_path / "state")


def run(env: Env, docs: list[Doc] | Iterator[Doc], window: Window, **kw: object) -> object:
    product, lake, state = env
    return run_product(
        Run(product=product, lake=lake, state=state, docs=docs, window=window, **kw)  # type: ignore[arg-type]
    )


def test_resolve_window_uses_lookback_then_watermark(cfg: Settings) -> None:
    p = cfg.product("np6-905-cd")
    w = resolve_window(p, Watermark(product=p.key), now=T0, explicit=None)
    assert w == Window(T0 - timedelta(hours=p.initial_lookback_hours), T0)
    resumed = Watermark(product=p.key, last_posted_at=T0 - timedelta(minutes=15))
    assert resolve_window(p, resumed, now=T0, explicit=None).post_from == resumed.last_posted_at
    explicit = Window(T0 - timedelta(days=1), T0)
    assert resolve_window(p, Watermark(product=p.key), now=T0, explicit=explicit) == explicit


def test_happy_path_writes_raw_curated_watermark_manifest(env: Env) -> None:
    product, lake, state = env
    docs = [
        doc(T0, "09/04/2026,11,4,HB_NORTH,HU,30.0,N\n09/04/2026,11,4,LZ_HOUSTON,LZ,31.0,N\n"),
        doc(T0 + timedelta(minutes=15), "09/04/2026,12,1,HB_NORTH,HU,32.0,N\n"),
    ]
    s = run(env, docs, Window(T0 - timedelta(hours=1), T0 + timedelta(hours=1)))

    assert (s.status, s.docs, s.rows_written) == ("ok", 2, 3)  # type: ignore[attr-defined]
    assert s.partitions == {"2026-09-04"}  # type: ignore[attr-defined]
    assert s.last_posted_at == docs[1].posted_at  # type: ignore[attr-defined]
    assert s.latest_interval_start == datetime(2026, 9, 4, 16, 0, tzinfo=UTC)  # type: ignore[attr-defined]

    assert lake.list_keys("raw/np6-905-cd") == [
        "raw/np6-905-cd/date=2026-09-04/posted=20260904T160201Z.zip",
        "raw/np6-905-cd/date=2026-09-04/posted=20260904T161701Z.zip",
    ]
    parts = lake.list_keys("curated/np6-905-cd")
    assert parts == [
        "curated/np6-905-cd/date=2026-09-04/part-20260904T160201Z.parquet",
        "curated/np6-905-cd/date=2026-09-04/part-20260904T161701Z.parquet",
    ]
    t = pq.read_table(Path(lake.root) / parts[0])
    assert t.num_rows == 2
    assert set(t.column("posted_at").to_pylist()) == {T0}

    wm = state.get(product.key)
    assert (wm.last_status, wm.last_posted_at) == ("ok", docs[1].posted_at)

    m = lake.read_json(manifest_key(product.key))
    assert (m["docs"], m["rows_written"], m["schema_version"]) == (2, 3, 1)
    assert m["last_posted_at"] == docs[1].posted_at.isoformat()
    assert "lake_root" not in m  # the manifest never names the bucket


def test_doc_spanning_midnight_splits_partitions(env: Env) -> None:
    _, lake, _ = env
    posted = datetime(2026, 9, 5, 5, 2, 1, tzinfo=UTC)  # 00:02 CDT Sep 5
    rows = "09/04/2026,24,4,HB_NORTH,HU,30.0,N\n09/05/2026,1,1,HB_NORTH,HU,31.0,N\n"
    s = run(env, [doc(posted, rows)], Window(posted - timedelta(hours=1), posted))
    assert s.partitions == {"2026-09-04", "2026-09-05"}  # type: ignore[attr-defined]
    assert "raw/np6-905-cd/date=2026-09-05/" in lake.list_keys("raw")[0]


def test_failure_keeps_watermark_at_last_good_doc_and_the_next_run_resumes(env: Env) -> None:
    product, lake, state = env
    good = doc(T0, "09/04/2026,11,4,HB_NORTH,HU,30.0,N\n")
    second = doc(T0 + timedelta(minutes=15), "09/04/2026,12,1,HB_NORTH,HU,32.0,N\n")

    def boom() -> bytes:
        msg = "download failed"
        raise RuntimeError(msg)

    bad = Doc(posted_at=second.posted_at, name="bad", load=boom)
    window = Window(T0 - timedelta(hours=1), T0 + timedelta(hours=1))
    with pytest.raises(RuntimeError, match="download failed"):
        run(env, [good, bad], window)

    wm = state.get(product.key)
    assert (wm.last_status, wm.last_error) == ("error", "RuntimeError: download failed")
    assert wm.last_posted_at == T0  # past the good doc, not the bad one
    assert not lake.exists(manifest_key(product.key))  # no manifest on failure

    # The next scheduled run lists from the watermark and picks up exactly the missing doc.
    listing = [good, second]
    resumed = resolve_window(product, wm, now=T0 + timedelta(hours=1), explicit=None)
    todo = [d for d in listing if resumed.post_from < d.posted_at <= resumed.post_to]
    s = run(env, todo, resumed)
    assert (s.docs, s.status) == (1, "ok")  # type: ignore[attr-defined]
    assert state.get(product.key).last_posted_at == second.posted_at
    assert len(lake.list_keys("curated/np6-905-cd")) == 2


def test_schema_drift_fails_the_run_after_raw_lands(env: Env) -> None:
    _, lake, _ = env
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("cdr.1.0.20260904.110201.X.csv", "DeliveryDate,Renamed\n09/04/2026,1\n")
    blob = buf.getvalue()
    drift = Doc(posted_at=T0, name="drift", load=lambda: blob)
    with pytest.raises(SchemaDriftError):
        run(env, [drift], Window(T0 - timedelta(hours=1), T0))
    # raw landed before the transform failed: nothing is lost, a fix can replay it
    assert lake.list_keys("raw/np6-905-cd") == [
        "raw/np6-905-cd/date=2026-09-04/posted=20260904T160201Z.zip"
    ]
    assert lake.list_keys("curated") == []


def test_rerun_is_idempotent(env: Env) -> None:
    product, lake, state = env
    d = doc(T0, "09/04/2026,11,4,HB_NORTH,HU,30.0,N\n")
    for _ in range(2):
        run(env, [d], Window(T0 - timedelta(hours=1), T0), update_watermark=False)
    assert len(lake.list_keys("raw")) == 1
    assert len(lake.list_keys("curated")) == 1
    assert state.get(product.key).last_posted_at is None  # backfill leaves the watermark alone


def test_a_run_with_nothing_new_keeps_the_previous_manifest_data(env: Env) -> None:
    product, lake, state = env
    run(env, [doc(T0, "09/04/2026,11,4,HB_NORTH,HU,30.0,N\n")], Window(T0 - timedelta(hours=1), T0))
    s = run(env, iter(()), Window(T0, T0 + timedelta(minutes=15)))
    assert s.docs == 0  # type: ignore[attr-defined]
    m = lake.read_json(manifest_key(product.key))
    assert (m["docs"], m["rows_written"]) == (0, 0)
    assert m["last_posted_at"] == T0.isoformat()  # carried forward
    assert m["partitions"] == ["2026-09-04"]
    assert state.get(product.key).last_status == "ok"


def test_a_listed_but_not_ready_doc_defers_without_error(env: Env) -> None:
    """A listed-but-undownloadable doc ends the run cleanly: status ok, watermark at the last
    good doc, later docs untouched (no holes under the watermark)."""
    product, _, state = env

    def not_ready() -> bytes:
        msg = "listed but not downloadable yet (400)"
        raise DocumentNotReadyError(msg)

    good = doc(T0, "09/04/2026,11,4,HB_NORTH,HU,30.0,N\n")
    pending = Doc(posted_at=T0 + timedelta(minutes=15), name="pending", load=not_ready)
    later_blob = posting(T0 + timedelta(minutes=30), "09/04/2026,11,6,HB_NORTH,HU,32.0,N\n")
    calls = {"later": 0}

    def later_load() -> bytes:
        calls["later"] += 1
        return later_blob

    later = Doc(posted_at=T0 + timedelta(minutes=30), name="later", load=later_load)
    s = run(env, [good, pending, later], Window(T0 - timedelta(hours=1), T0 + timedelta(hours=1)))
    assert (s.status, s.error, s.docs, s.deferred) == ("ok", None, 1, "pending")  # type: ignore[attr-defined]
    assert calls["later"] == 0
    wm = state.get(product.key)
    assert (wm.last_status, wm.last_posted_at) == ("ok", T0)


def test_backfill_leaves_the_manifest_alone(env: Env) -> None:
    """Freshness reads the manifest. A backfill of months-old postings must not replace it,
    or last_posted_at jumps back months and the product looks stale."""
    product, lake, _ = env
    run(env, [doc(T0, "09/04/2026,11,4,HB_NORTH,HU,30.0,N\n")], Window(T0 - timedelta(hours=1), T0))
    before = lake.read_json(manifest_key(product.key))
    old = doc(T0 - timedelta(days=200), "02/16/2026,11,4,HB_NORTH,HU,20.0,N\n")
    window = Window(T0 - timedelta(days=201), T0 - timedelta(days=199))
    run(env, [old], window, update_watermark=False)
    after = lake.read_json(manifest_key(product.key))
    assert after["last_posted_at"] == before["last_posted_at"] == T0.isoformat()
    assert lake.list_keys("curated/np6-905-cd/date=2026-02-16")  # the data itself did land


def test_a_second_pass_posting_on_the_fall_back_night_keeps_its_own_keys(env: Env) -> None:
    """01:30:20 happens twice on 2026-11-01 and the zip's filename says only "01:30:20". The
    listed time (already resolved in posting order) decides, so the second pass gets its own
    raw and curated keys instead of overwriting the first pass's."""
    _, lake, _ = env
    first = datetime(2026, 11, 1, 6, 30, 20, tzinfo=UTC)  # 01:30:20 CDT
    second = first + timedelta(hours=1)  # 01:30:20 CST
    docs = [
        doc(first, "11/01/2026,2,2,HB_NORTH,HU,30.0,N\n"),
        doc(second, "11/01/2026,2,2,HB_NORTH,HU,31.0,Y\n"),
    ]
    run(env, docs, Window(first - timedelta(minutes=1), second))
    assert lake.list_keys("raw/np6-905-cd") == [
        "raw/np6-905-cd/date=2026-11-01/posted=20261101T063020Z.zip",
        "raw/np6-905-cd/date=2026-11-01/posted=20261101T073020Z.zip",
    ]
    parts = lake.list_keys("curated/np6-905-cd")
    assert [p.rsplit("/", 1)[1] for p in parts] == [
        "part-20261101T063020Z.parquet",
        "part-20261101T073020Z.parquet",
    ]
    t = pq.read_table(Path(lake.root) / parts[1])
    assert t.column("posted_at").to_pylist() == [second]
    assert t.column("dst_flag").to_pylist() == [True]
