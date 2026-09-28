"""``manifests/_catalog.json``: what a lake holds, so consumers need no copy of the pipeline's
configuration. A deployment chooses its products, so the catalog, not a hard-coded list, says
which products exist and which table each one lives in.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from ercot_lake.contract import CONTRACT_VERSION, SUPPORTED_SCHEMA_VERSIONS, Table
from ercot_lake.errors import (
    IncompatibleContractError,
    UnknownProductError,
    UnsupportedSchemaVersionError,
    WrongTableError,
)


class CatalogProduct(BaseModel):
    # Unknown fields are ignored: the pipeline may add fields in a minor release.
    model_config = ConfigDict(frozen=True, extra="ignore")

    name: str
    table: Table
    interval_minutes: int
    schema_version: int
    collected_from: datetime | None = None  # None: backfilled, history is complete
    live: bool  # False: no longer collected; its data stays readable


class Catalog(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    contract_version: str
    generated_at: datetime
    timezone: str
    products: dict[str, CatalogProduct]

    @classmethod
    def parse(cls, text: str | bytes) -> Catalog:
        catalog = cls.model_validate_json(text)
        catalog.check_compatible()
        return catalog

    def check_compatible(self) -> None:
        """Minor and patch releases only add, so any lake with our major version is readable."""
        if _major(self.contract_version) != _major(CONTRACT_VERSION):
            msg = (
                f"lake is contract {self.contract_version}, this ercot-lake reads "
                f"{_major(CONTRACT_VERSION)}.x"
            )
            raise IncompatibleContractError(msg)

    def product(self, key: str) -> CatalogProduct:
        try:
            return self.products[key]
        except KeyError:
            known = ", ".join(sorted(self.products)) or "none"
            msg = f"{key!r} is not in this lake (products: {known})"
            raise UnknownProductError(msg) from None

    def require(self, key: str, table: Table) -> CatalogProduct:
        """The product, checked to be readable through ``table``'s queries."""
        p = self.product(key)
        if p.table != table:
            msg = f"{key} is a {p.table} product, not {table}"
            raise WrongTableError(msg)
        if p.schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            msg = f"{key} is written at schema_version {p.schema_version}; upgrade ercot-lake"
            raise UnsupportedSchemaVersionError(msg)
        return p

    def to_json(self) -> str:
        return self.model_dump_json(indent=2) + "\n"


def _major(version: str) -> int:
    return int(version.split(".", 1)[0])
