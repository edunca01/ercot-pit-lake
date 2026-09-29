"""Publish ``manifests/_catalog.json``: what this lake holds, for readers that have no config.

Every configured product is live. A product that left the configuration but still has data in
the lake (a manifest) stays listed with ``live: false``, taken from the previous catalog, since
nothing else remembers its table. The file is rewritten only when its content changes.
"""

from __future__ import annotations

import logging
from datetime import datetime

from ercot_lake.catalog import Catalog, CatalogProduct
from ercot_lake.contract import (
    CATALOG_KEY,
    CONTRACT_VERSION,
    MANIFESTS_PREFIX,
    SCHEMA_VERSION,
    TIMEZONE,
    manifest_key,
)
from ercot_lake.timeutil import now_utc
from ingest.config import Settings
from ingest.lake import Lake

log = logging.getLogger(__name__)


def build_catalog(
    settings: Settings, lake: Lake, previous: Catalog | None, *, now: datetime
) -> Catalog:
    products = {
        key: CatalogProduct(
            name=p.name,
            table=p.table,
            interval_minutes=p.interval_minutes,
            schema_version=SCHEMA_VERSION,
            collected_from=p.collected_from,
            live=True,
        )
        for key, p in settings.products.items()
    }
    for key, entry in (previous.products if previous else {}).items():
        if key not in products and lake.exists(manifest_key(key)):
            products[key] = entry.model_copy(update={"live": False})
    for key in _manifest_products(lake) - products.keys():
        log.warning("%s has data but no config entry and no catalog history; not listed", key)
    return Catalog(
        contract_version=CONTRACT_VERSION,
        generated_at=now,
        timezone=TIMEZONE,
        products=dict(sorted(products.items())),
    )


def publish_catalog(settings: Settings, lake: Lake, *, now: datetime | None = None) -> Catalog:
    previous = Catalog.parse(lake.read_bytes(CATALOG_KEY)) if lake.exists(CATALOG_KEY) else None
    catalog = build_catalog(settings, lake, previous, now=now or now_utc())
    if previous is not None and _same(previous, catalog):
        return previous
    lake.write_bytes(CATALOG_KEY, catalog.to_json().encode())
    log.info("catalog: %d products (%d live)", len(catalog.products), _live(catalog))
    return catalog


def _manifest_products(lake: Lake) -> set[str]:
    suffix = "/latest.json"
    return {
        k.removeprefix(f"{MANIFESTS_PREFIX}/").removesuffix(suffix)
        for k in lake.list_keys(MANIFESTS_PREFIX)
        if k.endswith(suffix)
    }


def _same(a: Catalog, b: Catalog) -> bool:
    return (a.contract_version, a.timezone, a.products) == (
        b.contract_version,
        b.timezone,
        b.products,
    )


def _live(c: Catalog) -> int:
    return sum(p.live for p in c.products.values())
