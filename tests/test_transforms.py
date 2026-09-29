"""Every configured product, both formats, against its committed ERCOT sample.

Matching the schema is the least of it. These checks catch the ways curated data goes wrong
without an error: nulls in required columns, two rows for one business key, timestamps off
the grid or shifted by an hour, and the rows consumers rely on quietly missing.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pytest

from ercot_lake.contract import AS_TYPES, BUSINESS_KEY, SCHEMAS
from ercot_lake.timeutil import utc_to_ct
from ingest.config import DEFAULT_CONFIG_PATH, REPO_ROOT, Product, load_settings
from ingest.transforms import SchemaDriftError, Source, TransformSpec, transform

SAMPLES = REPO_ROOT / "samples"
MANIFEST: dict[str, dict[str, Any]] = json.loads((SAMPLES / "manifest.json").read_text())
FINGERPRINTS: dict[str, dict[str, Any]] = json.loads(
    (REPO_ROOT / "tests" / "transform_fingerprints.json").read_text()
)
CFG = load_settings(DEFAULT_CONFIG_PATH)
PRODUCTS = sorted(CFG.products)
SOURCES: tuple[Source, ...] = ("api", "archive")
CASES = [pytest.param(k, s, id=f"{k}-{s}") for k in PRODUCTS for s in SOURCES]

# Stand-ins when a sample's posting time is not recorded (fingerprints use the same values).
POSTED = datetime(2026, 9, 1, tzinfo=UTC)
INGESTED = datetime(2026, 9, 1, 0, 5, tzinfo=UTC)

# Settlement points consumers read; every price sample must carry them.
KEY_POINTS = {"HB_NORTH", "HB_HOUSTON", "HB_SOUTH", "HB_WEST", "LZ_HOUSTON", "LZ_NORTH"}


# -- loading -------------------------------------------------------------------------------


def payload(key: str, source: Source) -> dict[str, Any] | str:
    if source == "api":
        body: dict[str, Any] = json.loads((SAMPLES / "api" / f"{key}.json").read_text())
        return body
    return (SAMPLES / "archive" / f"{key}.csv").read_text(encoding="utf-8-sig")


def posted_at(key: str, source: Source) -> datetime | None:
    """The sample's posting time, when the manifest records one (archive samples)."""
    text = MANIFEST.get(key, {}).get(source, {}).get("posted_at")
    return datetime.fromisoformat(text) if text else None


def curated(key: str, source: Source, data: dict[str, Any] | str | None = None) -> pa.Table:
    return transform(
        TransformSpec.for_product(CFG.product(key)),
        source,
        payload(key, source) if data is None else data,
        posted_at=posted_at(key, source) or POSTED,
        ingested_at=INGESTED,
    )


def raw_rows(key: str, source: Source) -> tuple[list[str], list[list[Any]]]:
    data = payload(key, source)
    if isinstance(data, dict):
        return [f["name"] for f in data["fields"]], data["data"]
    parsed = [r for r in csv.reader(io.StringIO(data)) if any(c.strip() for c in r)]
    return [h.strip() for h in parsed[0]], parsed[1:]


def source_column(p: Product, source: Source, canonical: str) -> str | None:
    pair = p.transform.columns.get(canonical)
    return None if pair is None else pair[0 if source == "api" else 1]


def by_posting(key: str, source: Source) -> list[pa.Table]:
    """The sample split into single postings. An archive sample is one posting; an API page
    spans several when the report carries its own posting time."""
    if source == "archive":
        return [curated(key, source)]
    p = CFG.product(key)
    body = payload(key, source)
    assert isinstance(body, dict)
    col = source_column(p, source, "posted_datetime")
    if col is None:
        return [curated(key, source)]
    names = [f["name"] for f in body["fields"]]
    groups: dict[Any, list[Any]] = defaultdict(list)
    for row in body["data"]:
        groups[row[names.index(col)]].append(row)
    return [curated(key, source, {**body, "data": rows}) for rows in groups.values()]


# -- 1. schema and nulls -------------------------------------------------------------------


@pytest.mark.parametrize(("key", "source"), CASES)
def test_schema_and_required_columns(key: str, source: Source) -> None:
    t = curated(key, source)
    assert t.num_rows > 0
    assert t.schema == SCHEMAS[CFG.product(key).table]
    for field in t.schema:
        if not field.nullable:
            assert t.column(field.name).null_count == 0, field.name


# -- 2. one row per business key within a posting ------------------------------------------


@pytest.mark.parametrize(("key", "source"), CASES)
def test_business_key_is_unique_within_a_posting(key: str, source: Source) -> None:
    cols = list(BUSINESS_KEY[CFG.product(key).table])
    for t in by_posting(key, source):
        keys = t.select(cols).to_pylist()
        dupes = {json.dumps(k, default=str) for k in keys if keys.count(k) > 1}
        assert not dupes, f"duplicate business keys: {sorted(dupes)[:3]}"


# -- 3. on the grid ------------------------------------------------------------------------


@pytest.mark.parametrize(("key", "source"), CASES)
def test_intervals_are_on_the_product_grid(key: str, source: Source) -> None:
    p = CFG.product(key)
    t = curated(key, source)
    assert set(t.column("interval_minutes").to_pylist()) == {p.interval_minutes}
    step = p.interval_minutes * 60
    for start in t.column("interval_start").to_pylist():
        assert int(start.timestamp()) % step == 0, start


# -- 4. delivery time plausible for the posting time --------------------------------------


def _plausible(p: Product, start: datetime, posted: datetime) -> bool:
    if p.cadence == "daily":
        # day-ahead prices clear today for tomorrow; daily actuals report yesterday
        shift = 1 if p.table in ("spp", "mcpc") else -1
        return utc_to_ct(start).date() == utc_to_ct(posted).date() + timedelta(days=shift)
    if p.cadence in ("15min", "5min"):  # real time: posted just after the interval
        return posted - timedelta(hours=1) <= start < posted
    # hourly reports: recent actuals plus forecasts up to about a week out
    return posted - timedelta(days=4) <= start <= posted + timedelta(days=8)


@pytest.mark.parametrize(("key", "source"), CASES)
def test_delivery_times_fit_the_posting_time(key: str, source: Source) -> None:
    p = CFG.product(key)
    if source == "archive":
        posted = posted_at(key, source)
        if posted is None:
            pytest.skip("posting time not recorded in samples/manifest.json")
        tables = [(curated(key, source), posted)]
    else:
        col = source_column(p, source, "posted_datetime")
        if col is None:
            pytest.skip("the API report carries no posting time")
        from ingest.timeutil import parse_post_datetime

        tables = []
        body = payload(key, source)
        assert isinstance(body, dict)
        names = [f["name"] for f in body["fields"]]
        for row in body["data"]:
            one = curated(key, source, {**body, "data": [row]})
            tables.append((one, parse_post_datetime(str(row[names.index(col)]))))
    for t, posted in tables:
        for start in t.column("interval_start").to_pylist():
            assert _plausible(p, start, posted), f"{start} does not fit posting {posted}"


# -- 5. the rows consumers rely on are there ----------------------------------------------


@pytest.mark.parametrize(("key", "source"), CASES)
def test_the_rows_we_rely_on_are_there(key: str, source: Source) -> None:
    p = CFG.product(key)
    t = curated(key, source)
    if p.table == "spp":
        points = set(t.column("settlement_point").to_pylist())
        assert points >= KEY_POINTS, f"missing {sorted(KEY_POINTS - points)}"
        types = t.column("settlement_point_type")
        if p.transform.time == "hour_interval":  # RT publishes the type; DAM and SCED don't
            assert types.null_count == 0
            lz = t.filter(pc.equal(t.column("settlement_point"), "LZ_HOUSTON"))
            assert set(lz.column("settlement_point_type").to_pylist()) >= {"LZ", "LZEW"}
        else:
            assert types.null_count == t.num_rows
    elif p.table == "mcpc":
        assert set(t.column("as_type").to_pylist()) == set(AS_TYPES)
    else:
        assert p.transform.series is not None
        assert set(t.column("series").to_pylist()) == set(p.transform.series.values())


# -- 6. both formats cover the same things -------------------------------------------------


def _coverage(t: pa.Table, table: str) -> set[str]:
    if table == "spp":
        return {s for s in t.column("settlement_point").to_pylist() if s.startswith(("HB_", "LZ_"))}
    return set(t.column("as_type" if table == "mcpc" else "series").to_pylist())


@pytest.mark.parametrize("key", PRODUCTS)
def test_api_and_archive_cover_the_same_things(key: str) -> None:
    table = CFG.product(key).table
    assert _coverage(curated(key, "api"), table) == _coverage(curated(key, "archive"), table)


# -- 7. filters have something to remove ---------------------------------------------------


@pytest.mark.parametrize(("key", "source"), CASES)
def test_filters_actually_remove_rows(key: str, source: Source) -> None:
    p = CFG.product(key)
    decl = p.transform
    if decl.keep_flag is None and decl.keep_point_prefixes is None:
        pytest.skip("no filter declared")
    header, rows = raw_rows(key, source)
    if decl.keep_flag is not None:
        col = source_column(p, source, decl.keep_flag)
        values = {str(r[header.index(col)]).strip().upper() for r in rows} if col else set()
        assert values & {"N", "FALSE", "0"}, "no rows for the keep_flag filter to drop"
    if decl.keep_point_prefixes is not None:
        col = source_column(p, source, "settlement_point")
        assert col is not None
        dropped = [
            r for r in rows if not str(r[header.index(col)]).startswith(decl.keep_point_prefixes)
        ]
        assert dropped, "no rows for the point-prefix filter to drop"


# -- 8. identical to the reference output -------------------------------------------------


def fingerprint(t: pa.Table) -> dict[str, Any]:
    rows = t.drop_columns(["ingested_at"]).to_pylist()
    lines = sorted(json.dumps(r, sort_keys=True, default=lambda v: v.isoformat()) for r in rows)
    return {"rows": len(lines), "sha256": hashlib.sha256("\n".join(lines).encode()).hexdigest()}


@pytest.mark.parametrize(("key", "source"), CASES)
def test_output_matches_its_reference_fingerprint(key: str, source: Source) -> None:
    ref = FINGERPRINTS.get(key, {}).get(source)
    if ref is None:
        pytest.skip("no reference transform for this product")
    assert fingerprint(curated(key, source)) == ref


# -- 9. drift ------------------------------------------------------------------------------


@pytest.mark.parametrize(("key", "source"), CASES)
def test_an_added_or_removed_column_is_drift(key: str, source: Source) -> None:
    data = payload(key, source)
    if isinstance(data, dict):
        added = {**data, "fields": [*data["fields"], {"name": "newColumn"}]}
        added["data"] = [[*r, 1] for r in data["data"]]
        removed = {**data, "fields": data["fields"][:-1], "data": [r[:-1] for r in data["data"]]}
    else:
        lines = data.splitlines()
        added = "\n".join(f"{ln},x" for ln in lines) + "\n"
        removed = "\n".join(ln.rsplit(",", 1)[0] for ln in lines) + "\n"
    for bad in (added, removed):
        with pytest.raises(SchemaDriftError, match="schema drift"):
            curated(key, source, bad)


# -- load reports: the zones add up to the system total --------------------------------------

LOAD_REPORTS = [
    pytest.param(k, s, id=f"{k}-{s}")
    for k in PRODUCTS
    for s in SOURCES
    if any(v.endswith(":SystemTotal") for v in (CFG.product(k).transform.series or {}).values())
]


@pytest.mark.parametrize(("key", "source"), LOAD_REPORTS)
def test_weather_zones_sum_to_the_system_total(key: str, source: Source) -> None:
    """A cheap check that each zone column is mapped to the right series: a swapped or dropped
    zone breaks the sum."""
    by_interval: dict[datetime, dict[str, float]] = defaultdict(dict)
    for r in curated(key, source).to_pylist():
        by_interval[r["interval_start"]][r["series"].split(":", 1)[1]] = r["value"]
    assert by_interval
    for start, values in by_interval.items():
        total = values.pop("SystemTotal")
        assert len(values) == 8, f"{start}: zones {sorted(values)}"
        assert abs(sum(values.values()) - total) <= max(1.0, 1e-4 * total), start
