"""Golden point-in-time tests on a fixture lake with reposts."""

from __future__ import annotations

import inspect
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from conftest import (
    DAM_HE1,
    DAM_P1,
    DAM_P2,
    DAY,
    I1,
    I2,
    I_LATE,
    P1,
    P2,
    P_LATE,
    POISONED_DAY,
    FixtureLake,
    utc,
    write_posting,
)

from ercot_lake import (
    CatalogNotFoundError,
    LakeReader,
    UnknownProductError,
    UnsupportedSchemaVersionError,
    WrongTableError,
)
from ercot_lake.contract import CATALOG_KEY, SCHEMAS, merged_key
from ercot_lake.reader import COLUMNS, POSTING_COLUMNS, s3_setup_sql

SEC = timedelta(seconds=1)
RT = "np6-905-cd"


def hb_north_i1(reader: LakeReader, as_of: datetime) -> list[float]:
    t = reader.spp_by_date(RT, DAY, as_of=as_of, points=["HB_NORTH"])
    return [r["price_mwh"] for r in t.to_pylist() if r["interval_start"] == I1]


# -- the three cases every point-in-time read must get right -----------------------------


def test_as_of_before_first_posting_sees_nothing(lake: FixtureLake) -> None:
    reader = LakeReader(lake.root)
    assert reader.spp_by_date(RT, DAY, as_of=P1 - SEC).num_rows == 0


def test_as_of_between_postings_sees_the_original(lake: FixtureLake) -> None:
    reader = LakeReader(lake.root)
    assert hb_north_i1(reader, P1) == [30.0]  # posted_at <= as_of is inclusive
    assert hb_north_i1(reader, P2 - SEC) == [30.0]


def test_as_of_after_repost_sees_the_correction(lake: FixtureLake) -> None:
    reader = LakeReader(lake.root)
    assert hb_north_i1(reader, P2) == [35.0]
    # The rows the repost did not touch keep their first posting.
    rows = reader.spp_by_date(RT, DAY, as_of=P2).to_pylist()
    i2 = [r for r in rows if r["interval_start"] == I2]
    assert [(r["price_mwh"], r["posted_at"]) for r in i2] == [(32.0, P1)]


# -- business keys -------------------------------------------------------------------------


def test_load_zone_types_are_distinct_rows(lake: FixtureLake) -> None:
    rows = LakeReader(lake.root).spp_by_date(RT, DAY, as_of=P2, points=["LZ_HOUSTON"]).to_pylist()
    assert sorted((r["settlement_point_type"], r["price_mwh"]) for r in rows) == [
        ("LZ", 31.0),
        ("LZEW", 31.5),
    ]


def test_null_settlement_point_type_dedupes(lake: FixtureLake) -> None:
    reader = LakeReader(lake.root)
    day = DAM_HE1.date()  # 05:00Z on Sep 4 is HE01 of Sep 4 CT
    before = reader.spp_by_date("np4-190-cd", day, as_of=DAM_P2 - SEC).to_pylist()
    after = reader.spp_by_date("np4-190-cd", day, as_of=DAM_P2).to_pylist()
    assert {r["settlement_point"]: r["price_mwh"] for r in before} == {
        "HB_NORTH": 40.0,
        "HB_WEST": 38.0,
    }
    assert {r["settlement_point"]: r["price_mwh"] for r in after} == {
        "HB_NORTH": 41.0,
        "HB_WEST": 38.0,
    }
    assert all(r["settlement_point_type"] is None for r in after)
    assert len(after) == 2
    assert reader.spp_by_date("np4-190-cd", day, as_of=DAM_P1 - SEC).num_rows == 0


def test_mcpc_repost(lake: FixtureLake) -> None:
    reader = LakeReader(lake.root)
    before = reader.mcpc_by_date("np6-331-cd", DAY, as_of=P2 - SEC).to_pylist()
    after = reader.mcpc_by_date("np6-331-cd", DAY, as_of=P2).to_pylist()
    assert {r["as_type"]: r["mcpc_mw"] for r in before} == {"REGUP": 5.0, "RRS": 3.0}
    assert {r["as_type"]: r["mcpc_mw"] for r in after} == {"REGUP": 6.0, "RRS": 3.0}
    only = reader.mcpc_by_date("np6-331-cd", DAY, as_of=P2, as_types=["RRS"]).to_pylist()
    assert [r["as_type"] for r in only] == ["RRS"]


def test_series_repost_and_name_filter(lake: FixtureLake) -> None:
    reader = LakeReader(lake.root)
    after = reader.series_by_date("np4-732-cd", DAY, as_of=P2).to_pylist()
    assert {r["series"]: r["value"] for r in after} == {
        "wind:STWPF_SYSTEM_WIDE": 1100.0,
        "wind:WGRPP_SYSTEM_WIDE": 900.0,
    }
    ranged = reader.series_by_range(
        "np4-732-cd", I1, I1 + timedelta(hours=1), as_of=P2 - SEC, names=["wind:STWPF_SYSTEM_WIDE"]
    ).to_pylist()
    assert [(r["series"], r["value"]) for r in ranged] == [("wind:STWPF_SYSTEM_WIDE", 1000.0)]


# -- ranges and partitions -----------------------------------------------------------------


def test_partition_is_the_ct_delivery_date(lake: FixtureLake) -> None:
    # 04:30Z on Sep 4 is 23:30 CDT on Sep 3, so it is in the Sep 3 partition.
    rows = LakeReader(lake.root).spp_by_date(RT, DAY, as_of=P_LATE).to_pylist()
    assert I_LATE in {r["interval_start"] for r in rows}


def test_range_is_half_open(lake: FixtureLake) -> None:
    reader = LakeReader(lake.root)
    rows = reader.spp_by_range(RT, I1, I2, as_of=P2).to_pylist()
    assert {r["interval_start"] for r in rows} == {I1}
    both = reader.spp_by_range(RT, I1, I2 + timedelta(minutes=15), as_of=P2).to_pylist()
    assert {r["interval_start"] for r in both} == {I1, I2}


def test_range_crossing_ct_midnight(lake: FixtureLake) -> None:
    reader = LakeReader(lake.root)
    rows = reader.spp_by_range(
        RT, I_LATE, I_LATE + timedelta(minutes=15), as_of=P_LATE, points=["HB_NORTH"]
    ).to_pylist()
    assert [(r["interval_start"], r["price_mwh"]) for r in rows] == [(I_LATE, 20.0)]


def test_only_requested_days_are_listed(lake: FixtureLake) -> None:
    reader = LakeReader(lake.root)
    # Sep 3 and Sep 4 are clean; the corrupt Sep 5 partition is never opened...
    reader.spp_by_range(RT, I1, utc(2026, 9, 5, 5, 0), as_of=P2)
    # ...and would break a query that did open it, so the check above is meaningful.
    with pytest.raises(duckdb.Error):
        reader.spp_by_date(RT, POISONED_DAY, as_of=P2)


def test_range_limit(lake: FixtureLake) -> None:
    reader = LakeReader(lake.root)
    with pytest.raises(ValueError, match="longer than"):
        reader.spp_by_range(RT, I1, I1 + timedelta(days=reader.max_days + 1), as_of=P2)
    with pytest.raises(ValueError, match="empty interval range"):
        reader.spp_by_range(RT, I2, I1, as_of=P2)


# -- every version -------------------------------------------------------------------------


def test_postings_keep_every_version_up_to_as_of(lake: FixtureLake) -> None:
    reader = LakeReader(lake.root)
    t = reader.postings(RT, I1, I2, as_of=P2, only=["HB_NORTH"])
    assert t.column_names == [*COLUMNS["spp"], *POSTING_COLUMNS]
    rows = t.to_pylist()
    assert [(r["price_mwh"], r["posted_at"]) for r in rows] == [(30.0, P1), (35.0, P2)]
    assert all(r["ingested_at"] == r["posted_at"] + timedelta(minutes=2) for r in rows)
    early = reader.postings(RT, I1, I2, as_of=P2 - SEC, only=["HB_NORTH"]).to_pylist()
    assert [r["price_mwh"] for r in early] == [30.0]


# -- no read without as_of -----------------------------------------------------------------


QUERIES = [
    "spp_by_date",
    "spp_by_range",
    "mcpc_by_date",
    "mcpc_by_range",
    "series_by_date",
    "series_by_range",
    "postings",
]


def test_every_query_requires_as_of() -> None:
    public = {
        name
        for name, fn in inspect.getmembers(LakeReader, inspect.isfunction)
        if not name.startswith("_") and name not in {"reconnect", "close", "products"}
    }
    assert public == set(QUERIES), "a new public query must be listed (and take as_of)"
    for name in QUERIES:
        param = inspect.signature(getattr(LakeReader, name)).parameters["as_of"]
        assert param.kind is inspect.Parameter.KEYWORD_ONLY
        assert param.default is inspect.Parameter.empty


def test_naive_as_of_is_refused(lake: FixtureLake) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        LakeReader(lake.root).spp_by_date(RT, DAY, as_of=datetime(2026, 9, 3, 21))


# -- catalog, tables and schema versions ---------------------------------------------------


def test_products_come_from_the_catalog(lake: FixtureLake) -> None:
    assert LakeReader(lake.root).products() == [
        "np4-190-cd",
        "np4-732-cd",
        "np6-331-cd",
        "np6-905-cd",
    ]


def test_wrong_table_and_unknown_product(lake: FixtureLake) -> None:
    reader = LakeReader(lake.root)
    with pytest.raises(WrongTableError, match="spp product, not mcpc"):
        reader.mcpc_by_date(RT, DAY, as_of=P2)
    with pytest.raises(UnknownProductError, match="np9-999-cd"):
        reader.spp_by_date("np9-999-cd", DAY, as_of=P2)


def test_missing_catalog(tmp_path: Path) -> None:
    with pytest.raises(CatalogNotFoundError, match="ercot-pit-lake root"):
        LakeReader(tmp_path).spp_by_date(RT, DAY, as_of=P2)


def test_rows_from_a_newer_schema_are_refused(lake: FixtureLake) -> None:
    write_posting(
        lake.root,
        "np6-331-cd",
        "mcpc",
        P2 + SEC,
        [{"interval_start": I2, "as_type": "ECRS", "mcpc_mw": 1.0}],
        interval_minutes=15,
        schema_version=2,
    )
    with pytest.raises(UnsupportedSchemaVersionError, match="upgrade ercot-lake"):
        LakeReader(lake.root).mcpc_by_date("np6-331-cd", DAY, as_of=P2 + SEC)


def test_empty_result_has_contract_types(lake: FixtureLake) -> None:
    t = LakeReader(lake.root).spp_by_date(RT, utc(2026, 8, 1, 0, 0).date(), as_of=P2)
    assert t.num_rows == 0
    assert t.schema == SCHEMAS["spp"].empty_table().select(list(COLUMNS["spp"])).schema


def test_result_types_match_the_contract(lake: FixtureLake) -> None:
    t = LakeReader(lake.root).spp_by_date(RT, DAY, as_of=P2)
    expected = SCHEMAS["spp"]
    for name in t.column_names:
        assert t.schema.field(name).type == expected.field(name).type, name


# -- compaction does not change any answer -------------------------------------------------


def _compact(root: Path, product: str) -> None:
    """Merge each partition's part- files into one merged- file, as compaction does."""
    for part_dir in (root / "curated" / product).iterdir():
        parts = sorted(part_dir.glob("part-*.parquet"))
        if len(parts) < 2:
            continue
        day = date.fromisoformat(part_dir.name.removeprefix("date="))
        merged = pa.concat_tables(pq.read_table(p) for p in parts)
        pq.write_table(merged, root / merged_key(product, day, utc(2026, 9, 10, 0, 0)))
        for p in parts:
            p.unlink()


@pytest.mark.parametrize(
    "as_of",
    [P1 - SEC, P1, P2 - SEC, P2, P_LATE, P_LATE + timedelta(days=1)],
    ids=lambda t: t.isoformat(),
)
def test_compaction_preserves_point_in_time_answers(
    lake: FixtureLake, tmp_path: Path, as_of: datetime
) -> None:
    compacted = tmp_path / "compacted"
    shutil.copytree(lake.root, compacted)
    shutil.rmtree(compacted / "curated" / RT / f"date={POISONED_DAY}")
    _compact(compacted, RT)
    assert not list((compacted / "curated" / RT / f"date={DAY}").glob("part-*"))
    before = LakeReader(lake.root).spp_by_date(RT, DAY, as_of=as_of)
    after = LakeReader(compacted).spp_by_date(RT, DAY, as_of=as_of)
    assert after.equals(before)


# -- S3 ------------------------------------------------------------------------------------


def test_s3_uses_the_standard_credential_chain() -> None:
    sql = " ".join(s3_setup_sql("us-east-2"))
    assert "PROVIDER credential_chain" in sql
    assert "REFRESH auto" in sql
    assert "REGION 'us-east-2'" in sql
    assert "REGION" not in " ".join(s3_setup_sql(None))


def test_region_is_validated() -> None:
    with pytest.raises(ValueError, match="not an AWS region"):
        LakeReader("s3://bucket", region="us-east-2'; DROP")


def test_local_root_is_absolute(lake: FixtureLake) -> None:
    assert LakeReader(lake.root).root == lake.root.resolve().as_posix()
    assert (lake.root / CATALOG_KEY).exists()


# -- connection lifecycle ------------------------------------------------------------------


def test_threads_reconnect_and_context_manager(lake: FixtureLake) -> None:
    with LakeReader(lake.root, threads=4) as reader:
        assert reader._con.execute("SELECT current_setting('threads')").fetchone() == (4,)
        before = reader.mcpc_by_range("np6-331-cd", I1, I2, as_of=P2)
        reader.reconnect()  # e.g. after credentials rotate; answers are unchanged
        assert reader.mcpc_by_range("np6-331-cd", I1, I2, as_of=P2).equals(before)
    with pytest.raises(duckdb.ConnectionException):
        reader.mcpc_by_range("np6-331-cd", I1, I2, as_of=P2)


def test_postings_of_an_empty_window(lake: FixtureLake) -> None:
    t = LakeReader(lake.root).postings(RT, utc(2026, 8, 1, 5, 0), utc(2026, 8, 1, 6, 0), as_of=P2)
    assert t.num_rows == 0
    assert t.column_names == [*COLUMNS["spp"], *POSTING_COLUMNS]
