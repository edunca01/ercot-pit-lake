"""Daily report: expected intervals on normal and DST days, gaps, sections, rendering, publish."""

from __future__ import annotations

import json
import sys
import types
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

import ingest.handler as handler
from ercot_lake.contract import curated_key, raw_key
from ercot_lake.timeutil import CT
from ingest import report
from ingest.config import (
    ExtraWatch,
    LakeConfig,
    ReportSection,
    ReportTargets,
    Settings,
    load_report_targets,
)
from ingest.lake import Lake

TARGETS = ReportTargets(ingest_function="ercot-ingest", ingest_log_group="/aws/lambda/ercot-ingest")


@pytest.fixture
def lake(tmp_path: Path) -> Lake:
    return Lake(LakeConfig(root=str(tmp_path / "lake")))


def ct(y: int, m: int, d: int, hh: int, mm: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=CT).astimezone(UTC)


NOW = ct(2026, 9, 14, 20, 0)


def only(cfg: Settings, *keys: str, **updates: Any) -> Settings:
    products = {k: cfg.product(k).model_copy(update=updates) for k in keys}
    return cfg.model_copy(update={"products": products})


# -- expected intervals ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("day", "n15", "n60"),
    [(date(2026, 9, 14), 96, 24), (date(2026, 11, 1), 100, 25), (date(2026, 3, 8), 92, 23)],
)
def test_day_intervals_follow_the_ct_calendar(day: date, n15: int, n60: int) -> None:
    assert len(report.day_intervals(day, 15)) == n15
    assert len(report.day_intervals(day, 60)) == n60
    starts = report.day_intervals(day, 15)
    assert starts[0] == ct(day.year, day.month, day.day, 0)
    assert all(b - a == timedelta(minutes=15) for a, b in pairwise(starts))


def test_rt_expects_only_intervals_ended_with_grace(cfg: Settings) -> None:
    p = cfg.product("np6-905-cd")
    exp = report.expected_intervals(p, date(2026, 9, 14), now=NOW)
    # the last expected interval ends by 19:50 CT, so it starts 19:30; 19:45 is in its grace
    assert exp[-1] == ct(2026, 9, 14, 19, 30)
    assert len(exp) == 79
    assert report.expected_intervals(p, date(2026, 9, 15), now=NOW) == []
    assert len(report.expected_intervals(p, date(2026, 9, 13), now=NOW)) == 96


def test_day_ahead_prices_are_due_the_day_before(cfg: Settings) -> None:
    p = cfg.product("np4-190-cd")
    tomorrow = date(2026, 9, 15)
    assert report.expected_intervals(p, tomorrow, now=ct(2026, 9, 14, 13, 0)) == []
    assert len(report.expected_intervals(p, tomorrow, now=ct(2026, 9, 14, 14, 0))) == 24


def test_daily_actuals_are_due_the_morning_after(cfg: Settings) -> None:
    p = cfg.product("np6-345-cd")
    day = date(2026, 9, 14)
    assert not report.is_day_ahead(p)
    assert report.expected_intervals(p, day, now=NOW) == []  # the evening of that day: not yet
    assert report.expected_intervals(p, day, now=ct(2026, 9, 15, 7, 59)) == []
    assert len(report.expected_intervals(p, day, now=ct(2026, 9, 15, 8, 0))) == 24


# -- coverage ----------------------------------------------------------------------------------


def write(lake: Lake, product: str, day: date, starts: list[datetime], posted: datetime) -> None:
    lake.write_table(
        curated_key(product, day, posted),
        pa.table({"interval_start": pa.array(starts, type=pa.timestamp("us", tz="UTC"))}),
    )


def test_coverage_counts_distinct_intervals_and_lists_missing(lake: Lake, cfg: Settings) -> None:
    day = date(2026, 9, 13)
    starts = report.day_intervals(day, 15)
    # two postings overlap on one interval; two intervals are absent
    write(lake, "np6-905-cd", day, starts[:50], ct(2026, 9, 13, 12))
    write(lake, "np6-905-cd", day, starts[49:94], ct(2026, 9, 13, 23))
    by_date = {c.delivery_date: c for c in report.coverage(only(cfg, "np6-905-cd"), lake, now=NOW)}
    c = by_date[day]
    assert (c.expected, c.present, len(c.missing)) == (96, 94, 2)
    assert c.missing == tuple(starts[94:])
    assert not c.complete
    today = by_date[date(2026, 9, 14)]  # no files at all: every expected interval is missing
    assert (today.present, today.expected, len(today.missing)) == (0, 79, 79)


def test_recent_rt_gaps_are_not_actionable_but_settled_ones_are(lake: Lake, cfg: Settings) -> None:
    day = date(2026, 9, 14)
    starts = report.expected_intervals(cfg.product("np6-905-cd"), day, now=NOW)
    # absent: 10:15 (hours old) and the newest expected interval (ERCOT may be late)
    held = [s for s in starts if s not in {ct(2026, 9, 14, 10, 15), starts[-1]}]
    write(lake, "np6-905-cd", day, held, ct(2026, 9, 14, 19, 50))
    (c,) = [
        c for c in report.coverage(only(cfg, "np6-905-cd"), lake, now=NOW) if c.delivery_date == day
    ]
    assert len(c.missing) == 2
    assert c.actionable == (ct(2026, 9, 14, 10, 15),)


def test_hourly_reports_are_counted_by_posting(lake: Lake, cfg: Settings) -> None:
    """These reports look days ahead, so the check is one raw posting per CT hour."""
    day = date(2026, 9, 13)
    hours = report.day_intervals(day, 60)
    absent = {hours[3], hours[4], hours[5]}
    for h in hours:
        if h not in absent:
            lake.write_bytes(raw_key("np4-732-cd", h + timedelta(minutes=55)), b"zip")
    by_date = {
        c.delivery_date: c
        for c in report.coverage(only(cfg, "np4-732-cd", collected_from=None), lake, now=NOW)
    }
    c = by_date[day]
    assert (c.unit, c.expected, c.present) == ("postings", 24, 21)
    assert c.actionable == c.missing == tuple(sorted(absent))  # three is past the tolerance
    assert by_date[date(2026, 9, 14)].expected == 19  # hours ending by 19:30 CT
    assert date(2026, 9, 15) not in by_date


def test_nothing_is_expected_before_collection_began(lake: Lake, cfg: Settings) -> None:
    start = ct(2026, 9, 14, 12, 0)
    (c,) = report.coverage(only(cfg, "np6-905-cd", collected_from=start), lake, now=NOW)
    assert c.delivery_date == date(2026, 9, 14)
    assert c.missing[0] == start
    assert c.expected == 31  # 12:00 .. 19:30 CT


# -- sections other systems fill ----------------------------------------------------------------


def with_sections(cfg: Settings, *specs: ReportSection) -> Settings:
    return only(cfg, "np6-905-cd").model_copy(update={"report_sections": list(specs)})


def test_sections_add_problems_kpis_and_lines(lake: Lake, cfg: Settings) -> None:
    lake.write_json(
        "status/consumer.json",
        {"problems": ["model is stale"], "kpis": ["runs today: 96"], "lines": ["detail one"]},
    )
    s = with_sections(cfg, ReportSection(title="Consumer", key="status/consumer.json"))
    r = report.build(s, lake, now=NOW)
    assert "Consumer: model is stale" in r.problems()
    assert "Consumer: runs today: 96" in r.kpis()
    text = report.render_email(r)
    assert "CONSUMER\n  runs today: 96\n  detail one" in text
    assert "detail one" not in report.render_slack(r)["content"]["description"]  # email only


def test_a_missing_section_is_skipped(lake: Lake, cfg: Settings) -> None:
    s = with_sections(cfg, ReportSection(title="Consumer", key="status/nothing.json"))
    r = report.build(s, lake, now=NOW)
    assert r.sections == []
    assert "CONSUMER" not in report.render_email(r)


@pytest.mark.parametrize(
    ("content", "error"),
    [
        (b"not json", "Expecting value"),
        (b"[1]", "not a JSON object"),
        (b'{"kpis": "one"}', "'kpis' must be a list of strings"),
        (b'{"problems": [1]}', "'problems' must be a list of strings"),
    ],
)
def test_an_unreadable_section_is_a_problem_not_silence(
    lake: Lake, cfg: Settings, content: bytes, error: str
) -> None:
    lake.write_bytes("status/consumer.json", content)
    s = with_sections(cfg, ReportSection(title="Consumer", key="status/consumer.json"))
    r = report.build(s, lake, now=NOW)
    (problem,) = [p for p in r.problems() if "report section" in p]
    assert error in problem
    assert "unreadable: lake:status/consumer.json" in report.render_email(r)


# -- CloudWatch half -------------------------------------------------------------------------------


class FakePaginator:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self.pages = pages
        self.kwargs: dict[str, Any] = {}

    def paginate(self, **kw: Any) -> list[dict[str, Any]]:
        self.kwargs = kw
        return self.pages


class FakeAws:
    def __init__(self, pages: dict[str, list[dict[str, Any]]]) -> None:
        self.paginators = {name: FakePaginator(p) for name, p in pages.items()}

    def get_paginator(self, name: str) -> FakePaginator:
        return self.paginators[name]


def history(at: datetime, alarm: str, old: str, new: str, reason: str) -> dict[str, Any]:
    data = {"oldState": {"stateValue": old}, "newState": {"stateValue": new, "stateReason": reason}}
    return {"Timestamp": at, "AlarmName": alarm, "HistoryData": json.dumps(data)}


def fake_logs(now: datetime) -> FakeAws:
    def event(minutes_ago: int, message: str) -> dict[str, Any]:
        at = now - timedelta(minutes=minutes_ago)
        return {"timestamp": int(at.timestamp() * 1000), "message": message}

    return FakeAws(
        {
            "filter_log_events": [
                {
                    "events": [
                        event(5, "not json at all"),
                        event(90, json.dumps({"level": "ERROR", "message": "np6-331-cd: gave up"})),
                    ]
                }
            ]
        }
    )


def fake_cloudwatch(cfg: Settings, now: datetime) -> FakeAws:
    watched = [p.key for p in cfg.products.values()] + [w.name for w in cfg.extra_watches]
    ids = {name: f"f{i}" for i, name in enumerate(watched)}
    peaks = {
        "np6-331-cd": [13.2, 28.2],
        "np6-905-cd": [13.3],
        "np4-190-cd": [1440.0],
        "heartbeat": [25.0],
    }
    return FakeAws(
        {
            "describe_alarm_history": [
                {
                    "AlarmHistoryItems": [
                        history(now - timedelta(hours=2), "ercot-data-stale", "OK", "ALARM", "r2"),
                        history(
                            now - timedelta(hours=3),
                            "ercot-daily-report-invocation-errors",
                            "OK",
                            "ALARM",
                            "r1",
                        ),
                        history(now - timedelta(hours=1), "someone-elses-alarm", "OK", "ALARM", ""),
                    ]
                }
            ],
            "describe_alarms": [
                {"MetricAlarms": [], "CompositeAlarms": [{"AlarmName": "ercot-data-stale"}]}
            ],
            "get_metric_data": [
                {
                    "MetricDataResults": [
                        {"Id": "inv", "Values": [3000.0, 3480.0]},
                        {"Id": "err", "Values": [13.0]},
                        *[
                            {"Id": ids[name], "Values": values}
                            for name, values in peaks.items()
                            if name in ids
                        ],
                    ]
                }
            ],
        }
    )


def test_build_reads_alarm_activity_and_ingest_stats(lake: Lake, cfg: Settings) -> None:
    beat = ExtraWatch(name="heartbeat", key="hb.json", field="at", stale_after_min=30)
    s = cfg.model_copy(update={"extra_watches": [beat]})
    cw = fake_cloudwatch(s, NOW)
    r = report.build(s, lake, now=NOW, aws=report.Aws(cw, fake_logs(NOW)), targets=TARGETS)  # type: ignore[arg-type]
    assert r.transitions is not None
    assert [t.alarm for t in r.transitions] == [
        "ercot-daily-report-invocation-errors",
        "ercot-data-stale",
    ]
    assert r.transitions[0].reason == "r1"
    assert r.alarming == ["ercot-data-stale"]
    assert r.ingest is not None
    assert (r.ingest.invocations, r.ingest.errors) == (6480, 13)
    assert r.ingest.freshness_peak["np6-331-cd"] == 28.2
    window = cw.paginators["describe_alarm_history"].kwargs
    assert window["EndDate"] - window["StartDate"] == timedelta(hours=24)
    kpis = "\n".join(r.kpis())
    assert "alarms fired, 24 h: 2" in kpis
    assert "ingest runs, 24 h: 6,480, 13 failed (0.20%)" in kpis
    # the heartbeat at 25 of 30 min is closest; the daily product's ~24 h is never the headline
    assert "closest to its limit: heartbeat peaked at 25 of 30 min" in kpis
    assert "ercot-data-stale is in ALARM right now" in r.problems()
    assert r.errors is not None
    assert r.errors.total == 2
    assert [text for _, text in r.errors.shown] == ["np6-331-cd: gave up", "not json at all"]
    text = report.render_email(r)
    assert "INGEST ERROR LINES, last 24 h (UTC): 2" in text
    assert "ercot-data-stale  OK -> ALARM" in text
    assert "heartbeat" in text.split("FRESHNESS PEAKS")[1]
    assert "gave up" not in report.render_slack(r)["content"]["description"]


# -- rendering and publishing ----------------------------------------------------------------------


def a_report(covs: list[report.Coverage]) -> report.DailyReport:
    return report.DailyReport(now=NOW, coverage=covs, thresholds={"np6-905-cd": 60})


def test_email_is_the_full_record_and_slack_only_problems_and_kpis() -> None:
    gap = (ct(2026, 9, 14, 10, 15), ct(2026, 9, 14, 10, 30))
    covs = [
        report.Coverage("np6-905-cd", date(2026, 9, 13), 96, 96),
        report.Coverage("np6-905-cd", date(2026, 9, 14), 79, 77, gap, actionable=gap),
    ]
    r = a_report(covs)
    text = report.render_email(r)
    assert text.startswith("ERCOT daily report as of 2026-09-14 20:00 CT\n1 item needs attention")
    assert "np6-905-cd   2026-09-14 intervals       79       77       2" in text
    assert "np6-905-cd 2026-09-14: 10:15, 10:30 CT" in text
    card = report.render_slack(r)
    assert (card["source"], card["version"]) == ("custom", "1.0")
    assert card["content"]["title"] == "⚠️ ERCOT daily 2026-09-14: 1 item needs attention"
    body = card["content"]["description"]
    assert "np6-905-cd 2026-09-14: 2 intervals missing (10:15, 10:30 CT)" in body
    assert "coverage 2026-09-13: 100.0 % (0 of 96 missing)" in body
    assert "expected" not in body  # no coverage table in Slack
    healthy = report.render_slack(a_report(covs[:1]))
    assert healthy["content"]["title"] == "✅ ERCOT daily 2026-09-14: nothing needs attention"
    assert "Needs attention" not in healthy["content"]["description"]


def test_slack_caps_the_problem_list() -> None:
    gap = (ct(2026, 9, 13, 10, 15),)
    covs = [
        report.Coverage(f"p{i}", date(2026, 9, 13), 96, 95, gap, actionable=gap) for i in range(9)
    ]
    body = report.render_slack(a_report(covs))["content"]["description"]
    assert body.count("backfill that window") == report.SLACK_PROBLEMS
    assert "+3 more in the email report" in body


class FakeSNS:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def publish(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(kw)
        return {"MessageId": "x"}


def test_publish_to_either_both_or_neither_channel(
    lake: Lake, cfg: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    r = report.build(only(cfg, "np6-905-cd"), lake, now=NOW)
    sns = FakeSNS()
    assert report.publish(r, sns, email_topic="topic-email", slack_topic="topic-slack") == [
        "email",
        "slack",
    ]  # type: ignore[arg-type]
    email, slack = sns.calls
    assert email["TopicArn"] == "topic-email"
    assert email["Subject"].startswith("ERCOT daily 2026-09-14:")
    assert email["Message"].startswith("ERCOT daily report as of 2026-09-14 20:00 CT")
    assert slack["TopicArn"] == "topic-slack"
    assert "Subject" not in slack
    assert json.loads(slack["Message"])["source"] == "custom"
    sns.calls.clear()
    assert report.publish(r, sns, email_topic=None, slack_topic="topic-slack") == ["slack"]  # type: ignore[arg-type]
    # a fork with neither channel still gets the report, in its logs
    caplog.set_level("INFO")
    assert report.publish(r, None, email_topic=None, slack_topic=None) == []
    assert "no report channel configured" in caplog.text


# -- configuration and the handler ---------------------------------------------------------------


def test_report_targets_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INGEST_FUNCTION_NAME", "ercot-ingest")
    monkeypatch.setenv("INGEST_LOG_GROUP", "/aws/lambda/ercot-ingest")
    monkeypatch.setenv("ACTIONS_TOPIC_ARN", "")  # declared but empty: no Slack
    t = load_report_targets()
    assert (t.alarm_prefix, t.email_topic, t.slack_topic) == ("ercot", None, None)
    monkeypatch.setenv("ALERTS_TOPIC_ARN", "topic-email")
    assert load_report_targets().email_topic == "topic-email"


def test_the_daily_report_handler_without_channels(
    lake: Lake, cfg: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = only(cfg, "np6-905-cd").model_copy(update={"lake": LakeConfig(root=lake.root)})
    clients = {"cloudwatch": fake_cloudwatch(cfg, NOW), "logs": fake_logs(NOW)}
    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=clients.__getitem__))
    monkeypatch.setattr(handler, "settings", lambda: s)
    monkeypatch.setattr(handler, "load_report_targets", lambda: TARGETS)
    monkeypatch.setattr(handler, "now_utc", lambda: NOW)
    out = handler.daily_report({})
    assert out["sent"] == []  # no topics: SNS is never created, the report is logged
    assert "ercot-data-stale is in ALARM right now" in out["problems"]
    assert {c["product"] for c in out["coverage"]} == {"np6-905-cd"}
