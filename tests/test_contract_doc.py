"""docs/CONTRACT.md matches ercot_lake.contract.

Parses the column tables in §3-§4, the business-key lines and the version header, and compares
them to the code, so the published contract cannot drift from what the library enforces.
"""

from __future__ import annotations

import re
from pathlib import Path

import pyarrow as pa
import pytest

from ercot_lake import contract as c

DOC = Path(__file__).resolve().parents[1] / "docs" / "CONTRACT.md"

TYPES = {
    "timestamp[us, UTC]": pa.timestamp("us", tz="UTC"),
    "int32": pa.int32(),
    "string": pa.string(),
    "float64": pa.float64(),
    "bool": pa.bool_(),
}
ROW = re.compile(r"^\|\s*`(?P<name>[a-z_]+)`\s*\|\s*(?P<type>[^|]+?)\s*\|\s*(?P<null>yes|no)\s*\|")


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    nxt = re.search(r"^#{2,3} ", text[start + len(heading) :], re.M)
    return text[start : start + len(heading) + (nxt.start() if nxt else len(text))]


def _columns(section: str) -> list[tuple[str, pa.DataType, bool]]:
    out = []
    for line in section.splitlines():
        m = ROW.match(line)
        if m:
            out.append((m["name"], TYPES[m["type"]], m["null"] == "yes"))
    return out


@pytest.fixture(scope="module")
def doc() -> str:
    return DOC.read_text()


def test_versions(doc: str) -> None:
    assert f"Contract version: **{c.CONTRACT_VERSION}**" in doc
    assert f"Curated schema version: **{c.SCHEMA_VERSION}**" in doc


def test_common_columns(doc: str) -> None:
    common = _columns(_section(doc, "## 3. Common columns"))
    assert common == [(f.name, f.type, f.nullable) for f in c.COMMON_FIELDS]


@pytest.mark.parametrize("table", c.TABLES)
def test_table_columns_and_business_key(doc: str, table: c.Table) -> None:
    section = _section(doc, f"### `{table}`")
    common = _columns(_section(doc, "## 3. Common columns"))
    expected = [(f.name, f.type, f.nullable) for f in c.SCHEMAS[table]]
    assert common + _columns(section) == expected
    key = re.search(r"Business key: \(([^)]*)\)", section)
    assert key, f"no business key line under {table}"
    assert tuple(re.findall(r"`([a-z_]+)`", key[1])) == c.BUSINESS_KEY[table]


def test_layout_keys(doc: str) -> None:
    layout = _section(doc, "## 2. Layout")
    for line in (
        "raw/<product>/date=<posting date, CT>/posted=<stamp>.zip",
        "curated/<product>/date=<delivery date, CT>/part-<stamp>.parquet",
        "curated/<product>/date=<delivery date, CT>/merged-<stamp>.parquet",
        "manifests/<product>/latest.json",
        c.CATALOG_KEY,
    ):
        assert line in layout, line
