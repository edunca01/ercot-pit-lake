"""The lake contract as code: layout, key formats, curated schemas and business keys.

The pipeline builds every key with these functions and the reader dedupes on these business
keys, so writer and reader cannot drift apart. Any change here is a contract change: bump
``SCHEMA_VERSION`` when a table's columns change, and ``CONTRACT_VERSION`` (SemVer) always.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Final, Literal

import pyarrow as pa

from ercot_lake.timeutil import delivery_date_ct, stamp

CONTRACT_VERSION: Final = "1.0.0"

# Stored on every curated row. Readers accept the versions they know how to read and refuse
# anything newer rather than guess at its columns.
SCHEMA_VERSION: Final = 1
SUPPORTED_SCHEMA_VERSIONS: Final = frozenset({1})

TIMEZONE: Final = "America/Chicago"  # curated `date=` partitions are CT delivery dates

RAW_PREFIX: Final = "raw"
CURATED_PREFIX: Final = "curated"
MANIFESTS_PREFIX: Final = "manifests"
CATALOG_KEY: Final = f"{MANIFESTS_PREFIX}/_catalog.json"

Table = Literal["spp", "mcpc", "series"]
TABLES: Final[tuple[Table, ...]] = ("spp", "mcpc", "series")

SOURCES: Final = frozenset({"api", "archive"})
AS_TYPES: Final = frozenset({"REGUP", "REGDN", "RRS", "ECRS", "NSPIN"})

_TS = pa.timestamp("us", tz="UTC")

COMMON_FIELDS: Final[list[pa.Field[pa.DataType]]] = [
    pa.field("interval_start", _TS, nullable=False),
    pa.field("interval_minutes", pa.int32(), nullable=False),
    pa.field("posted_at", _TS, nullable=False),
    pa.field("ingested_at", _TS, nullable=False),
    pa.field("source", pa.string(), nullable=False),
    pa.field("schema_version", pa.int32(), nullable=False),
]

SPP_SCHEMA: Final = pa.schema(
    [
        *COMMON_FIELDS,
        pa.field("settlement_point", pa.string(), nullable=False),
        # ERCOT's DAM and SCED reports carry no settlement point type, so it is NULL there;
        # deriving it from the name was rejected because some RT types (PUN, LCCRN, PCCRN)
        # cannot be recovered from a prefix.
        pa.field("settlement_point_type", pa.string(), nullable=True),
        pa.field("price_mwh", pa.float64(), nullable=False),
        pa.field("dst_flag", pa.bool_(), nullable=False),
    ]
)

MCPC_SCHEMA: Final = pa.schema(
    [
        *COMMON_FIELDS,
        pa.field("as_type", pa.string(), nullable=False),
        pa.field("mcpc_mw", pa.float64(), nullable=False),
        pa.field("dst_flag", pa.bool_(), nullable=False),
    ]
)

# Every numeric column of an ERCOT system report (load, wind, solar, system lambda), melted to
# one row per (interval, series). ``series`` is ``<report>:<column>``, e.g.
# ``load_fcst:SystemTotal``, so a new source column is a schema bump, never a silent extra
# series.
SERIES_SCHEMA: Final = pa.schema(
    [
        *COMMON_FIELDS,
        pa.field("series", pa.string(), nullable=False),
        pa.field("value", pa.float64(), nullable=False),
        pa.field("dst_flag", pa.bool_(), nullable=False),
    ]
)

SCHEMAS: Final[dict[Table, pa.Schema]] = {
    "spp": SPP_SCHEMA,
    "mcpc": MCPC_SCHEMA,
    "series": SERIES_SCHEMA,
}

# Readers keep, per business key, the row with the greatest posted_at <= as_of. Load zones
# appear twice per RT interval (types LZ and LZEW, at different prices), so the settlement
# point type is part of the SPP key.
BUSINESS_KEY: Final[dict[Table, tuple[str, ...]]] = {
    "spp": ("interval_start", "settlement_point", "settlement_point_type"),
    "mcpc": ("interval_start", "as_type"),
    "series": ("interval_start", "series"),
}

# Lower-case ERCOT report IDs (np6-905-cd). Checked because the key becomes a path segment.
_PRODUCT = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _product(product: str) -> str:
    if not _PRODUCT.fullmatch(product):
        msg = f"not a lower-case ERCOT report ID: {product!r}"
        raise ValueError(msg)
    return product


def raw_key(product: str, posted_at: datetime) -> str:
    """Raw is partitioned by *posting* date: it lands before parsing, so the delivery date is
    not known yet."""
    return (
        f"{RAW_PREFIX}/{_product(product)}/date={delivery_date_ct(posted_at)}"
        f"/posted={stamp(posted_at)}.zip"
    )


def curated_partition(product: str, delivery_date: date) -> str:
    """The prefix of one curated partition, with a trailing slash. Readers list one of these
    per delivery day instead of globbing ``date=*``, which on S3 lists the whole product."""
    return f"{CURATED_PREFIX}/{_product(product)}/date={delivery_date.isoformat()}/"


def curated_key(product: str, delivery_date: date, posted_at: datetime) -> str:
    """One posting's rows for one delivery day. Same posting, same key: re-runs overwrite."""
    return f"{curated_partition(product, delivery_date)}part-{stamp(posted_at)}.parquet"


def merged_key(product: str, delivery_date: date, compacted_at: datetime) -> str:
    """Compaction output: replaces a partition's ``part-`` files without changing a row."""
    return f"{curated_partition(product, delivery_date)}merged-{stamp(compacted_at)}.parquet"


def manifest_key(product: str) -> str:
    return f"{MANIFESTS_PREFIX}/{_product(product)}/latest.json"
