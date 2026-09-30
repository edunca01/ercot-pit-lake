"""Lambda entry points. The container image's CMD selects one: ``ingest.handler.ingest``,
``ingest.handler.backfill``, ``ingest.handler.compact``, ``ingest.handler.freshness`` or
``ingest.handler.daily_report``.

Event: ``{"product": "np6-905-cd" | "all", "from"?: ISO, "to"?: ISO, "source"?: "archive" |
"bundles" | "hist", "delivery_from"?: date, "delivery_to"?: date, "log_level"?: "INFO"}``.
``from``/``to`` are posting-time bounds; naive values are Central Prevailing Time, as on the
CLI. A failed product makes the invocation fail after the others have run, so the Lambda error
metric and the log-based alerts both fire.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from ercot_lake.timeutil import now_utc
from ingest import compact as _compact
from ingest import freshness as _freshness
from ingest import report as _report
from ingest.cli import SOURCES, configure_logging, parse_when, run_products
from ingest.config import load_report_targets, settings
from ingest.lake import Lake
from ingest.run import Window

log = logging.getLogger(__name__)


def _run(event: dict[str, Any], *, backfill: bool) -> dict[str, Any]:
    configure_logging(str(event.get("log_level", "INFO")))
    product = str(event.get("product", "all"))
    source = str(event.get("source", "archive"))
    if source not in SOURCES:
        msg = f"unknown source {source!r}; one of {sorted(SOURCES)}"
        raise ValueError(msg)
    explicit = None
    if backfill or "from" in event or "to" in event:
        if backfill and "from" not in event:
            msg = "backfill event requires 'from'"
            raise ValueError(msg)
        post_from = parse_when(event["from"]) if "from" in event else now_utc()
        post_to = parse_when(event["to"]) if "to" in event else now_utc()
        explicit = Window(post_from, post_to)
    d_from, d_to = event.get("delivery_from"), event.get("delivery_to")
    delivery_range = None
    if d_from or d_to:
        delivery_range = (
            date.fromisoformat(d_from) if d_from else date.min,
            date.fromisoformat(d_to) if d_to else date.max,
        )
    summaries = run_products(
        settings(),
        product,
        explicit=explicit,
        backfill=backfill,
        source=source,
        delivery_range=delivery_range,
    )
    result = {"summaries": [s.as_dict() for s in summaries]}
    failed = [s.product for s in summaries if s.status == "error"]
    if failed:
        msg = f"ingest failed for {', '.join(failed)}"
        log.error("%s: %s", msg, result)
        raise RuntimeError(msg)
    return result


def ingest(event: dict[str, Any], context: object = None) -> dict[str, Any]:
    return _run(event, backfill=False)


def backfill(event: dict[str, Any], context: object = None) -> dict[str, Any]:
    return _run(event, backfill=True)


# A partition merge takes seconds, a large backlog day under a minute: stop starting new ones
# with this much time left, rather than be killed mid-merge by the Lambda timeout.
_COMPACT_MARGIN_MS = 90_000


def compact(event: dict[str, Any], context: object = None) -> dict[str, Any]:
    """Merge old small curated files, every configured product (hourly schedule). A backlog
    larger than one run is continued by the next."""
    configure_logging(str(event.get("log_level", "INFO")))
    cfg = settings()
    remaining = getattr(context, "get_remaining_time_in_millis", None)

    def stop() -> bool:
        return remaining is not None and remaining() < _COMPACT_MARGIN_MS

    summaries = _compact.compact(cfg, Lake(cfg.lake), stop=stop)
    return {"products": [s.as_dict() for s in summaries]}


def freshness(event: dict[str, Any], context: object = None) -> dict[str, Any]:
    """Publish StaleProducts and FreshnessMinutes for every product and extra watch."""
    import boto3  # noqa: PLC0415  (only the deployed path needs the AWS SDK)

    configure_logging(str(event.get("log_level", "INFO")))
    cfg = settings()
    now = now_utc()
    results = _freshness.publish(cfg, Lake(cfg.lake), boto3.client("cloudwatch"), now=now)
    return {"results": [f.as_dict(now) for f in results]}


def daily_report(event: dict[str, Any], context: object = None) -> dict[str, Any]:
    """Build and send the daily report: the full record by email, problems and KPIs to Slack.
    Either channel may be absent."""
    import boto3  # noqa: PLC0415  (only the deployed path needs the AWS SDK)

    configure_logging(str(event.get("log_level", "INFO")))
    cfg = settings()
    targets = load_report_targets()
    aws = _report.Aws(boto3.client("cloudwatch"), boto3.client("logs"))
    report = _report.build(cfg, Lake(cfg.lake), now=now_utc(), aws=aws, targets=targets)
    sns = boto3.client("sns") if targets.email_topic or targets.slack_topic else None
    sent = _report.publish(
        report, sns, email_topic=targets.email_topic, slack_topic=targets.slack_topic
    )
    return {
        "sent": sent,
        "problems": report.problems(),
        "kpis": report.kpis(),
        "coverage": [c.as_dict() for c in report.coverage],
    }
