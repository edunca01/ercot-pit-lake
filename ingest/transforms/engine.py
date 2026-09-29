"""Table-driven transform: (API JSON | archive CSV) -> curated pyarrow Table.

Each product declares, in ``config.yaml``, exactly the source columns it expects in each format
and the canonical name for each. Any deviation raises :class:`SchemaDriftError`; nothing is
coerced silently, because a quietly re-mapped column would corrupt history without anyone
noticing.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

import pyarrow as pa

from ercot_lake.contract import AS_TYPES, SCHEMA_VERSION, SCHEMAS, Table
from ingest.config import Product, TimeStyle
from ingest.timeutil import (
    interval_start_utc,
    parse_delivery_date,
    parse_hour_ending,
    sced_interval_start_utc,
)

Source = Literal["api", "archive"]


class SchemaDriftError(ValueError):
    """Source columns or values differ from what the product's declaration expects."""


@dataclass(frozen=True)
class TransformSpec:
    product: str
    table: Table
    interval_minutes: int
    time: TimeStyle
    api_columns: Mapping[str, str]  # source field name -> canonical name
    csv_columns: Mapping[str, str]
    # series tables: canonical column -> series name; each record becomes one row per series
    # whose value is present (an empty cell is "not published", not a row)
    series: Mapping[str, str] | None = None
    keep_flag: str | None = None
    keep_point_prefixes: tuple[str, ...] | None = None

    @classmethod
    def for_product(cls, product: Product) -> TransformSpec:
        decl = product.transform
        if decl is None:
            msg = f"{product.key} has no transform declaration"
            raise LookupError(msg)
        return cls(
            product=product.key,
            table=product.table,
            interval_minutes=product.interval_minutes,
            time=decl.time,
            api_columns={api: c for c, (api, _) in decl.columns.items() if api is not None},
            csv_columns={csv: c for c, (_, csv) in decl.columns.items() if csv is not None},
            series=decl.series,
            keep_flag=decl.keep_flag,
            keep_point_prefixes=decl.keep_point_prefixes,
        )

    def columns_for(self, source: Source) -> Mapping[str, str]:
        return self.api_columns if source == "api" else self.csv_columns


# -- source readers ----------------------------------------------------------------------


def read_api_body(body: Mapping[str, Any]) -> tuple[list[str], list[Sequence[Any]]]:
    fields = [f["name"] for f in body.get("fields", [])]
    return fields, list(body.get("data", []))


def read_csv_text(text: str) -> tuple[list[str], list[Sequence[Any]]]:
    reader = csv.reader(io.StringIO(text))
    header = next(reader, [])
    rows: list[Sequence[Any]] = [r for r in reader if any(cell.strip() for cell in r)]
    return [h.strip() for h in header], rows


# -- normalisation ------------------------------------------------------------------------


def _to_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    s = str(v).strip().upper()
    if s in {"Y", "TRUE", "1"}:
        return True
    if s in {"N", "FALSE", "0", ""}:
        return False
    msg = f"unrecognised flag value {v!r}"
    raise SchemaDriftError(msg)


def _to_float(v: Any) -> float:
    if isinstance(v, int | float) and not isinstance(v, bool):
        return float(v)
    return float(str(v).strip())


def _to_int(v: Any) -> int:
    return int(str(v).strip())


def _to_optional_float(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, int | float) and not isinstance(v, bool):
        return float(v)
    text = str(v).strip()
    return float(text) if text else None


def _interval_start(spec: TransformSpec, rec: Mapping[str, Any], dst: bool) -> datetime:
    if spec.time == "sced_timestamp":
        return sced_interval_start_utc(
            str(rec["sced_timestamp"]), spec.interval_minutes, repeated_hour=dst
        )
    ddate = parse_delivery_date(rec["delivery_date"])
    if spec.time == "hour_ending":
        hour, interval = parse_hour_ending(rec["hour_ending"]), 1
    else:
        hour, interval = _to_int(rec["delivery_hour"]), _to_int(rec["delivery_interval"])
    return interval_start_utc(ddate, hour, interval, spec.interval_minutes, repeated_hour=dst)


def _check_columns(spec: TransformSpec, source: Source, header: Sequence[str]) -> list[str]:
    expected = spec.columns_for(source)
    missing = [c for c in expected if c not in header]
    extra = [c for c in header if c not in expected]
    if missing or extra:
        msg = (
            f"{spec.product} {source}: schema drift; missing={missing} unexpected={extra}. "
            "Declare the new shape (and bump schema_version if curated columns change)."
        )
        raise SchemaDriftError(msg)
    return [expected[c] for c in header]


def to_records(
    spec: TransformSpec, source: Source, header: Sequence[str], rows: Iterable[Sequence[Any]]
) -> list[dict[str, Any]]:
    """Rows keyed by canonical column name, values still raw."""
    canon = _check_columns(spec, source, header)
    out = []
    for r in rows:
        if len(r) != len(canon):
            msg = f"{spec.product} {source}: row has {len(r)} cells, header has {len(canon)}"
            raise SchemaDriftError(msg)
        out.append(dict(zip(canon, r, strict=True)))
    return out


def build_table(
    spec: TransformSpec,
    records: Iterable[Mapping[str, Any]],
    *,
    posted_at: datetime,
    ingested_at: datetime,
    source: Source,
) -> pa.Table:
    """Canonical records -> Arrow table matching ``SCHEMAS[spec.table]``."""
    cols: dict[str, list[Any]] = {name: [] for name in SCHEMAS[spec.table].names}

    def common(start: datetime, dst: bool) -> None:
        cols["interval_start"].append(start)
        cols["interval_minutes"].append(spec.interval_minutes)
        cols["posted_at"].append(posted_at)
        cols["ingested_at"].append(ingested_at)
        cols["source"].append(source)
        cols["schema_version"].append(SCHEMA_VERSION)
        cols["dst_flag"].append(dst)

    for rec in records:
        if spec.keep_flag is not None and not _to_bool(rec.get(spec.keep_flag, False)):
            continue
        if spec.keep_point_prefixes is not None and not str(
            rec.get("settlement_point", "")
        ).strip().startswith(spec.keep_point_prefixes):
            continue
        dst = _to_bool(rec.get("dst_flag", False))
        start = _interval_start(spec, rec, dst)

        if spec.table == "series":
            for col, series in (spec.series or {}).items():
                value = _to_optional_float(rec.get(col))
                if value is None:
                    continue
                common(start, dst)
                cols["series"].append(series)
                cols["value"].append(value)
            continue

        common(start, dst)
        if spec.table == "spp":
            cols["settlement_point"].append(str(rec["settlement_point"]).strip())
            spt = rec.get("settlement_point_type")
            cols["settlement_point_type"].append(str(spt).strip() if spt is not None else None)
            cols["price_mwh"].append(_to_float(rec["price_mwh"]))
        else:
            as_type = str(rec["as_type"]).strip().upper()
            if as_type not in AS_TYPES:
                msg = f"{spec.product}: unknown AS type {as_type!r}; extend AS_TYPES deliberately"
                raise SchemaDriftError(msg)
            cols["as_type"].append(as_type)
            cols["mcpc_mw"].append(_to_float(rec["mcpc_mw"]))

    schema = SCHEMAS[spec.table]
    arrays = {n: pa.array(cols[n], type=schema.field(n).type) for n in schema.names}
    return pa.table(arrays, schema=schema)


def transform(
    spec: TransformSpec,
    source: Source,
    payload: Mapping[str, Any] | str,
    *,
    posted_at: datetime,
    ingested_at: datetime,
) -> pa.Table:
    """One entrypoint for both formats. ``payload`` is the API JSON body or the CSV text."""
    if source == "api":
        if not isinstance(payload, Mapping):
            msg = "api payload must be the parsed JSON body"
            raise TypeError(msg)
        header, rows = read_api_body(payload)
    else:
        if not isinstance(payload, str):
            msg = "archive payload must be CSV text"
            raise TypeError(msg)
        header, rows = read_csv_text(payload)
    records = to_records(spec, source, header, rows)
    return build_table(spec, records, posted_at=posted_at, ingested_at=ingested_at, source=source)
