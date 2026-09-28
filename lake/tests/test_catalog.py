from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from ercot_lake import (
    CONTRACT_VERSION,
    Catalog,
    IncompatibleContractError,
    UnknownProductError,
    UnsupportedSchemaVersionError,
    WrongTableError,
)

# The published example, verbatim.
EXAMPLE = """
{
  "contract_version": "1.0.0",
  "generated_at": "2026-09-23T00:00:00Z",
  "timezone": "America/Chicago",
  "products": {
    "np6-905-cd": {
      "name": "RT settlement point prices, 15-min",
      "table": "spp",
      "interval_minutes": 15,
      "schema_version": 1,
      "collected_from": null,
      "live": true
    }
  }
}
"""


def _with(**changes: object) -> str:
    doc = json.loads(EXAMPLE)
    product = doc["products"]["np6-905-cd"]
    for k, v in changes.items():
        if k in product:
            product[k] = v
        else:
            doc[k] = v
    return json.dumps(doc)


def test_parses_the_example() -> None:
    cat = Catalog.parse(EXAMPLE)
    p = cat.product("np6-905-cd")
    assert (p.table, p.interval_minutes, p.schema_version, p.live) == ("spp", 15, 1, True)
    assert p.collected_from is None
    assert cat.generated_at == datetime(2026, 9, 23, tzinfo=UTC)


def test_unknown_fields_are_ignored() -> None:
    doc = json.loads(EXAMPLE)
    doc["build"] = "abc123"
    doc["products"]["np6-905-cd"]["owner"] = "someone"
    assert Catalog.parse(json.dumps(doc)).product("np6-905-cd").name


def test_newer_minor_is_readable_other_major_is_not() -> None:
    major = int(CONTRACT_VERSION.split(".")[0])
    Catalog.parse(_with(contract_version=f"{major}.7.2"))
    with pytest.raises(IncompatibleContractError, match="this ercot-lake reads"):
        Catalog.parse(_with(contract_version=f"{major + 1}.0.0"))


def test_require_checks_table_and_schema_version() -> None:
    cat = Catalog.parse(EXAMPLE)
    assert cat.require("np6-905-cd", "spp").name
    with pytest.raises(WrongTableError):
        cat.require("np6-905-cd", "mcpc")
    with pytest.raises(UnsupportedSchemaVersionError):
        Catalog.parse(_with(schema_version=99)).require("np6-905-cd", "spp")


def test_unknown_product_lists_what_exists() -> None:
    with pytest.raises(UnknownProductError, match=r"products: np6-905-cd"):
        Catalog.parse(EXAMPLE).product("np4-190-cd")


def test_round_trip() -> None:
    cat = Catalog.parse(EXAMPLE)
    assert Catalog.parse(cat.to_json()) == cat
