"""The released package names the contract version it implements: a `lake-vX.Y.Z` tag, the
package version and ``CONTRACT_VERSION`` are one number."""

from __future__ import annotations

import importlib.metadata

import ercot_lake


def test_package_version_is_the_contract_version() -> None:
    assert importlib.metadata.version("ercot-lake") == ercot_lake.CONTRACT_VERSION
