"""ercot-lake: the lake contract as code, and a point-in-time reader (usage: README.md)."""

from ercot_lake.catalog import Catalog, CatalogProduct
from ercot_lake.contract import CONTRACT_VERSION, SCHEMA_VERSION
from ercot_lake.errors import (
    CatalogNotFoundError,
    IncompatibleContractError,
    LakeError,
    UnknownProductError,
    UnsupportedSchemaVersionError,
    WrongTableError,
)
from ercot_lake.reader import LakeReader

__all__ = [
    "CONTRACT_VERSION",
    "SCHEMA_VERSION",
    "Catalog",
    "CatalogNotFoundError",
    "CatalogProduct",
    "IncompatibleContractError",
    "LakeError",
    "LakeReader",
    "UnknownProductError",
    "UnsupportedSchemaVersionError",
    "WrongTableError",
]
