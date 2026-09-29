"""The CLI and the facade the Lambda handler shares with it."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

import pytest

import ingest.cli as cli
from ercot_lake import LakeReader
from ingest.config import DEFAULT_CONFIG_PATH, Settings, load_settings
from ingest.run import RunSummary, Window


@pytest.fixture
def offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("LAKE_ROOT", str(tmp_path / "lake"))
    monkeypatch.setenv("STATE_DIR", str(tmp_path / "state"))
    cli.settings.cache_clear()
    monkeypatch.setattr(cli, "settings", lambda: load_settings(DEFAULT_CONFIG_PATH))
    return load_settings(DEFAULT_CONFIG_PATH)


def test_naive_input_is_central_time() -> None:
    assert cli.parse_when("2026-09-03T12:00:00") == datetime(2026, 9, 3, 17, 0, tzinfo=UTC)
    assert cli.parse_when("2026-09-03") == datetime(2026, 9, 3, 5, 0, tzinfo=UTC)
    assert cli.parse_when("2026-09-03T12:00:00+00:00") == datetime(2026, 9, 3, 12, tzinfo=UTC)


def test_offline_ingests_every_product_from_the_samples(
    offline: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    """No credentials, no network: the committed samples through the whole pipeline."""
    assert cli.main_ingest(["--product", "all", "--offline"]) == 0
    out = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert {s["product"] for s in out} == set(offline.products)
    assert all(s["status"] == "ok" and s["rows_written"] > 0 for s in out)
    with LakeReader(offline.lake.root) as reader:
        assert reader.products() == sorted(offline.products)


def test_offline_leaves_watermarks_and_manifests_alone(offline: Settings) -> None:
    from ingest.state import LocalStateStore

    cli.main_ingest(["--product", "np6-905-cd", "--offline"])
    assert LocalStateStore(offline.state.local_dir).get("np6-905-cd").last_posted_at is None
    assert not list(Path(offline.lake.root).glob("manifests/np6-905-cd/*"))


def test_run_products_isolates_failures(offline: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    def flaky(run: Any) -> RunSummary:
        if run.product.key == "np4-190-cd":
            msg = "boom"
            raise RuntimeError(msg)
        return RunSummary(product=run.product.key, started_at=run.window.post_to, window=run.window)

    monkeypatch.setattr(cli, "run_product", flaky)
    out = cli.run_products(offline, "all", explicit=None, backfill=False, source="samples")
    failed = [s for s in out if s.status == "error"]
    assert [(s.product, s.error) for s in failed] == [("np4-190-cd", "RuntimeError: boom")]
    assert len(out) == len(offline.products)  # the others still ran


class FakeClient:
    instances: ClassVar[list[FakeClient]] = []

    def __init__(self, cfg: Any, creds: Any, *, live: bool = False) -> None:
        self.live = live
        FakeClient.instances.append(self)

    def __enter__(self) -> FakeClient:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


@pytest.mark.parametrize(
    ("argv", "live"),
    [
        (["--product", "np6-905-cd"], True),  # scheduled: short retry budget
        (["--product", "np6-905-cd", "--from", "2026-09-01"], False),  # explicit window
    ],
)
def test_scheduled_runs_use_the_short_retry_budget(
    offline: Settings, monkeypatch: pytest.MonkeyPatch, argv: list[str], live: bool
) -> None:
    FakeClient.instances.clear()
    monkeypatch.setattr(cli, "ErcotClient", FakeClient)
    monkeypatch.setattr(cli, "load_credentials", lambda: None)
    seen: list[Window] = []
    monkeypatch.setitem(cli.SOURCES, "archive", lambda c, p, w: seen.append(w) or iter(()))
    assert cli.main_ingest(argv) == 0
    assert FakeClient.instances[0].live is live
    if not live:
        assert seen[0].post_from == datetime(2026, 9, 1, 5, 0, tzinfo=UTC)


def test_backfill_passes_source_and_delivery_range(
    offline: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_run_products(cfg: Settings, product: str, **kw: Any) -> list[RunSummary]:
        calls.append({"product": product, **kw})
        return []

    monkeypatch.setattr(cli, "run_products", fake_run_products)
    argv = [
        "--product",
        "np6-331-cd",
        "--from",
        "2026-08-29",
        "--to",
        "2026-08-31",
        "--source",
        "hist",
        "--delivery-from",
        "2026-07-30",
    ]
    assert cli.main_backfill(argv) == 0
    (call,) = calls
    assert (call["product"], call["backfill"], call["source"]) == ("np6-331-cd", True, "hist")
    assert call["delivery_range"][0].isoformat() == "2026-07-30"
    assert call["explicit"] == Window(cli.parse_when("2026-08-29"), cli.parse_when("2026-08-31"))


def test_backfill_requires_from() -> None:
    with pytest.raises(SystemExit):
        cli.main_backfill(["--product", "np6-905-cd"])


def test_a_failed_product_makes_the_cli_exit_non_zero(
    offline: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = RunSummary(
        product="x",
        started_at=datetime(2026, 9, 1, tzinfo=UTC),
        window=Window(datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 1, tzinfo=UTC)),
        status="error",
    )
    monkeypatch.setattr(cli, "run_products", lambda *a, **k: [bad])
    assert cli.main_ingest(["--product", "all"]) == 1
