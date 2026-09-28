"""Lake layout, key formats and schemas, against literal expected values. A failure here means
the published layout changed: that is a contract change, not a test to update in passing."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pyarrow as pa
import pytest

from ercot_lake import contract as c

POSTED = datetime(2026, 9, 4, 4, 40, 7, tzinfo=UTC)  # 23:40 CDT on Sep 3


def test_raw_key_is_partitioned_by_ct_posting_date() -> None:
    assert c.raw_key("np6-905-cd", POSTED) == (
        "raw/np6-905-cd/date=2026-09-03/posted=20260904T044007Z.zip"
    )


def test_curated_keys() -> None:
    day = date(2026, 9, 3)
    assert c.curated_partition("np6-905-cd", day) == "curated/np6-905-cd/date=2026-09-03/"
    assert c.curated_key("np6-905-cd", day, POSTED) == (
        "curated/np6-905-cd/date=2026-09-03/part-20260904T044007Z.parquet"
    )
    compacted = datetime(2026, 9, 10, 1, 0, tzinfo=UTC)
    assert c.merged_key("np6-905-cd", day, compacted) == (
        "curated/np6-905-cd/date=2026-09-03/merged-20260910T010000Z.parquet"
    )


def test_manifest_and_catalog_keys() -> None:
    assert c.manifest_key("np4-190-cd") == "manifests/np4-190-cd/latest.json"
    assert c.CATALOG_KEY == "manifests/_catalog.json"


@pytest.mark.parametrize("bad", ["NP6-905-CD", "np6/905", "../raw", "", "np6_905_cd"])
def test_product_must_be_a_lower_case_report_id(bad: str) -> None:
    with pytest.raises(ValueError, match="report ID"):
        c.manifest_key(bad)


def test_keys_refuse_non_utc_times() -> None:
    naive = datetime(2026, 9, 3, 12, 0)
    with pytest.raises(ValueError, match="aware"):
        c.raw_key("np6-905-cd", naive)


TS = pa.timestamp("us", tz="UTC")
COMMON = [
    ("interval_start", TS, False),
    ("interval_minutes", pa.int32(), False),
    ("posted_at", TS, False),
    ("ingested_at", TS, False),
    ("source", pa.string(), False),
    ("schema_version", pa.int32(), False),
]
EXPECTED = {
    "spp": [
        *COMMON,
        ("settlement_point", pa.string(), False),
        ("settlement_point_type", pa.string(), True),
        ("price_mwh", pa.float64(), False),
        ("dst_flag", pa.bool_(), False),
    ],
    "mcpc": [
        *COMMON,
        ("as_type", pa.string(), False),
        ("mcpc_mw", pa.float64(), False),
        ("dst_flag", pa.bool_(), False),
    ],
    "series": [
        *COMMON,
        ("series", pa.string(), False),
        ("value", pa.float64(), False),
        ("dst_flag", pa.bool_(), False),
    ],
}


@pytest.mark.parametrize("table", c.TABLES)
def test_schemas(table: c.Table) -> None:
    got = [(f.name, f.type, f.nullable) for f in c.SCHEMAS[table]]
    assert got == EXPECTED[table]


def test_business_keys() -> None:
    assert c.BUSINESS_KEY == {
        "spp": ("interval_start", "settlement_point", "settlement_point_type"),
        "mcpc": ("interval_start", "as_type"),
        "series": ("interval_start", "series"),
    }


def test_versions() -> None:
    assert c.SCHEMA_VERSION in c.SUPPORTED_SCHEMA_VERSIONS
    major, minor, patch = (int(x) for x in c.CONTRACT_VERSION.split("."))
    assert major >= 1
    assert minor >= 0
    assert patch >= 0
