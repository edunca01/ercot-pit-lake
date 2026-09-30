"""Freshness: product manifests and extra watches -> StaleProducts and FreshnessMinutes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import ingest.handler as handler
from ercot_lake.contract import manifest_key
from ingest import freshness as fr
from ingest.config import ExtraWatch, LakeConfig, Settings
from ingest.lake import Lake

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


class FakeCloudWatch:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def put_metric_data(self, **kw: Any) -> None:
        self.calls.append(kw)

    def metrics(self) -> dict[tuple[str, str | None], float]:
        (call,) = self.calls
        assert call["Namespace"] == "ErcotIngest"
        out = {}
        for d in call["MetricData"]:
            dims = d.get("Dimensions") or [{"Name": None, "Value": None}]
            out[(d["MetricName"], dims[0]["Value"])] = d["Value"]
        return out


@pytest.fixture
def lake(tmp_path: Path) -> Lake:
    return Lake(LakeConfig(root=str(tmp_path / "lake")))


def posted(lake: Lake, key: str, minutes_ago: float) -> None:
    lake.write_json(
        manifest_key(key),
        {"product": key, "last_posted_at": (NOW - timedelta(minutes=minutes_ago)).isoformat()},
    )


def all_fresh(cfg: Settings, lake: Lake) -> None:
    for p in cfg.products.values():
        posted(lake, p.key, p.stale_after_min / 2)


def test_every_configured_product_is_measured_and_nothing_else(cfg: Settings, lake: Lake) -> None:
    all_fresh(cfg, lake)
    posted(lake, "np9-999-cd", 5)  # data from a product no longer configured is not watched
    cw = FakeCloudWatch()
    results = fr.publish(cfg, lake, cw, now=NOW)
    assert {f.name for f in results} == set(cfg.products)
    m = cw.metrics()
    assert m[("StaleProducts", None)] == 0
    assert {name for (metric, name) in m if metric == "FreshnessMinutes"} == set(cfg.products)
    assert m[("FreshnessMinutes", "np6-905-cd")] == pytest.approx(30.0)


def test_a_product_past_its_threshold_is_stale(
    cfg: Settings, lake: Lake, caplog: pytest.LogCaptureFixture
) -> None:
    all_fresh(cfg, lake)
    posted(lake, "np6-905-cd", 61)  # threshold 60
    cw = FakeCloudWatch()
    fr.publish(cfg, lake, cw, now=NOW)
    assert cw.metrics()[("StaleProducts", None)] == 1
    assert "stale: np6-905-cd (61 min > 60)" in caplog.text


def test_no_manifest_or_no_posting_is_stale_and_has_no_age(cfg: Settings, lake: Lake) -> None:
    all_fresh(cfg, lake)
    lake.write_json(manifest_key("np6-322-cd"), {"product": "np6-322-cd", "last_posted_at": None})
    missing = Lake(LakeConfig(root=lake.root))
    (Path(lake.root) / manifest_key("np4-190-cd")).unlink()
    cw = FakeCloudWatch()
    results = {f.name: f for f in fr.publish(cfg, missing, cw, now=NOW)}
    assert results["np4-190-cd"].problem == "no manifest"
    assert results["np6-322-cd"].problem == "no posting yet"
    m = cw.metrics()
    assert m[("StaleProducts", None)] == 2
    assert ("FreshnessMinutes", "np4-190-cd") not in m  # nothing to date it by


# -- extra watches ---------------------------------------------------------------------------


def with_watches(cfg: Settings, *watches: ExtraWatch) -> Settings:
    return cfg.model_copy(update={"extra_watches": list(watches)})


def watch(**kw: Any) -> ExtraWatch:
    return ExtraWatch(
        **{
            "name": "consumer",
            "key": "status/consumer.json",
            "field": "last_run",
            "stale_after_min": 30,
            **kw,
        }
    )


def test_a_stale_extra_watch_counts(cfg: Settings, lake: Lake) -> None:
    all_fresh(cfg, lake)
    lake.write_json("status/consumer.json", {"last_run": (NOW - timedelta(minutes=45)).isoformat()})
    cw = FakeCloudWatch()
    results = fr.publish(with_watches(cfg, watch()), lake, cw, now=NOW)
    m = cw.metrics()
    assert m[("StaleProducts", None)] == 1
    assert m[("FreshnessMinutes", "consumer")] == pytest.approx(45.0)
    (w,) = [f for f in results if f.kind == "watch"]
    assert w.reason(NOW) == "consumer (45 min > 30)"


def test_a_fresh_extra_watch_does_not(cfg: Settings, lake: Lake) -> None:
    all_fresh(cfg, lake)
    lake.write_json("status/consumer.json", {"last_run": "2026-09-30T11:50:00"})  # no offset: UTC
    cw = FakeCloudWatch()
    fr.publish(with_watches(cfg, watch()), lake, cw, now=NOW)
    assert cw.metrics()[("StaleProducts", None)] == 0


@pytest.mark.parametrize(
    ("content", "problem"),
    [
        (None, "missing lake:status/consumer.json"),
        (b"not json", "unreadable lake:status/consumer.json"),
        (b'{"last_run": "yesterday"}', "no ISO timestamp in 'last_run'"),
        (b'{"other": "2026-09-30T11:50:00Z"}', "no ISO timestamp in 'last_run'"),
        (b"[1, 2]", "no ISO timestamp in 'last_run'"),
    ],
)
def test_a_broken_heartbeat_is_stale(
    cfg: Settings, lake: Lake, content: bytes | None, problem: str
) -> None:
    all_fresh(cfg, lake)
    if content is not None:
        lake.write_bytes("status/consumer.json", content)
    cw = FakeCloudWatch()
    results = fr.publish(with_watches(cfg, watch()), lake, cw, now=NOW)
    (w,) = [f for f in results if f.kind == "watch"]
    assert w.problem is not None
    assert w.problem.startswith(problem)
    assert cw.metrics()[("StaleProducts", None)] == 1


def test_a_watch_in_another_bucket_reads_that_bucket(
    cfg: Settings, lake: Lake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = Lake(LakeConfig(root=str(tmp_path / "other")))
    other.write_json("hb.json", {"at": NOW.isoformat()})
    seen: list[str] = []

    def fake_lake(cfg_: LakeConfig, *, region: str | None = None) -> Lake:
        seen.append(cfg_.root)
        return other

    monkeypatch.setattr(fr, "Lake", fake_lake)
    f = fr.measure_watch(lake, watch(key="hb.json", field="at", bucket="other-bucket"))
    assert seen == ["s3://other-bucket"]
    assert (f.problem, f.minutes(NOW)) == (None, 0.0)


def test_watch_names_are_checked(cfg: Settings) -> None:
    with pytest.raises(ValidationError, match="String should match pattern"):
        watch(name="Has Spaces")
    with pytest.raises(ValidationError, match="names repeat"):
        Settings.model_validate({**cfg.model_dump(), "extra_watches": [watch().model_dump()] * 2})


# -- the handler ----------------------------------------------------------------------------


def test_the_freshness_handler(cfg: Settings, lake: Lake, monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    import types

    all_fresh(cfg, lake)
    cw = FakeCloudWatch()
    fake_boto3 = types.SimpleNamespace(client=lambda name: cw)
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    monkeypatch.setattr(
        handler, "settings", lambda: cfg.model_copy(update={"lake": LakeConfig(root=lake.root)})
    )
    monkeypatch.setattr(handler, "now_utc", lambda: NOW)
    out = handler.freshness({})
    assert len(out["results"]) == len(cfg.products)
    assert all(r["stale"] is False for r in out["results"])
    assert cw.metrics()[("StaleProducts", None)] == 0
