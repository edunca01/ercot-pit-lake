"""The lake writer on a local directory (S3 goes through the same pyarrow filesystem calls)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from ingest.config import LakeConfig
from ingest.lake import Lake

KEY = "raw/np6-905-cd/date=2026-09-03/posted=20260903T173253Z.zip"


@pytest.fixture
def lake(tmp_path: Path) -> Lake:
    return Lake(LakeConfig(root=str(tmp_path / "lake")))


def test_bytes_round_trip_and_overwrite(lake: Lake) -> None:
    assert not lake.exists(KEY)
    lake.write_bytes(KEY, b"one")
    lake.write_bytes(KEY, b"two")  # same input, same key: an overwrite, never a duplicate
    assert lake.read_bytes(KEY) == b"two"
    assert lake.list_keys("raw/np6-905-cd") == [KEY]
    assert lake.uri(KEY) == f"{lake.root}/{KEY}"


def test_table_round_trip(lake: Lake) -> None:
    t = pa.table({"a": [1, 2], "b": ["x", "y"]})
    key = "curated/x/date=2026-09-03/part-20260903T173253Z.parquet"
    lake.write_table(key, t)
    assert pq.read_table(Path(lake.root) / key).equals(t)


def test_json_round_trip_with_datetimes(lake: Lake) -> None:
    key = "manifests/np6-905-cd/latest.json"
    lake.write_json(key, {"at": datetime(2026, 9, 3, 17, 0, tzinfo=UTC), "n": 1, "p": Path("x")})
    assert lake.read_json(key) == {"at": "2026-09-03T17:00:00+00:00", "n": 1, "p": "x"}


def test_listing_a_missing_prefix_is_empty(lake: Lake) -> None:
    assert lake.list_keys("curated/nothing") == []
