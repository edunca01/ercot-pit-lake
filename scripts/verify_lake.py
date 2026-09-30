"""Check a lake, local or on S3, against the contract with DuckDB. Read-only.

    uv run python -m scripts.verify_lake [--root ./data] [--product KEY]

Checks, per configured product that has curated data:

- files: every curated file has exactly the contract's columns and types, and rows at a
  schema version this code can read;
- keys: no business key appears twice within one posting (one ``posted_at``);
- calendar: every delivery day from the first to the last partition is present (a gap is a
  missing day of data), counting only from ``collected_from`` where it is set;
- load zones: where a report publishes weather zones and a system total, the zones add up to
  the total for every interval and posting.

Exits non-zero when any check fails, so it can gate a deploy or a backfill.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import duckdb
import pyarrow as pa

from ercot_lake.contract import (
    BUSINESS_KEY,
    CURATED_PREFIX,
    SCHEMAS,
    SUPPORTED_SCHEMA_VERSIONS,
    Table,
)
from ercot_lake.reader import s3_setup_sql
from ercot_lake.timeutil import delivery_date_ct
from ingest.config import Product, settings

# pyarrow type -> DuckDB's name for it, as DESCRIBE prints
_DUCK_TYPES = {
    "timestamp[us, tz=UTC]": "TIMESTAMP WITH TIME ZONE",
    "int32": "INTEGER",
    "string": "VARCHAR",
    "double": "DOUBLE",
    "bool": "BOOLEAN",
}
TOTAL = "SystemTotal"
# DuckDB would add a `date` column from the `date=` folder; the files themselves are checked.
_READ = "read_parquet(?, hive_partitioning = false)"
ZONE_TOLERANCE_MW = 1.0  # ERCOT rounds each zone to 0.01 MW; eight roundings stay far below this


@dataclass(frozen=True)
class Finding:
    product: str
    check: str
    detail: str

    def __str__(self) -> str:
        return f"FAIL {self.product} {self.check}: {self.detail}"


def connect(root: str) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("SET TimeZone='UTC';")
    if root.startswith("s3://"):
        for sql in s3_setup_sql(None):
            con.execute(sql)
    return con


def expected_columns(table: Table) -> list[tuple[str, str]]:
    schema: pa.Schema = SCHEMAS[table]
    return [(f.name, _DUCK_TYPES[str(f.type)]) for f in schema]


def product_files(con: duckdb.DuckDBPyConnection, root: str, key: str) -> list[str]:
    pattern = f"{root.rstrip('/')}/{CURATED_PREFIX}/{key}/date=*/*.parquet"
    return sorted(str(r[0]) for r in con.execute("SELECT file FROM glob(?)", [pattern]).fetchall())


def check_files(con: duckdb.DuckDBPyConnection, p: Product, files: list[str]) -> list[Finding]:
    out = []
    want = expected_columns(p.table)
    describe = f"DESCRIBE SELECT * FROM {_READ}"  # noqa: S608  (a constant; the path is bound)
    versions_sql = f"SELECT DISTINCT schema_version FROM {_READ}"  # noqa: S608  (likewise)
    # DESCRIBE does not show nullability, so required columns are checked for actual nulls
    required = [f.name for f in SCHEMAS[p.table] if not f.nullable]
    nulls_sql = f"SELECT count(*) FILTER (WHERE {{col}} IS NULL) FROM {_READ}"  # noqa: S608
    for f in files:
        got = [(str(r[0]), str(r[1])) for r in con.execute(describe, [f]).fetchall()]
        if got != want:
            diff = sorted(set(got) ^ set(want)) or "column order"
            out.append(Finding(p.key, "files", f"{f}: differs from the contract: {diff}"))
            continue
        nulls = [
            c for c in required if con.execute(nulls_sql.format(col=c), [f]).fetchone() != (0,)
        ]
        if nulls:
            out.append(Finding(p.key, "files", f"{f}: nulls in required columns {nulls}"))
        versions = {int(r[0]) for r in con.execute(versions_sql, [f]).fetchall()}
        if not versions <= SUPPORTED_SCHEMA_VERSIONS:
            out.append(Finding(p.key, "files", f"{f}: schema_version {sorted(versions)}"))
    return out


def check_keys(con: duckdb.DuckDBPyConnection, p: Product, files: list[str]) -> list[Finding]:
    key = ", ".join((*BUSINESS_KEY[p.table], "posted_at"))
    rows = con.execute(
        f"SELECT {key}, count(*) AS n FROM {_READ} GROUP BY ALL HAVING n > 1 LIMIT 5",  # noqa: S608  (identifiers from the contract)
        [files],
    )
    return [
        Finding(p.key, "keys", f"repeated within one posting: {_show(r, 'n')} x{r['n']}")
        for r in _rows(rows)
    ]


def check_calendar(p: Product, files: list[str]) -> list[Finding]:
    days = sorted({date.fromisoformat(f.split("/date=")[1][:10]) for f in files})
    if p.collected_from is not None:
        first = delivery_date_ct(p.collected_from)
        days = [d for d in days if d >= first]
    if not days:
        return []
    present = set(days)
    span = (days[-1] - days[0]).days
    missing = [d for i in range(span + 1) if (d := days[0] + timedelta(days=i)) not in present]
    if not missing:
        return []
    shown = ", ".join(str(d) for d in missing[:5]) + (" ..." if len(missing) > 5 else "")
    return [Finding(p.key, "calendar", f"{len(missing)} missing delivery days: {shown}")]


def check_zone_sums(con: duckdb.DuckDBPyConnection, p: Product, files: list[str]) -> list[Finding]:
    names = list((p.transform.series or {}).values())
    totals = [n for n in names if n.endswith(f":{TOTAL}")]
    if p.table != "series" or not totals:
        return []
    zones = [n for n in names if n not in totals]
    rows = con.execute(
        f"""
        SELECT interval_start, posted_at,
               sum(value) FILTER (WHERE series IN (SELECT unnest(?))) AS zones,
               max(value) FILTER (WHERE series = ?) AS total,
               count(*) FILTER (WHERE series IN (SELECT unnest(?))) AS n
        FROM {_READ}
        GROUP BY interval_start, posted_at
        HAVING total IS NOT NULL AND (n <> ? OR abs(zones - total) > ?)
        ORDER BY interval_start LIMIT 5
        """,  # noqa: S608  (the read is a constant; values are bound)
        [zones, totals[0], zones, files, len(zones), ZONE_TOLERANCE_MW],
    )
    return [
        Finding(
            p.key,
            "zones",
            f"{r['interval_start']:%Y-%m-%dT%H:%MZ} posted {r['posted_at']:%Y-%m-%dT%H:%M:%SZ}: "
            f"{r['n']} zones sum {r['zones']} vs total {r['total']}",
        )
        for r in _rows(rows)
    ]


def _rows(result: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    # Through Arrow, not fetchall(): timestamps come back as aware UTC without pytz.
    rows: list[dict[str, Any]] = result.to_arrow_table().to_pylist()
    return rows


def _show(row: dict[str, Any], skip: str) -> str:
    return ", ".join(
        f"{k}={v:%Y-%m-%dT%H:%MZ}" if hasattr(v, "tzinfo") else f"{k}={v}"
        for k, v in row.items()
        if k != skip
    )


def verify(root: str, products: list[Product]) -> tuple[list[Finding], dict[str, int]]:
    """All findings, and the number of curated files checked per product."""
    con = connect(root)
    findings: list[Finding] = []
    counted: dict[str, int] = {}
    for p in products:
        files = product_files(con, root, p.key)
        counted[p.key] = len(files)
        if not files:
            continue
        findings += check_files(con, p, files)
        findings += check_keys(con, p, files)
        findings += check_calendar(p, files)
        findings += check_zone_sums(con, p, files)
    return findings, counted


def main(argv: list[str] | None = None) -> int:
    cfg = settings()
    ap = argparse.ArgumentParser(prog="verify_lake", description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", default=cfg.lake.root, help="lake root (default: LAKE_ROOT)")
    ap.add_argument("--product", default="all", help="product key from config.yaml, or 'all'")
    args = ap.parse_args(argv)
    products = list(cfg.products.values()) if args.product == "all" else [cfg.product(args.product)]
    root = args.root if args.root.startswith("s3://") else str(args.root)
    findings, counted = verify(root, products)
    for key, n in counted.items():
        print(f"{key}: {n} curated files")
    for f in findings:
        print(f)
    print(f"{len(findings)} problems" if findings else "OK: no problems")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
