"""Compaction: fewer files, the same answers."""

from __future__ import annotations

import os
import shutil
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import ingest.cli as cli
import ingest.handler as handler
from ercot_lake import LakeReader
from ercot_lake.contract import SCHEMAS, curated_key, merged_key, raw_key
from ercot_lake.timeutil import delivery_date_ct
from ingest.catalog import publish_catalog
from ingest.compact import compact, compact_product
from ingest.config import LakeConfig, Settings
from ingest.lake import Lake

PRODUCT = "np6-905-cd"
DAY0 = datetime(2026, 9, 3, 5, 0, tzinfo=UTC)  # 00:00 CDT
NOW = datetime(2026, 9, 10, tzinfo=UTC)
POINTS = [("HB_NORTH", "HU"), ("LZ_HOUSTON", "LZ"), ("LZ_HOUSTON", "LZEW")]


def age(lake: Lake, key: str, when: datetime) -> None:
    """Set a file's write time, as if it had been written at ``when``."""
    ts = when.timestamp()
    os.utime(Path(lake.root) / key, (ts, ts))


def write_posting(
    lake: Lake, posted: datetime, rows: list[tuple[int, int, float]], *, written: datetime
) -> list[str]:
    """One RT posting: rows of (interval index from DAY0, point index, price), one part file per
    delivery day, as ingest writes them."""
    by_day: dict[date, list[dict[str, Any]]] = {}
    for interval, point, price in rows:
        start = DAY0 + timedelta(minutes=15 * interval)
        name, kind = POINTS[point]
        by_day.setdefault(delivery_date_ct(start), []).append(
            {
                "interval_start": start,
                "interval_minutes": 15,
                "posted_at": posted,
                "ingested_at": posted + timedelta(minutes=1),
                "source": "archive",
                "schema_version": 1,
                "settlement_point": name,
                "settlement_point_type": kind,
                "price_mwh": price,
                "dst_flag": False,
            }
        )
    keys = []
    for day, day_rows in by_day.items():
        key = curated_key(PRODUCT, day, posted)
        lake.write_table(key, pa.Table.from_pylist(day_rows, schema=SCHEMAS["spp"]))
        age(lake, key, written)
        keys.append(key)
    return keys


@pytest.fixture
def lake(tmp_path: Path, cfg: Settings) -> Lake:
    lk = Lake(LakeConfig(root=str(tmp_path / "lake")))
    publish_catalog(cfg, lk)
    return lk


def files(lake: Lake, prefix: str = f"curated/{PRODUCT}/") -> list[str]:
    return [k.rsplit("/", 1)[1] for k in lake.list_keys(prefix)]


# -- the property: any as_of, same answer ----------------------------------------------------

postings = st.lists(
    st.tuples(
        st.integers(min_value=0, max_value=2000),  # minutes after DAY0 the posting went out
        st.lists(
            st.tuples(
                st.integers(min_value=0, max_value=150),  # interval: spans two delivery days
                st.integers(min_value=0, max_value=len(POINTS) - 1),
                st.floats(min_value=-250, max_value=5000, allow_nan=False, width=32),
            ),
            min_size=1,
            max_size=12,
            # ingestion refuses a posting that repeats a business key (interval, point)
            unique_by=lambda row: (row[0], row[1]),
        ),
    ),
    min_size=1,
    max_size=10,
    unique_by=lambda p: p[0],  # one posting per posting time, as ERCOT's keys are
)


@settings(
    max_examples=30, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(postings=postings, probes=st.lists(st.integers(-10, 2100), min_size=1, max_size=6))
def test_point_in_time_answers_are_the_same_after_compaction(
    tmp_path_factory: pytest.TempPathFactory,
    cfg: Settings,
    postings: list[tuple[int, list[tuple[int, int, float]]]],
    probes: list[int],
) -> None:
    root = tmp_path_factory.mktemp("prop")
    before = Lake(LakeConfig(root=str(root / "before")))
    publish_catalog(cfg, before)
    for minute, rows in postings:
        write_posting(
            before, DAY0 + timedelta(minutes=minute), rows, written=NOW - timedelta(days=1)
        )
    shutil.copytree(root / "before", root / "after")
    after = Lake(LakeConfig(root=str(root / "after")))
    compact_product(cfg.product(PRODUCT), after, now=NOW)

    window = (DAY0, DAY0 + timedelta(days=2))
    with LakeReader(before.root) as a, LakeReader(after.root) as b:
        for probe in probes:
            as_of = DAY0 + timedelta(minutes=probe)
            assert b.spp_by_range(PRODUCT, *window, as_of=as_of).equals(
                a.spp_by_range(PRODUCT, *window, as_of=as_of)
            ), as_of
            assert b.postings(PRODUCT, *window, as_of=as_of).equals(
                a.postings(PRODUCT, *window, as_of=as_of)
            ), as_of
    # and the files really were merged: at most one per partition that had any
    for day in {k.split("/")[2] for k in after.list_keys(f"curated/{PRODUCT}/")}:
        assert len(after.list_keys(f"curated/{PRODUCT}/{day}/")) == 1


# -- what it touches and what it leaves -----------------------------------------------------


def test_merges_old_parts_into_one_and_deletes_them(lake: Lake, cfg: Settings) -> None:
    old = NOW - timedelta(hours=3)
    write_posting(lake, DAY0 + timedelta(hours=1), [(4, 0, 30.0)], written=old)
    write_posting(lake, DAY0 + timedelta(hours=2), [(4, 0, 35.0), (8, 1, 31.0)], written=old)
    s = compact_product(cfg.product(PRODUCT), lake, now=NOW)
    assert (s.partitions_merged, s.files_in, s.rows, s.leftovers_removed) == (1, 2, 3, 0)
    assert files(lake) == ["merged-20260910T000000Z.parquet"]
    t = lake.read_table(merged_key(PRODUCT, date(2026, 9, 3), NOW))
    assert t.schema == SCHEMAS["spp"]
    # sorted by business key then posting time
    keys = [(r["interval_start"], r["settlement_point"], r["posted_at"]) for r in t.to_pylist()]
    assert keys == sorted(keys)


def test_a_second_run_changes_nothing(lake: Lake, cfg: Settings) -> None:
    old = NOW - timedelta(hours=3)
    write_posting(lake, DAY0 + timedelta(hours=1), [(4, 0, 30.0)], written=old)
    write_posting(lake, DAY0 + timedelta(hours=2), [(4, 0, 35.0)], written=old)
    compact_product(cfg.product(PRODUCT), lake, now=NOW)
    first = lake.list_files(f"curated/{PRODUCT}/")
    s = compact_product(cfg.product(PRODUCT), lake, now=NOW + timedelta(hours=2))
    assert s.partitions_merged == 0
    assert lake.list_files(f"curated/{PRODUCT}/") == first  # same file, same write time


def test_young_files_are_left_alone(lake: Lake, cfg: Settings) -> None:
    write_posting(lake, DAY0 + timedelta(hours=1), [(4, 0, 30.0)], written=NOW - timedelta(hours=3))
    write_posting(
        lake, DAY0 + timedelta(hours=2), [(4, 0, 35.0)], written=NOW - timedelta(minutes=5)
    )
    s = compact_product(cfg.product(PRODUCT), lake, now=NOW)
    assert s.partitions_merged == 0  # one old file is already compact; the young one waits
    assert len(files(lake)) == 2


def test_an_existing_merged_file_is_folded_in(lake: Lake, cfg: Settings) -> None:
    old = NOW - timedelta(hours=3)
    write_posting(lake, DAY0 + timedelta(hours=1), [(4, 0, 30.0)], written=old)
    write_posting(lake, DAY0 + timedelta(hours=2), [(4, 0, 35.0)], written=old)
    compact_product(cfg.product(PRODUCT), lake, now=NOW)
    first = merged_key(PRODUCT, date(2026, 9, 3), NOW)
    age(lake, first, old)
    write_posting(lake, DAY0 + timedelta(hours=3), [(4, 0, 40.0)], written=old)
    later = NOW + timedelta(hours=1)
    s = compact_product(cfg.product(PRODUCT), lake, now=later)
    assert (s.files_in, s.rows) == (2, 3)
    assert files(lake) == ["merged-20260910T010000Z.parquet"]


def test_raw_is_never_touched(lake: Lake, cfg: Settings) -> None:
    posted = DAY0 + timedelta(hours=1)
    lake.write_bytes(raw_key(PRODUCT, posted), b"PK as received")
    age(lake, raw_key(PRODUCT, posted), NOW - timedelta(days=30))
    write_posting(lake, posted, [(4, 0, 30.0)], written=NOW - timedelta(hours=3))
    write_posting(
        lake, posted + timedelta(hours=1), [(4, 0, 31.0)], written=NOW - timedelta(hours=3)
    )
    compact_product(cfg.product(PRODUCT), lake, now=NOW)
    assert lake.read_bytes(raw_key(PRODUCT, posted)) == b"PK as received"
    with pytest.raises(PermissionError, match="refusing to delete outside curated/"):
        lake.delete(raw_key(PRODUCT, posted))


def test_an_interrupted_compaction_is_repaired_without_losing_rows(
    lake: Lake, cfg: Settings
) -> None:
    """Merged file written, sources not yet deleted: the next run recognises the sources as
    already merged, and keeps each row exactly once."""
    old = NOW - timedelta(hours=3)
    keys = write_posting(lake, DAY0 + timedelta(hours=1), [(4, 0, 30.0), (5, 0, 31.0)], written=old)
    keys += write_posting(lake, DAY0 + timedelta(hours=2), [(4, 0, 35.0)], written=old)
    rows = pa.concat_tables([lake.read_table(k) for k in keys])
    leftover = merged_key(PRODUCT, date(2026, 9, 3), NOW - timedelta(hours=2))
    lake.write_table(leftover, rows)  # the merge that never got to delete its sources
    age(lake, leftover, old)
    late = write_posting(lake, DAY0 + timedelta(hours=3), [(4, 0, 36.0)], written=old)
    s = compact_product(cfg.product(PRODUCT), lake, now=NOW)
    assert (s.files_in, s.rows, s.leftovers_removed) == (4, 4, 2)
    (only,) = lake.list_keys(f"curated/{PRODUCT}/")
    assert lake.read_table(only).num_rows == 4
    assert late[0] not in lake.list_keys("curated")


def test_duplicate_rows_inside_one_posting_are_kept(lake: Lake, cfg: Settings) -> None:
    """Found by the property test: a posting that repeats a row must come out the same, or the
    every-version read (postings) changes."""
    old = NOW - timedelta(hours=3)
    write_posting(lake, DAY0 + timedelta(hours=1), [(4, 0, 30.0), (4, 0, 30.0)], written=old)
    write_posting(lake, DAY0 + timedelta(hours=2), [(5, 0, 31.0)], written=old)
    s = compact_product(cfg.product(PRODUCT), lake, now=NOW)
    assert (s.rows, s.leftovers_removed) == (3, 0)


def test_files_that_disagree_on_schema_are_skipped(lake: Lake, cfg: Settings) -> None:
    old = NOW - timedelta(hours=3)
    write_posting(lake, DAY0 + timedelta(hours=1), [(4, 0, 30.0)], written=old)
    odd = curated_key(PRODUCT, date(2026, 9, 3), DAY0 + timedelta(hours=2))
    lake.write_table(odd, pa.table({"interval_start": [DAY0], "extra": [1]}))
    age(lake, odd, old)
    s = compact_product(cfg.product(PRODUCT), lake, now=NOW)
    assert (s.partitions_merged, s.skipped) == (0, ["2026-09-03"])
    assert len(files(lake)) == 2


# -- entry points ------------------------------------------------------------------------------


def test_compact_every_product_and_the_handler(
    lake: Lake, cfg: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = NOW - timedelta(hours=3)
    write_posting(lake, DAY0 + timedelta(hours=1), [(4, 0, 30.0)], written=old)
    write_posting(lake, DAY0 + timedelta(hours=2), [(4, 0, 35.0)], written=old)
    assert {s.product for s in compact(cfg, lake, now=NOW)} == set(cfg.products)

    write_posting(lake, DAY0 + timedelta(hours=3), [(4, 0, 36.0)], written=old)
    age(lake, merged_key(PRODUCT, date(2026, 9, 3), NOW), old)
    monkeypatch.setattr(
        handler, "settings", lambda: cfg.model_copy(update={"lake": LakeConfig(root=lake.root)})
    )
    out = handler.compact({})
    merged = [p for p in out["products"] if p["partitions_merged"]]
    assert [p["product"] for p in merged] == [PRODUCT]


def test_the_cli(
    lake: Lake, cfg: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    old = NOW - timedelta(hours=3)
    write_posting(lake, DAY0 + timedelta(hours=1), [(4, 0, 30.0)], written=old)
    write_posting(lake, DAY0 + timedelta(hours=2), [(4, 0, 35.0)], written=old)
    monkeypatch.setattr(
        cli, "settings", lambda: cfg.model_copy(update={"lake": LakeConfig(root=lake.root)})
    )
    assert cli.main_compact(["--product", PRODUCT]) == 0
    assert '"partitions_merged": 1' in capsys.readouterr().out
