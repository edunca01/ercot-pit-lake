"""Point-in-time reads over the lake with DuckDB.

Every query answers "what was known at ``as_of``": rows posted after ``as_of`` are invisible,
and of the postings that remain the latest one wins per business key. There is deliberately
no method without ``as_of``, because a read that ignores it quietly uses the future.
"""

from __future__ import annotations

import re
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any, Final, cast

import duckdb
import pyarrow as pa
import pyarrow.compute as pc

from ercot_lake.catalog import Catalog
from ercot_lake.contract import (
    BUSINESS_KEY,
    CATALOG_KEY,
    SCHEMAS,
    SUPPORTED_SCHEMA_VERSIONS,
    Table,
    curated_partition,
)
from ercot_lake.errors import CatalogNotFoundError, UnsupportedSchemaVersionError
from ercot_lake.timeutil import delivery_date_ct

COLUMNS: Final[dict[Table, tuple[str, ...]]] = {
    "spp": (
        "interval_start",
        "interval_minutes",
        "settlement_point",
        "settlement_point_type",
        "price_mwh",
        "dst_flag",
        "posted_at",
    ),
    "mcpc": ("interval_start", "interval_minutes", "as_type", "mcpc_mw", "dst_flag", "posted_at"),
    "series": ("interval_start", "interval_minutes", "series", "value", "dst_flag", "posted_at"),
}
# Postings keep every version, so they also carry the second clock: a replay of "what this
# system had at T" filters ingested_at <= T on top of posted_at <= as_of.
POSTING_COLUMNS: Final = ("ingested_at", "source")

# The column the optional name filter applies to, per table.
_FILTER_COLUMN: Final[dict[Table, str]] = {
    "spp": "settlement_point",
    "mcpc": "as_type",
    "series": "series",
}

_REGION = re.compile(r"^[a-z]{2}(?:-[a-z]+)+-\d+$")


class LakeReader:
    """Read one lake, local (``./data``) or on S3 (``s3://bucket``).

    S3 credentials come from the standard AWS chain (env, profile, SSO, instance role) through
    DuckDB's ``aws`` extension. One DuckDB connection per reader; calls are serialized with a
    lock because a DuckDB connection is not safe to share across threads.
    """

    # Longest delivery range one query may span. Each day is one listing plus its files, so
    # this bounds the cost of a single call on S3; loop over windows for longer backtests.
    max_days: int = 62

    def __init__(
        self,
        root: str | Path,
        *,
        region: str | None = None,
        threads: int | None = None,
        catalog: Catalog | None = None,
    ) -> None:
        text = str(root)
        self.is_s3 = text.startswith("s3://")
        self.root = text.rstrip("/") if self.is_s3 else Path(text).expanduser().resolve().as_posix()
        if region is not None and not _REGION.fullmatch(region):
            msg = f"not an AWS region: {region!r}"
            raise ValueError(msg)
        self._region = region
        self._threads = threads
        self._catalog = catalog
        self._lock = threading.Lock()
        self._con = self._connect()

    # -- connection ------------------------------------------------------------------------

    def _connect(self) -> duckdb.DuckDBPyConnection:
        con = duckdb.connect()
        con.execute("SET TimeZone='UTC';")
        if self._threads is not None:
            # Reads are many small objects, I/O-bound: more threads than cores can pay off.
            con.execute(f"SET threads={int(self._threads)};")
        if self.is_s3:
            for sql in s3_setup_sql(self._region):
                con.execute(sql)
        return con

    def reconnect(self) -> None:
        """A fresh connection, and so freshly resolved credentials."""
        with self._lock:
            old, self._con = self._con, self._connect()
            old.close()

    def close(self) -> None:
        with self._lock:
            self._con.close()

    def __enter__(self) -> LakeReader:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # -- catalog ---------------------------------------------------------------------------

    @property
    def catalog(self) -> Catalog:
        """The lake's catalog, read once per reader."""
        if self._catalog is None:
            uri = f"{self.root}/{CATALOG_KEY}"
            missing = f"no catalog at {uri}; is {self.root} an ercot-pit-lake root?"
            try:
                with self._lock:
                    row = self._con.execute("SELECT content FROM read_text(?)", [uri]).fetchone()
            except (duckdb.IOException, duckdb.HTTPException) as e:  # S3: 403/404
                raise CatalogNotFoundError(missing) from e
            if row is None:  # a local path that does not exist matches no file
                raise CatalogNotFoundError(missing)
            self._catalog = Catalog.parse(cast(str, row[0]))
        return self._catalog

    def products(self) -> list[str]:
        return sorted(self.catalog.products)

    # -- by delivery date and by interval range ------------------------------------------

    def spp_by_date(
        self, product: str, day: date, *, as_of: datetime, points: list[str] | None = None
    ) -> pa.Table:
        """Settlement point prices for one CT delivery day, as known at ``as_of``."""
        return self._point_in_time(product, "spp", day, day, as_of=as_of, only=points)

    def spp_by_range(
        self,
        product: str,
        start: datetime,
        end: datetime,
        *,
        as_of: datetime,
        points: list[str] | None = None,
    ) -> pa.Table:
        """Settlement point prices for intervals in ``[start, end)``, as known at ``as_of``."""
        return self._range(product, "spp", start, end, as_of=as_of, only=points)

    def mcpc_by_date(
        self, product: str, day: date, *, as_of: datetime, as_types: list[str] | None = None
    ) -> pa.Table:
        return self._point_in_time(product, "mcpc", day, day, as_of=as_of, only=as_types)

    def mcpc_by_range(
        self,
        product: str,
        start: datetime,
        end: datetime,
        *,
        as_of: datetime,
        as_types: list[str] | None = None,
    ) -> pa.Table:
        return self._range(product, "mcpc", start, end, as_of=as_of, only=as_types)

    def series_by_date(
        self, product: str, day: date, *, as_of: datetime, names: list[str] | None = None
    ) -> pa.Table:
        return self._point_in_time(product, "series", day, day, as_of=as_of, only=names)

    def series_by_range(
        self,
        product: str,
        start: datetime,
        end: datetime,
        *,
        as_of: datetime,
        names: list[str] | None = None,
    ) -> pa.Table:
        return self._range(product, "series", start, end, as_of=as_of, only=names)

    # -- every version ---------------------------------------------------------------------

    def postings(
        self,
        product: str,
        start: datetime,
        end: datetime,
        *,
        as_of: datetime,
        only: list[str] | None = None,
    ) -> pa.Table:
        """Every posting of every row for intervals in ``[start, end)`` published by ``as_of``,
        oldest first and not deduplicated, with ``ingested_at`` and ``source``. For replay
        clocks that need to see revisions as they happened. ``only`` filters on the table's
        name column (settlement point, AS type or series)."""
        table = self.catalog.product(product).table
        self.catalog.require(product, table)
        _require_aware(as_of=as_of, start=start, end=end)
        d_from, d_to = self._day_range(start, end)
        columns = (*COLUMNS[table], *POSTING_COLUMNS)
        files = self._files(product, d_from, d_to)
        if not files:
            return _empty(table, columns)
        where, params = _only_filter(table, only)
        order = ", ".join((*BUSINESS_KEY[table], "posted_at"))
        sql = f"""
            SELECT {", ".join(columns)}, schema_version
            FROM read_parquet(?, union_by_name=true)
            WHERE posted_at <= ? AND interval_start >= ? AND interval_start < ? {where}
            ORDER BY {order}
        """  # noqa: S608  (identifiers come from the contract; values are bound)
        return self._run(sql, [files, as_of, start, end, *params])

    # -- the one query builder -------------------------------------------------------------

    def _range(  # noqa: PLR0913  (keyword-only past the range)
        self,
        product: str,
        table: Table,
        start: datetime,
        end: datetime,
        *,
        as_of: datetime,
        only: list[str] | None,
    ) -> pa.Table:
        _require_aware(start=start, end=end)
        d_from, d_to = self._day_range(start, end)
        return self._point_in_time(
            product, table, d_from, d_to, as_of=as_of, only=only, interval=(start, end)
        )

    def _point_in_time(  # noqa: PLR0913  (one private builder; keyword-only past the days)
        self,
        product: str,
        table: Table,
        d_from: date,
        d_to: date,
        *,
        as_of: datetime,
        only: list[str] | None,
        interval: tuple[datetime, datetime] | None = None,
    ) -> pa.Table:
        """Keep rows with ``posted_at <= as_of``, then the latest posting per business key."""
        self.catalog.require(product, table)
        _require_aware(as_of=as_of)
        self._check_days(d_from, d_to)
        columns = COLUMNS[table]
        files = self._files(product, d_from, d_to)
        if not files:
            return _empty(table, columns)
        where, params = _only_filter(table, only)
        if interval is not None:
            where = f"AND interval_start >= ? AND interval_start < ? {where}"
            params = [*interval, *params]
        key = ", ".join(BUSINESS_KEY[table])
        sql = f"""
            SELECT {", ".join(columns)}, schema_version
            FROM read_parquet(?, union_by_name=true)
            WHERE posted_at <= ? {where}
            QUALIFY row_number() OVER (
                PARTITION BY {key} ORDER BY posted_at DESC, ingested_at DESC
            ) = 1
            ORDER BY {key}
        """  # noqa: S608  (identifiers come from the contract; values are bound)
        return self._run(sql, [files, as_of, *params])

    # -- helpers ---------------------------------------------------------------------------

    def _day_range(self, start: datetime, end: datetime) -> tuple[date, date]:
        if end <= start:
            msg = f"empty interval range {start} .. {end}"
            raise ValueError(msg)
        # end is exclusive: an interval range ending at midnight CT needs no extra day
        d_from, d_to = delivery_date_ct(start), delivery_date_ct(end - timedelta(microseconds=1))
        self._check_days(d_from, d_to)
        return d_from, d_to

    def _check_days(self, d_from: date, d_to: date) -> None:
        if d_to < d_from or (d_to - d_from).days >= self.max_days:
            msg = f"delivery days {d_from}..{d_to} invalid or longer than {self.max_days} days"
            raise ValueError(msg)

    def _files(self, product: str, d_from: date, d_to: date) -> list[str]:
        """Parquet files of the requested delivery days, one listing per day. A ``date=*`` glob
        would make DuckDB list every object of the product on S3 on every query."""
        out: list[str] = []
        with self._lock:
            for i in range((d_to - d_from).days + 1):
                partition = curated_partition(product, d_from + timedelta(days=i))
                pattern = f"{self.root}/{partition}*.parquet"
                rows = self._con.execute("SELECT file FROM glob(?)", [pattern]).fetchall()
                out.extend(sorted(cast(str, r[0]) for r in rows))
        return out

    def _run(self, sql: str, params: list[Any]) -> pa.Table:
        # Through Arrow, not fetchall(): timestamps come back as aware UTC without pytz.
        with self._lock:
            result = self._con.execute(sql, params).to_arrow_table()
        versions = set(cast(list[int], pc.unique(result.column("schema_version")).to_pylist()))
        unknown = versions - SUPPORTED_SCHEMA_VERSIONS
        if unknown:
            msg = f"rows at schema_version {sorted(unknown)}; upgrade ercot-lake"
            raise UnsupportedSchemaVersionError(msg)
        return result.drop_columns(["schema_version"])


def s3_setup_sql(region: str | None) -> list[str]:
    """DuckDB statements that make ``s3://`` readable with the standard AWS credential chain.
    ``REFRESH auto`` re-resolves credentials when S3 rejects a request, so a long-lived reader
    outlives a rotated SSO or role session."""
    region_opt = f", REGION '{region}'" if region else ""
    return [
        "INSTALL httpfs; LOAD httpfs;",
        "INSTALL aws; LOAD aws;",
        "CREATE SECRET IF NOT EXISTS ercot_lake "
        f"(TYPE s3, PROVIDER credential_chain, REFRESH auto{region_opt});",
    ]


def _require_aware(**stamps: datetime) -> None:
    for name, ts in stamps.items():
        if ts.tzinfo is None or ts.utcoffset() is None:
            msg = f"{name} must be timezone-aware (UTC recommended), got {ts!r}"
            raise ValueError(msg)


def _only_filter(table: Table, only: list[str] | None) -> tuple[str, list[Any]]:
    if not only:
        return "", []
    marks = ", ".join("?" for _ in only)
    return f"AND {_FILTER_COLUMN[table]} IN ({marks})", list(only)


def _empty(table: Table, columns: tuple[str, ...]) -> pa.Table:
    return SCHEMAS[table].empty_table().select(list(columns))
