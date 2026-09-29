"""Lambda events -> run_products, and failures that surface as invocation errors."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

import ingest.handler as h
from ingest.run import RunSummary, Window

T = datetime(2026, 9, 14, 12, tzinfo=UTC)


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []

    def fake(cfg: Any, product: str, **kw: Any) -> list[RunSummary]:
        seen.append({"product": product, **kw})
        status = "error" if product == "np0-000-xx" else "ok"
        return [RunSummary(product=product, started_at=T, window=Window(T, T), status=status)]

    monkeypatch.setattr(h, "run_products", fake)
    monkeypatch.setattr(h, "settings", lambda: None)
    return seen


def test_ingest_defaults_to_all_from_the_watermark(calls: list[dict[str, Any]]) -> None:
    out = h.ingest({})
    assert calls == [
        {
            "product": "all",
            "explicit": None,
            "backfill": False,
            "source": "archive",
            "delivery_range": None,
        }
    ]
    assert out["summaries"][0]["status"] == "ok"


def test_backfill_requires_from(calls: list[dict[str, Any]]) -> None:
    with pytest.raises(ValueError, match="requires 'from'"):
        h.backfill({"product": "np6-905-cd"})
    assert calls == []


def test_backfill_event_is_central_time_with_source_and_days(calls: list[dict[str, Any]]) -> None:
    h.backfill(
        {
            "product": "np6-331-cd",
            "from": "2026-08-29",
            "to": "2026-08-31T00:00:00+00:00",
            "source": "hist",
            "delivery_from": "2026-07-30",
            "delivery_to": "2026-08-27",
        }
    )
    (call,) = calls
    assert call["explicit"] == Window(
        datetime(2026, 8, 29, 5, tzinfo=UTC), datetime(2026, 8, 31, tzinfo=UTC)
    )
    assert (call["backfill"], call["source"]) == (True, "hist")
    assert call["delivery_range"] == (date(2026, 7, 30), date(2026, 8, 27))


def test_an_unknown_source_is_refused(calls: list[dict[str, Any]]) -> None:
    with pytest.raises(ValueError, match="unknown source"):
        h.backfill({"product": "x", "from": "2026-08-29", "source": "samples"})


def test_a_failed_product_fails_the_invocation(calls: list[dict[str, Any]]) -> None:
    with pytest.raises(RuntimeError, match="ingest failed for np0-000-xx"):
        h.ingest({"product": "np0-000-xx"})
