"""Freshness: how old each product's newest posting is, and how many things are stale.

Reads ``manifests/<product>/latest.json`` (written by every successful scheduled run) and each
configured extra watch, then publishes one metric family:

    StaleProducts       products whose newest posting is older than their ``stale_after_min``
                        or that have no manifest at all, plus extra watches that are stale,
                        unreadable or missing. The one data alarm watches this and treats
                        missing data as breaching, so this check not running alarms too.
    FreshnessMinutes    now - newest posting, per product (dimension ``product``) and per
                        watch (dimension ``watch``). For the daily report and graphs.

Thresholds live in config.yaml, not in an alarm per product: a handful of metrics stays in
CloudWatch's free tier, and adding a product adds no alarm.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ercot_lake.contract import manifest_key
from ercot_lake.timeutil import now_utc
from ingest.config import ExtraWatch, LakeConfig, Settings
from ingest.lake import Lake

if TYPE_CHECKING:
    from mypy_boto3_cloudwatch import CloudWatchClient

log = logging.getLogger(__name__)

NAMESPACE = "ErcotIngest"


@dataclass(frozen=True)
class Freshness:
    """One product's or one watch's age. ``minutes`` is None when there is nothing to date it
    by (no manifest, no object, no usable timestamp); ``problem`` says why."""

    name: str
    kind: str  # "product" or "watch": the metric dimension
    stale_after_min: int
    last_seen: datetime | None = None
    latest_interval_start: datetime | None = None
    problem: str | None = None

    def minutes(self, now: datetime) -> float | None:
        return (now - self.last_seen).total_seconds() / 60 if self.last_seen else None

    def stale(self, now: datetime) -> bool:
        age = self.minutes(now)
        return age is None or age > self.stale_after_min

    def reason(self, now: datetime) -> str:
        age = self.minutes(now)
        if age is None:
            return f"{self.name} ({self.problem})"
        return f"{self.name} ({age:.0f} min > {self.stale_after_min})"

    def as_dict(self, now: datetime) -> dict[str, Any]:
        age = self.minutes(now)
        return {
            "name": self.name,
            "kind": self.kind,
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
            "latest_interval_start": (
                self.latest_interval_start.isoformat() if self.latest_interval_start else None
            ),
            "freshness_minutes": round(age, 1) if age is not None else None,
            "stale_after_min": self.stale_after_min,
            "stale": self.stale(now),
            "problem": self.problem,
        }


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        ts = datetime.fromisoformat(value)
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def measure_product(lake: Lake, key: str, stale_after_min: int) -> Freshness:
    mkey = manifest_key(key)
    if not lake.exists(mkey):
        return Freshness(key, "product", stale_after_min, problem="no manifest")
    m = lake.read_json(mkey)
    posted = _timestamp(m.get("last_posted_at"))
    if posted is None:
        return Freshness(key, "product", stale_after_min, problem="no posting yet")
    return Freshness(
        key,
        "product",
        stale_after_min,
        last_seen=posted,
        latest_interval_start=_timestamp(m.get("latest_interval_start")),
    )


def measure_watch(lake: Lake, watch: ExtraWatch, *, region: str | None = None) -> Freshness:
    source = (
        lake
        if watch.bucket is None
        else Lake(LakeConfig(root=f"s3://{watch.bucket}"), region=region)
    )
    where = f"{watch.bucket or 'lake'}:{watch.key}"
    try:
        if not source.exists(watch.key):
            return Freshness(watch.name, "watch", watch.stale_after_min, problem=f"missing {where}")
        obj = source.read_json(watch.key)
    except (OSError, ValueError) as exc:  # unreadable or not JSON: a broken heartbeat is stale
        return Freshness(
            watch.name, "watch", watch.stale_after_min, problem=f"unreadable {where}: {exc}"
        )
    seen = _timestamp(obj.get(watch.field)) if isinstance(obj, dict) else None
    if seen is None:
        problem = f"no ISO timestamp in {watch.field!r} of {where}"
        return Freshness(watch.name, "watch", watch.stale_after_min, problem=problem)
    return Freshness(watch.name, "watch", watch.stale_after_min, last_seen=seen)


def measure(cfg: Settings, lake: Lake, *, region: str | None = None) -> list[Freshness]:
    """Every configured product, then every extra watch."""
    out = [measure_product(lake, p.key, p.stale_after_min) for p in cfg.products.values()]
    out += [measure_watch(lake, w, region=region) for w in cfg.extra_watches]
    return out


def metric_data(results: list[Freshness], *, now: datetime) -> list[dict[str, Any]]:
    data: list[dict[str, Any]] = []
    for f in results:
        age = f.minutes(now)
        if age is not None:
            data.append(
                {
                    "MetricName": "FreshnessMinutes",
                    "Dimensions": [{"Name": f.kind, "Value": f.name}],
                    "Timestamp": now,
                    "Value": age,
                    "Unit": "None",
                }
            )
    stale = sum(f.stale(now) for f in results)
    data.append({"MetricName": "StaleProducts", "Timestamp": now, "Value": stale, "Unit": "Count"})
    return data


def publish(
    cfg: Settings,
    lake: Lake,
    cloudwatch: CloudWatchClient,
    *,
    now: datetime | None = None,
    region: str | None = None,
) -> list[Freshness]:
    when = now or now_utc()
    results = measure(cfg, lake, region=region)
    for f in results:
        log.info("%s %s: %s", f.kind, f.name, f.as_dict(when))
    stale = [f.reason(when) for f in results if f.stale(when)]
    if stale:
        # WARNING, not ERROR: the alarm on StaleProducts is the notification.
        log.warning("stale: %s", ", ".join(stale))
    cloudwatch.put_metric_data(Namespace=NAMESPACE, MetricData=metric_data(results, now=when))  # type: ignore[arg-type]
    return results
