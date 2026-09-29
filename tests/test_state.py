"""Watermark stores: local JSON files and DynamoDB (against an in-memory boto3 Table)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import ingest.state as m
from ingest.config import StateConfig
from ingest.state import DynamoStateStore, LocalStateStore, Watermark, make_state_store

T0 = datetime(2026, 9, 14, 16, 32, 1, tzinfo=UTC)


class FakeTable:
    """Keyword names are boto3's, capitalized."""

    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}
        self.consistent_reads = 0

    def get_item(self, *, Key: dict[str, str], ConsistentRead: bool = False) -> dict[str, Any]:
        self.consistent_reads += int(ConsistentRead)
        item = self.items.get(Key["product"])
        return {"Item": item} if item else {}

    def put_item(self, *, Item: dict[str, Any]) -> dict[str, Any]:
        self.items[Item["product"]] = dict(Item)
        return {}


@pytest.fixture
def store() -> tuple[DynamoStateStore, FakeTable]:
    t = FakeTable()
    return DynamoStateStore(t), t  # type: ignore[arg-type]


def test_local_round_trip_and_default(tmp_path: Path) -> None:
    s = LocalStateStore(tmp_path / "state")
    assert s.get("np6-905-cd") == Watermark(product="np6-905-cd")
    wm = Watermark(product="np6-905-cd", last_posted_at=T0, last_run_at=T0, last_status="ok")
    s.put(wm)
    assert s.get("np6-905-cd") == wm
    assert not list((tmp_path / "state").glob("*.tmp"))  # written via rename


def test_missing_product_is_never(store: tuple[DynamoStateStore, FakeTable]) -> None:
    s, t = store
    wm = s.get("np6-905-cd")
    assert wm == Watermark(product="np6-905-cd")
    assert wm.last_status == "never"
    assert t.consistent_reads == 1


def test_round_trip_is_iso_strings_without_nulls(
    store: tuple[DynamoStateStore, FakeTable],
) -> None:
    s, t = store
    wm = Watermark(product="np6-905-cd", last_posted_at=T0, last_run_at=T0, last_status="ok")
    s.put(wm)
    assert t.items["np6-905-cd"] == {
        "product": "np6-905-cd",
        "last_posted_at": "2026-09-14T16:32:01Z",
        "last_run_at": "2026-09-14T16:32:01Z",
        "last_status": "ok",
    }
    assert s.get("np6-905-cd") == wm


def test_error_then_ok_clears_last_error(store: tuple[DynamoStateStore, FakeTable]) -> None:
    s, t = store
    s.put(Watermark(product="p", last_status="error", last_error="boom"))
    assert s.get("p").last_error == "boom"
    s.put(Watermark(product="p", last_status="ok", last_posted_at=T0))
    assert s.get("p").last_error is None
    assert "last_error" not in t.items["p"]


def test_local_and_dynamo_agree(tmp_path: Path, store: tuple[DynamoStateStore, FakeTable]) -> None:
    s, _ = store
    local = LocalStateStore(tmp_path)
    wm = Watermark(product="p", last_posted_at=T0, last_run_at=T0, last_status="ok")
    local.put(wm)
    s.put(wm)
    assert local.get("p") == s.get("p") == wm


def test_factory_selects_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    local = StateConfig(backend="local", local_dir=str(tmp_path), dynamodb_table="t")
    assert isinstance(make_state_store(local), LocalStateStore)
    seen: list[str] = []

    def fake_from_table_name(name: str) -> DynamoStateStore:
        seen.append(name)
        return DynamoStateStore(FakeTable())  # type: ignore[arg-type]

    monkeypatch.setattr(m.DynamoStateStore, "from_table_name", fake_from_table_name)
    dynamo = StateConfig(backend="dynamodb", local_dir="x", dynamodb_table="ercot-ingest-state")
    assert isinstance(make_state_store(dynamo), DynamoStateStore)
    assert seen == ["ercot-ingest-state"]
