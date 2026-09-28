"""A small fixture lake with reposts, written with the contract's own keys and schemas."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from ercot_lake.catalog import Catalog, CatalogProduct
from ercot_lake.contract import (
    CATALOG_KEY,
    CONTRACT_VERSION,
    SCHEMAS,
    Table,
    curated_key,
    curated_partition,
)
from ercot_lake.timeutil import delivery_date_ct


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)  # type: ignore[misc]


# Delivery day 2026-09-03 is CDT (UTC-5).
I1 = utc(2026, 9, 3, 20, 0)  # 15:00 CDT
I2 = utc(2026, 9, 3, 20, 15)
I_LATE = utc(2026, 9, 4, 4, 30)  # 23:30 CDT on Sep 3: belongs to the Sep 3 partition
P1 = utc(2026, 9, 3, 20, 20)  # first posting
P2 = utc(2026, 9, 3, 21, 5)  # a correction of I1
P_LATE = utc(2026, 9, 4, 4, 40)
DAM_HE1 = utc(2026, 9, 4, 5, 0)  # HE01 of delivery day Sep 4
DAM_P1 = utc(2026, 9, 3, 17, 30)
DAM_P2 = utc(2026, 9, 3, 18, 10)
DAY = date(2026, 9, 3)
POISONED_DAY = date(2026, 9, 5)


def write_posting(
    root: Path,
    product: str,
    table: Table,
    posted_at: datetime,
    rows: list[dict[str, Any]],
    *,
    interval_minutes: int,
    ingested_at: datetime | None = None,
    source: str = "api",
    schema_version: int = 1,
) -> list[Path]:
    """One ERCOT posting: one ``part-`` file per delivery day it touches, as the pipeline
    writes it."""
    by_day: dict[date, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        full = {
            "interval_minutes": interval_minutes,
            "posted_at": posted_at,
            "ingested_at": ingested_at or posted_at + timedelta(minutes=2),
            "source": source,
            "schema_version": schema_version,
            "dst_flag": False,
            **r,
        }
        by_day[delivery_date_ct(r["interval_start"])].append(full)
    written = []
    for day, day_rows in by_day.items():
        path = root / curated_key(product, day, posted_at)
        path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.Table.from_pylist(day_rows, schema=SCHEMAS[table]), path)
        written.append(path)
    return written


def catalog() -> Catalog:
    def p(name: str, table: Table, minutes: int, **kw: Any) -> CatalogProduct:
        return CatalogProduct(
            name=name, table=table, interval_minutes=minutes, schema_version=1, live=True, **kw
        )

    return Catalog(
        contract_version=CONTRACT_VERSION,
        generated_at=utc(2026, 9, 3, 0, 0),
        timezone="America/Chicago",
        products={
            "np6-905-cd": p("RT settlement point prices, 15-min", "spp", 15),
            "np4-190-cd": p("DAM settlement point prices", "spp", 60),
            "np6-331-cd": p("RT AS clearing prices, 15-min", "mcpc", 15),
            "np4-732-cd": p("Wind production", "series", 60, collected_from=utc(2026, 9, 1, 5, 0)),
        },
    )


@dataclass(frozen=True)
class FixtureLake:
    root: Path


def build_lake(root: Path) -> FixtureLake:
    rt = "np6-905-cd"
    write_posting(
        root,
        rt,
        "spp",
        P1,
        [
            {"interval_start": I1, "settlement_point": "HB_NORTH",
             "settlement_point_type": "HU", "price_mwh": 30.0},
            # A load zone appears twice per interval, energy-weighted or not, at different prices.
            {"interval_start": I1, "settlement_point": "LZ_HOUSTON",
             "settlement_point_type": "LZ", "price_mwh": 31.0},
            {"interval_start": I1, "settlement_point": "LZ_HOUSTON",
             "settlement_point_type": "LZEW", "price_mwh": 31.5},
            {"interval_start": I2, "settlement_point": "HB_NORTH",
             "settlement_point_type": "HU", "price_mwh": 32.0},
        ],
        interval_minutes=15,
    )  # fmt: skip
    write_posting(
        root,
        rt,
        "spp",
        P2,
        [{"interval_start": I1, "settlement_point": "HB_NORTH",
          "settlement_point_type": "HU", "price_mwh": 35.0}],
        interval_minutes=15,
    )  # fmt: skip
    write_posting(
        root,
        rt,
        "spp",
        P_LATE,
        [{"interval_start": I_LATE, "settlement_point": "HB_NORTH",
          "settlement_point_type": "HU", "price_mwh": 20.0}],
        interval_minutes=15,
    )  # fmt: skip
    # DAM carries no settlement point type: NULL is part of the business key.
    dam = "np4-190-cd"
    write_posting(
        root,
        dam,
        "spp",
        DAM_P1,
        [
            {"interval_start": DAM_HE1, "settlement_point": "HB_NORTH",
             "settlement_point_type": None, "price_mwh": 40.0},
            {"interval_start": DAM_HE1, "settlement_point": "HB_WEST",
             "settlement_point_type": None, "price_mwh": 38.0},
        ],
        interval_minutes=60,
    )  # fmt: skip
    write_posting(
        root,
        dam,
        "spp",
        DAM_P2,
        [{"interval_start": DAM_HE1, "settlement_point": "HB_NORTH",
          "settlement_point_type": None, "price_mwh": 41.0}],
        interval_minutes=60,
    )  # fmt: skip
    mcpc = "np6-331-cd"
    write_posting(
        root,
        mcpc,
        "mcpc",
        P1,
        [
            {"interval_start": I1, "as_type": "REGUP", "mcpc_mw": 5.0},
            {"interval_start": I1, "as_type": "RRS", "mcpc_mw": 3.0},
        ],
        interval_minutes=15,
    )
    write_posting(
        root,
        mcpc,
        "mcpc",
        P2,
        [{"interval_start": I1, "as_type": "REGUP", "mcpc_mw": 6.0}],
        interval_minutes=15,
    )
    wind = "np4-732-cd"
    write_posting(
        root,
        wind,
        "series",
        P1,
        [
            {"interval_start": I1, "series": "wind:STWPF_SYSTEM_WIDE", "value": 1000.0},
            {"interval_start": I1, "series": "wind:WGRPP_SYSTEM_WIDE", "value": 900.0},
        ],
        interval_minutes=60,
    )
    write_posting(
        root,
        wind,
        "series",
        P2,
        [{"interval_start": I1, "series": "wind:STWPF_SYSTEM_WIDE", "value": 1100.0}],
        interval_minutes=60,
    )
    # A corrupt file two days later. Reading it fails, so any query that touches it proves the
    # reader listed more than the requested days.
    bad = root / curated_partition(rt, POISONED_DAY) / "part-20260905T000000Z.parquet"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(b"not parquet")

    cat = root / CATALOG_KEY
    cat.parent.mkdir(parents=True, exist_ok=True)
    cat.write_text(catalog().to_json())
    return FixtureLake(root)


@pytest.fixture
def lake(tmp_path: Path) -> FixtureLake:
    return build_lake(tmp_path / "lake")
