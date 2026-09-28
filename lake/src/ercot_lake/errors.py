"""Errors raised by the reader. All derive from ``LakeError``."""

from __future__ import annotations


class LakeError(Exception):
    """Base class for every error this package raises on purpose."""


class CatalogNotFoundError(LakeError):
    """The lake has no ``manifests/_catalog.json``: wrong root, or nothing deployed yet."""


class IncompatibleContractError(LakeError):
    """The lake was written under a different major contract version than this library."""


class UnknownProductError(LakeError, KeyError):
    """The product is not in this lake's catalog."""


class WrongTableError(LakeError, ValueError):
    """A product was queried through another table's method (e.g. an SPP product as MCPC)."""


class UnsupportedSchemaVersionError(LakeError):
    """Curated rows carry a ``schema_version`` this library cannot read. Upgrade ercot-lake."""
