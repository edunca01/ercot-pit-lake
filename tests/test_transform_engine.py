"""The declaration-driven transform engine, on small inline products. Each built-in product is
also tested against its committed ERCOT sample (test_transforms.py)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from ercot_lake.contract import SCHEMA_VERSION, SCHEMAS
from ingest.config import Product
from ingest.transforms import SchemaDriftError, TransformSpec, transform

POSTED = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
INGESTED = datetime(2026, 11, 1, 12, 5, tzinfo=UTC)


def product(table: str, minutes: int, decl: dict[str, Any], key: str = "np0-001-cd") -> Product:
    return Product.model_validate(
        {
            "key": key,
            "name": "test product",
            "archive_id": key.upper(),
            "endpoint": f"/{key}/x",
            "table": table,
            "cadence": {60: "hourly", 15: "15min", 5: "5min"}[minutes],
            "interval_minutes": minutes,
            "post_time_ct": "x",
            "schedule": "cron(* * * * ? *)",
            "date_params": ["a", "b"],
            "initial_lookback_hours": 1,
            "stale_after_min": 60,
            "enabled": True,
            "transform": decl,
        }
    )


RT_SPP = product(
    "spp",
    15,
    {
        "time": "hour_interval",
        "columns": {
            "delivery_date": ["deliveryDate", "DeliveryDate"],
            "delivery_hour": ["deliveryHour", "DeliveryHour"],
            "delivery_interval": ["deliveryInterval", "DeliveryInterval"],
            "settlement_point": ["settlementPoint", "SettlementPointName"],
            "settlement_point_type": ["settlementPointType", "SettlementPointType"],
            "price_mwh": ["settlementPointPrice", "SettlementPointPrice"],
            "dst_flag": ["DSTFlag", "DSTFlag"],
        },
    },
)
API_FIELDS = [
    "deliveryDate",
    "deliveryHour",
    "deliveryInterval",
    "settlementPoint",
    "settlementPointType",
    "settlementPointPrice",
    "DSTFlag",
]
# The fall-back day: hour 2 interval 1 first (CDT), then again (CST, flagged).
ROWS = [
    ["2026-11-01", 2, 1, "HB_NORTH", "HU", 21.5, "N"],
    ["2026-11-01", 2, 1, "HB_NORTH", "HU", 19.25, "Y"],
]
CSV = (
    "DeliveryDate,DeliveryHour,DeliveryInterval,SettlementPointName,SettlementPointType,"
    "SettlementPointPrice,DSTFlag\n"
    "11/01/2026,2,1,HB_NORTH,HU,21.5,N\n"
    "11/01/2026,2,1,HB_NORTH,HU,19.25,Y\n"
    "\n"  # blank lines are skipped
)


def api_body(fields: list[str], rows: list[list[Any]]) -> dict[str, Any]:
    return {"fields": [{"name": f} for f in fields], "data": rows}


def run(p: Product, source: str, payload: Any) -> list[dict[str, Any]]:
    t = transform(
        TransformSpec.for_product(p),
        source,  # type: ignore[arg-type]
        payload,
        posted_at=POSTED,
        ingested_at=INGESTED,
    )
    assert t.schema == SCHEMAS[p.table]
    rows: list[dict[str, Any]] = t.to_pylist()
    return rows


def test_api_and_archive_give_the_same_rows() -> None:
    api = run(RT_SPP, "api", api_body(API_FIELDS, ROWS))
    csv = run(RT_SPP, "archive", CSV)
    assert [{**r, "source": None} for r in api] == [{**r, "source": None} for r in csv]
    assert {r["source"] for r in api} == {"api"}
    assert {r["source"] for r in csv} == {"archive"}


def test_repeated_hour_flag_reaches_the_curated_row() -> None:
    first, second = run(RT_SPP, "archive", CSV)
    assert first["interval_start"] == datetime(2026, 11, 1, 6, 0, tzinfo=UTC)  # 01:00 CDT
    assert second["interval_start"] == datetime(2026, 11, 1, 7, 0, tzinfo=UTC)  # 01:00 CST
    assert (first["dst_flag"], second["dst_flag"]) == (False, True)


def test_common_columns() -> None:
    row = run(RT_SPP, "api", api_body(API_FIELDS, ROWS[:1]))[0]
    assert row["interval_minutes"] == 15
    assert (row["posted_at"], row["ingested_at"]) == (POSTED, INGESTED)
    assert row["schema_version"] == SCHEMA_VERSION


# -- drift fails loudly ------------------------------------------------------------------


def test_a_new_source_column_is_drift() -> None:
    with pytest.raises(SchemaDriftError, match=r"unexpected=\['newColumn'\]"):
        run(RT_SPP, "api", api_body([*API_FIELDS, "newColumn"], [[*ROWS[0], 1]]))


def test_a_missing_source_column_is_drift() -> None:
    with pytest.raises(SchemaDriftError, match=r"missing=\['DSTFlag'\]"):
        run(RT_SPP, "api", api_body(API_FIELDS[:-1], [ROWS[0][:-1]]))


def test_a_short_row_is_drift() -> None:
    with pytest.raises(SchemaDriftError, match="row has 6 cells"):
        run(RT_SPP, "api", api_body(API_FIELDS, [ROWS[0][:-1]]))


def test_an_unknown_flag_value_is_drift() -> None:
    with pytest.raises(SchemaDriftError, match="unrecognised flag value"):
        run(RT_SPP, "api", api_body(API_FIELDS, [[*ROWS[0][:-1], "maybe"]]))


def test_payload_type_must_match_the_source() -> None:
    spec = TransformSpec.for_product(RT_SPP)
    with pytest.raises(TypeError, match="parsed JSON"):
        transform(spec, "api", CSV, posted_at=POSTED, ingested_at=INGESTED)
    with pytest.raises(TypeError, match="CSV text"):
        transform(spec, "archive", {}, posted_at=POSTED, ingested_at=INGESTED)


def test_a_product_without_a_declaration() -> None:
    bare = RT_SPP.model_copy(update={"transform": None})
    with pytest.raises(LookupError, match="no transform declaration"):
        TransformSpec.for_product(bare)


# -- table families ----------------------------------------------------------------------


def test_dam_spp_has_no_settlement_point_type() -> None:
    dam = product(
        "spp",
        60,
        {
            "time": "hour_ending",
            "columns": {
                "delivery_date": ["deliveryDate", "DeliveryDate"],
                "hour_ending": ["hourEnding", "HourEnding"],
                "settlement_point": ["settlementPoint", "SettlementPoint"],
                "price_mwh": ["settlementPointPrice", "SettlementPointPrice"],
                "dst_flag": ["DSTFlag", "DSTFlag"],
            },
        },
    )
    rows = run(dam, "archive", "DeliveryDate,HourEnding,SettlementPoint,SettlementPointPrice,"
               "DSTFlag\n09/03/2026,01:00,HB_NORTH,30.5,N\n")  # fmt: skip
    assert rows[0]["settlement_point_type"] is None
    assert rows[0]["interval_start"] == datetime(2026, 9, 3, 5, 0, tzinfo=UTC)


def test_sced_spp_keeps_only_declared_point_prefixes() -> None:
    sced = product(
        "spp",
        5,
        {
            "time": "sced_timestamp",
            "columns": {
                "sced_timestamp": ["SCEDTimestamp", "SCEDTimestamp"],
                "dst_flag": ["repeatHourFlag", "RepeatedHourFlag"],
                "settlement_point": ["settlementPoint", "SettlementPoint"],
                "price_mwh": ["LMP", "LMP"],
            },
            "keep_point_prefixes": ["HB_", "LZ_"],
        },
    )
    csv = (
        "SCEDTimestamp,RepeatedHourFlag,SettlementPoint,LMP\n"
        "09/16/2026 15:05:19,N,HB_NORTH,25.1\n"
        "09/16/2026 15:05:19,N,SOME_RESOURCE_NODE,24.0\n"
        "09/16/2026 15:05:19,N,LZ_WEST,26.2\n"
    )
    rows = run(sced, "archive", csv)
    assert [r["settlement_point"] for r in rows] == ["HB_NORTH", "LZ_WEST"]
    assert rows[0]["interval_start"] == datetime(2026, 9, 16, 20, 5, tzinfo=UTC)


def test_mcpc_normalises_and_checks_as_types() -> None:
    mcpc = product(
        "mcpc",
        15,
        {
            "time": "hour_interval",
            "columns": {
                "delivery_date": [None, "DeliveryDate"],
                "delivery_hour": [None, "DeliveryHour"],
                "delivery_interval": [None, "DeliveryInterval"],
                "dst_flag": [None, "RepeatedHourFlag"],
                "as_type": [None, "ASType"],
                "mcpc_mw": [None, "MCPC"],
            },
        },
    )
    head = "DeliveryDate,DeliveryHour,DeliveryInterval,RepeatedHourFlag,ASType,MCPC\n"
    rows = run(mcpc, "archive", head + "09/03/2026,15,1,N, regup ,5.5\n")
    assert (rows[0]["as_type"], rows[0]["mcpc_mw"]) == ("REGUP", 5.5)
    with pytest.raises(SchemaDriftError, match="unknown AS type 'NEWAS'"):
        run(mcpc, "archive", head + "09/03/2026,15,1,N,NEWAS,1\n")


def test_series_melts_keeps_the_in_use_model_and_skips_empty_cells() -> None:
    load = product(
        "series",
        60,
        {
            "time": "hour_ending",
            "columns": {
                "delivery_date": ["deliveryDate", "DeliveryDate"],
                "hour_ending": ["hourEnding", "HourEnding"],
                "coast": ["coast", "Coast"],
                "system_total": ["systemTotal", "SystemTotal"],
                "model": ["model", "Model"],
                "in_use": ["inUseFlag", "InUseFlag"],
                "dst_flag": ["DSTFlag", "DSTFlag"],
                "posted_datetime": ["postedDatetime", None],  # API only, not stored
            },
            "series": {"coast": "load_fcst:Coast", "system_total": "load_fcst:SystemTotal"},
            "keep_flag": "in_use",
        },
    )
    csv = (
        "DeliveryDate,HourEnding,Coast,SystemTotal,Model,InUseFlag,DSTFlag\n"
        "09/03/2026,01:00,12000.5,50000,A,Y,N\n"
        "09/03/2026,01:00,99999,99999,B,N,N\n"  # a model not in use
        "09/03/2026,02:00,,51000,A,Y,N\n"  # coast not published this hour
    )
    rows = run(load, "archive", csv)
    assert [(r["interval_start"].hour, r["series"], r["value"]) for r in rows] == [
        (5, "load_fcst:Coast", 12000.5),
        (5, "load_fcst:SystemTotal", 50000.0),
        (6, "load_fcst:SystemTotal", 51000.0),
    ]
