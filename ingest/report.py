"""Daily report, sent once a day in two shapes, each channel optional.

    email   the full record: coverage table, the ingest ERROR lines and alarm transitions of
            the last 24 h, freshness peaks, and every report section. There are no
            per-event emails; this is the trace.
    Slack   only what needs someone to act, plus a handful of KPIs.

Coverage: for each product and delivery date, how many intervals ERCOT should have published
by now versus how many the lake holds. Expected intervals come from the CT calendar (so DST
days have 92 or 100 RT intervals, 23 or 25 hours). "Should have been published by now":

    RT, SCED        the interval ended at least ``RT_GRACE`` ago (ERCOT stamps +2 min, lists
                    later)
    day-ahead       the whole day, once the posting deadline (13:30 CT the day before) passed
    daily actuals   the whole day, once the next morning's posting window (08:00 CT) passed
    hourly reports  these look days ahead, so intervals are always "present"; what can go
                    missing is a posting, so they are counted as one posting per CT hour from
                    raw/

Present intervals are the distinct ``interval_start`` values across every posting in the
partition, so a re-posting counts once and a missing posting shows as missing intervals.
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any

from ercot_lake.contract import RAW_PREFIX, curated_partition
from ercot_lake.timeutil import CT, delivery_date_ct, parse_stamp, utc_to_ct
from ingest.config import LakeConfig, Product, ReportSection, ReportTargets, Settings
from ingest.freshness import NAMESPACE
from ingest.lake import Lake

if TYPE_CHECKING:
    from mypy_boto3_cloudwatch import CloudWatchClient
    from mypy_boto3_logs import CloudWatchLogsClient
    from mypy_boto3_sns import SNSClient

log = logging.getLogger(__name__)

RT_GRACE = timedelta(minutes=10)
DAY_AHEAD_DEADLINE_CT = time(13, 30)  # the day before delivery
DAY_AFTER_DEADLINE_CT = time(8, 0)  # the morning after: actuals post ~05:50, polling ends 07:55
POSTING_GRACE = timedelta(minutes=30)  # an hourly report for hour h is due by h + 1 h + this
# A gap is "actionable" (Slack) once it can no longer be ERCOT merely running late.
GAP_SETTLE = timedelta(hours=2)
POSTING_TOLERANCE = 2  # ERCOT skips the odd hourly report and catches up; more than this is ours
WINDOW = timedelta(hours=24)  # alarm activity and ingest stats look back this far
ERROR_LINES = 25  # the email shows this many of the window's ERROR lines, newest last
SLACK_PROBLEMS = 6  # Slack shows this many; the email report has them all
READ_THREADS = 16  # a 5-minute product is 288 small files a day before compaction


# -- coverage ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Coverage:
    product: str
    delivery_date: date
    expected: int
    present: int
    missing: tuple[datetime, ...] = field(default=())  # UTC interval (or posting-hour) starts
    unit: str = "intervals"  # intervals | postings
    actionable: tuple[datetime, ...] = field(default=())  # the part of ``missing`` worth a ping

    @property
    def complete(self) -> bool:
        return self.expected == self.present and not self.missing

    def as_dict(self) -> dict[str, Any]:
        return {
            "product": self.product,
            "date": self.delivery_date.isoformat(),
            "unit": self.unit,
            "expected": self.expected,
            "present": self.present,
            "missing": [m.isoformat() for m in self.missing],
            "actionable": len(self.actionable),
        }


def day_intervals(delivery_date: date, interval_minutes: int) -> list[datetime]:
    """Every interval start of a CT delivery day, as UTC, DST-correct (23/24/25 hours)."""
    start = datetime.combine(delivery_date, time.min, tzinfo=CT).astimezone(UTC)
    end = datetime.combine(delivery_date + timedelta(days=1), time.min, tzinfo=CT).astimezone(UTC)
    step = timedelta(minutes=interval_minutes)
    out: list[datetime] = []
    cur = start  # walk in UTC so the repeated/skipped CT hour falls out naturally
    while cur < end:
        out.append(cur)
        cur += step
    return out


def is_day_ahead(product: Product) -> bool:
    """Daily prices clear the day before delivery; daily actuals are published the day after."""
    return product.cadence == "daily" and product.table in ("spp", "mcpc")


def expected_intervals(product: Product, delivery_date: date, *, now: datetime) -> list[datetime]:
    intervals = day_intervals(delivery_date, product.interval_minutes)
    if product.cadence == "daily":
        if is_day_ahead(product):
            due = datetime.combine(delivery_date - timedelta(days=1), DAY_AHEAD_DEADLINE_CT)
        else:
            due = datetime.combine(delivery_date + timedelta(days=1), DAY_AFTER_DEADLINE_CT)
        return intervals if now >= due.replace(tzinfo=CT) else []
    step = timedelta(minutes=product.interval_minutes)
    return [s for s in intervals if s + step + RT_GRACE <= now]


def present_intervals(lake: Lake, product: Product, delivery_date: date) -> set[datetime]:
    keys = [
        k
        for k in lake.list_keys(curated_partition(product.key, delivery_date))
        if k.endswith(".parquet")
    ]
    found: set[datetime] = set()
    with ThreadPoolExecutor(max_workers=READ_THREADS) as pool:
        for starts in pool.map(lambda k: lake.read_column(k, "interval_start"), keys):
            found.update(starts)
    return found


def expected_posting_hours(posting_date: date, *, now: datetime) -> list[datetime]:
    """CT hours of ``posting_date`` whose hourly report is due by now."""
    hours = day_intervals(posting_date, 60)
    return [h for h in hours if h + timedelta(hours=1) + POSTING_GRACE <= now]


def present_posting_hours(lake: Lake, product: Product, posting_date: date) -> set[datetime]:
    """Hours (UTC) with at least one raw posting. raw/ is keyed by posting date and stamp."""
    found: set[datetime] = set()
    for key in lake.list_keys(f"{RAW_PREFIX}/{product.key}/date={posting_date}/"):
        name = key.rsplit("/", 1)[-1]
        if name.startswith("posted=") and name.endswith(".zip"):
            posted = parse_stamp(name.removeprefix("posted=").removesuffix(".zip"))
            found.add(posted.replace(minute=0, second=0, microsecond=0))
    return found


def report_dates(now: datetime) -> list[date]:
    today = delivery_date_ct(now)
    return [today - timedelta(days=1), today, today + timedelta(days=1)]


def _actionable(
    product: Product, missing: tuple[datetime, ...], now: datetime
) -> tuple[datetime, ...]:
    if product.cadence == "daily":
        return missing  # expected only after its deadline, and the report runs hours after it
    if product.cadence == "hourly":
        return missing if len(missing) > POSTING_TOLERANCE else ()
    step = timedelta(minutes=product.interval_minutes)
    return tuple(m for m in missing if m + step + GAP_SETTLE <= now)


def coverage(cfg: Settings, lake: Lake, *, now: datetime) -> list[Coverage]:
    out: list[Coverage] = []
    for product in cfg.products.values():
        hourly = product.cadence == "hourly"
        for d in report_dates(now):
            if hourly:
                expected = expected_posting_hours(d, now=now)
            else:
                expected = expected_intervals(product, d, now=now)
            if product.collected_from is not None:
                expected = [s for s in expected if s >= product.collected_from]
            if not expected:
                continue
            if hourly:
                present = present_posting_hours(lake, product, d)
            else:
                present = present_intervals(lake, product, d)
            missing = tuple(s for s in expected if s not in present)
            out.append(
                Coverage(
                    product=product.key,
                    delivery_date=d,
                    expected=len(expected),
                    present=len(expected) - len(missing),
                    missing=missing,
                    unit="postings" if hourly else "intervals",
                    actionable=_actionable(product, missing, now),
                )
            )
    return out


# -- sections other systems fill ----------------------------------------------------------------


@dataclass(frozen=True)
class Section:
    title: str
    problems: tuple[str, ...] = ()
    kpis: tuple[str, ...] = ()
    lines: tuple[str, ...] = ()
    error: str | None = None  # the object exists but cannot be read as a section


def _strings(obj: dict[str, Any], name: str) -> tuple[str, ...]:
    value = obj.get(name, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        msg = f"{name!r} must be a list of strings"
        raise TypeError(msg)
    return tuple(value)


def read_section(lake: Lake, spec: ReportSection, *, region: str | None = None) -> Section | None:
    """None when the object does not exist: that system has nothing to say today."""
    source = (
        lake if spec.bucket is None else Lake(LakeConfig(root=f"s3://{spec.bucket}"), region=region)
    )
    if not source.exists(spec.key):
        return None
    try:
        obj: Any = json.loads(source.read_bytes(spec.key))  # any JSON; checked below
        if not isinstance(obj, dict):
            msg = "not a JSON object"
            raise TypeError(msg)
        return Section(
            spec.title, _strings(obj, "problems"), _strings(obj, "kpis"), _strings(obj, "lines")
        )
    except (OSError, ValueError, TypeError) as exc:
        return Section(spec.title, error=f"{spec.bucket or 'lake'}:{spec.key}: {exc}")


# -- alarm activity and ingest stats (CloudWatch) ----------------------------------------------


@dataclass(frozen=True)
class Transition:
    at: datetime
    alarm: str
    old: str
    new: str
    reason: str


@dataclass(frozen=True)
class IngestStats:
    invocations: int
    errors: int
    freshness_peak: dict[str, float]  # product or watch -> worst FreshnessMinutes in the window

    @property
    def error_rate(self) -> float:
        return self.errors / self.invocations if self.invocations else 0.0


def alarm_transitions(
    cloudwatch: CloudWatchClient, *, prefix: str, since: datetime, now: datetime
) -> list[Transition]:
    """Every state change of this project's alarms in the window, oldest first."""
    out: list[Transition] = []
    pages = cloudwatch.get_paginator("describe_alarm_history").paginate(
        AlarmTypes=["MetricAlarm", "CompositeAlarm"],
        HistoryItemType="StateUpdate",
        StartDate=since,
        EndDate=now,
    )
    for page in pages:
        for item in page["AlarmHistoryItems"]:
            name = item.get("AlarmName", "")
            if not name.startswith(prefix):
                continue
            data = json.loads(item.get("HistoryData", "{}"))
            new = data.get("newState", {})
            out.append(
                Transition(
                    at=item["Timestamp"].astimezone(UTC),
                    alarm=name,
                    old=data.get("oldState", {}).get("stateValue", "?"),
                    new=new.get("stateValue", "?"),
                    reason=new.get("stateReason", ""),
                )
            )
    return sorted(out, key=lambda t: t.at)


def alarms_in_alarm(cloudwatch: CloudWatchClient, *, prefix: str) -> list[str]:
    names: list[str] = []
    pages = cloudwatch.get_paginator("describe_alarms").paginate(
        AlarmNamePrefix=prefix, StateValue="ALARM", AlarmTypes=["MetricAlarm", "CompositeAlarm"]
    )
    for page in pages:
        names += [a["AlarmName"] for a in page.get("MetricAlarms", [])]
        names += [a["AlarmName"] for a in page.get("CompositeAlarms", [])]
    return sorted(names)


def ingest_stats(
    cfg: Settings,
    cloudwatch: CloudWatchClient,
    targets: ReportTargets,
    *,
    since: datetime,
    now: datetime,
) -> IngestStats:
    watched = [("product", p.key) for p in cfg.products.values()]
    watched += [("watch", w.name) for w in cfg.extra_watches]

    def query(i: str, ns: str, metric: str, dim: tuple[str, str], stat: str) -> dict[str, Any]:
        return {
            "Id": i,
            "MetricStat": {
                "Metric": {
                    "Namespace": ns,
                    "MetricName": metric,
                    "Dimensions": [{"Name": dim[0], "Value": dim[1]}],
                },
                "Period": 3600,
                "Stat": stat,
            },
        }

    fn = ("FunctionName", targets.ingest_function)
    queries = [
        query("inv", "AWS/Lambda", "Invocations", fn, "Sum"),
        query("err", "AWS/Lambda", "Errors", fn, "Sum"),
        *[
            query(f"f{i}", NAMESPACE, "FreshnessMinutes", dim, "Maximum")
            for i, dim in enumerate(watched)
        ],
    ]
    values: dict[str, list[float]] = {}
    pages = cloudwatch.get_paginator("get_metric_data").paginate(
        MetricDataQueries=queries,  # type: ignore[arg-type]
        StartTime=since,
        EndTime=now,
    )
    for page in pages:
        for r in page["MetricDataResults"]:
            values.setdefault(r["Id"], []).extend(r["Values"])
    return IngestStats(
        invocations=int(sum(values.get("inv", []))),
        errors=int(sum(values.get("err", []))),
        freshness_peak={
            name: max(values[f"f{i}"]) for i, (_, name) in enumerate(watched) if values.get(f"f{i}")
        },
    )


@dataclass(frozen=True)
class ErrorLines:
    total: int
    shown: list[tuple[datetime, str]]  # the newest ERROR_LINES, oldest first


def ingest_error_lines(
    logs: CloudWatchLogsClient, *, log_group: str, since: datetime, now: datetime
) -> ErrorLines:
    """ERROR-level lines of the ingest log (JSON logs). This replaces a per-event alarm email."""
    found: list[tuple[datetime, str]] = []
    pages = logs.get_paginator("filter_log_events").paginate(
        logGroupName=log_group,
        filterPattern='{ $.level = "ERROR" }',
        startTime=int(since.timestamp() * 1000),
        endTime=int(now.timestamp() * 1000),
    )
    for page in pages:
        for event in page["events"]:
            raw = event.get("message", "")
            try:
                text = str(json.loads(raw).get("message", raw))
            except json.JSONDecodeError:
                text = raw
            at = datetime.fromtimestamp(event.get("timestamp", 0) / 1000, tz=UTC)
            found.append((at, " ".join(text.split())[:240]))
    found.sort(key=lambda e: e[0])
    return ErrorLines(total=len(found), shown=found[-ERROR_LINES:])


# -- the report --------------------------------------------------------------------------------


@dataclass(frozen=True)
class DailyReport:
    now: datetime
    coverage: list[Coverage]
    thresholds: dict[str, int]  # product or watch -> stale_after_min
    peak_names: frozenset[str] = frozenset()  # whose freshness peak is a KPI
    sections: list[Section] = field(default_factory=list)
    transitions: list[Transition] | None = None  # None: CloudWatch not consulted (local run)
    alarming: list[str] | None = None
    ingest: IngestStats | None = None
    errors: ErrorLines | None = None

    def problems(self) -> list[str]:
        """What someone has to do something about. Empty on a healthy day."""
        out = [f"{name} is in ALARM right now" for name in self.alarming or []]
        for c in self.coverage:
            if c.actionable:
                out.append(
                    f"{c.product} {c.delivery_date}: {len(c.actionable)} {c.unit} missing "
                    f"({_fmt_missing(c.actionable, limit=4)}); backfill that window"
                )
        for s in self.sections:
            if s.error:
                out.append(f"report section {s.title!r} is unreadable: {s.error}")
            out += [f"{s.title}: {p}" for p in s.problems]
        return out

    def kpis(self) -> list[str]:
        out: list[str] = []
        yesterday = delivery_date_ct(self.now) - timedelta(days=1)
        rows = [c for c in self.coverage if c.delivery_date == yesterday and c.unit == "intervals"]
        expected = sum(c.expected for c in rows)
        if expected:
            missing = sum(len(c.missing) for c in rows)
            pct = 100 * (expected - missing) / expected
            out.append(f"coverage {yesterday}: {pct:.1f} % ({missing} of {expected:,} missing)")
        if self.transitions is not None:
            fired = sum(1 for t in self.transitions if t.new == "ALARM")
            out.append(f"alarms fired, 24 h: {fired}")
        if self.ingest is not None:
            i = self.ingest
            out.append(
                f"ingest runs, 24 h: {i.invocations:,}, {i.errors} failed ({i.error_rate:.2%})"
            )
            ratios = {
                name: peak / self.thresholds[name]
                for name, peak in i.freshness_peak.items()
                if name in self.peak_names and name in self.thresholds
            }
            if ratios:
                worst = max(ratios, key=lambda n: ratios[n])
                out.append(
                    f"closest to its limit: {worst} peaked at {i.freshness_peak[worst]:.0f} of "
                    f"{self.thresholds[worst]} min"
                )
        for s in self.sections:
            out += [f"{s.title}: {k}" for k in s.kpis]
        return out


@dataclass(frozen=True)
class Aws:
    """The clients behind the CloudWatch half of the report. Absent on a local run."""

    cloudwatch: CloudWatchClient
    logs: CloudWatchLogsClient


def build(
    cfg: Settings,
    lake: Lake,
    *,
    now: datetime,
    aws: Aws | None = None,
    targets: ReportTargets | None = None,
) -> DailyReport:
    thresholds = {p.key: p.stale_after_min for p in cfg.products.values()}
    thresholds |= {w.name: w.stale_after_min for w in cfg.extra_watches}
    sections = [s for spec in cfg.report_sections if (s := read_section(lake, spec)) is not None]
    report = DailyReport(
        now=now,
        coverage=coverage(cfg, lake, now=now),
        thresholds=thresholds,
        # A daily product sits at ~24 h of its 26 h limit by construction; not a signal.
        peak_names=frozenset(
            [p.key for p in cfg.products.values() if p.cadence != "daily"]
            + [w.name for w in cfg.extra_watches]
        ),
        sections=sections,
    )
    if aws is None or targets is None:
        return report
    since = now - WINDOW
    return replace(
        report,
        transitions=alarm_transitions(
            aws.cloudwatch, prefix=targets.alarm_prefix, since=since, now=now
        ),
        alarming=alarms_in_alarm(aws.cloudwatch, prefix=targets.alarm_prefix),
        ingest=ingest_stats(cfg, aws.cloudwatch, targets, since=since, now=now),
        errors=ingest_error_lines(
            aws.logs, log_group=targets.ingest_log_group, since=since, now=now
        ),
    )


# -- rendering ---------------------------------------------------------------------------------


def _fmt_missing(missing: tuple[datetime, ...], limit: int = 8) -> str:
    shown = [utc_to_ct(m).strftime("%H:%M") for m in missing[:limit]]
    more = f" +{len(missing) - limit} more" if len(missing) > limit else ""
    return ", ".join(shown) + more + " CT"


def summary_line(report: DailyReport) -> str:
    n = len(report.problems())
    if n == 0:
        return "nothing needs attention"
    return f"{n} item{'s' if n > 1 else ''} need{'' if n > 1 else 's'} attention"


def render_email(report: DailyReport) -> str:
    """The full record, plain text."""
    local = utc_to_ct(report.now)
    problems = report.problems()
    lines = [f"ERCOT daily report as of {local:%Y-%m-%d %H:%M} CT", summary_line(report), ""]
    if problems:
        lines += ["NEEDS ATTENTION", *[f"  - {p}" for p in problems], ""]
    lines += ["KPIs", *[f"  {k}" for k in report.kpis()], ""]

    lines += [
        "COVERAGE",
        f"  {'product':12} {'date':10} {'unit':9} {'expected':>8} {'present':>8} {'missing':>7}",
    ]
    lines += [
        f"  {c.product:12} {c.delivery_date} {c.unit:9} {c.expected:>8} {c.present:>8} "
        f"{len(c.missing):>7}"
        for c in report.coverage
    ]
    gaps = [c for c in report.coverage if c.missing]
    if gaps:
        lines += ["", "  missing:"]
        lines += [f"    {c.product} {c.delivery_date}: {_fmt_missing(c.missing)}" for c in gaps]
    lines.append("")

    for s in report.sections:
        lines.append(s.title.upper())
        if s.error:
            lines.append(f"  unreadable: {s.error}")
        lines += [f"  {x}" for x in (*s.kpis, *s.lines)]
        lines.append("")

    if report.transitions is not None:
        lines.append(f"ALARM TRANSITIONS, last 24 h (UTC): {len(report.transitions)}")
        for t in report.transitions:
            lines.append(f"  {t.at:%Y-%m-%d %H:%M:%S}  {t.alarm}  {t.old} -> {t.new}")
            if t.reason:
                lines.append(f"      {t.reason}")
        lines += ["", f"IN ALARM NOW: {', '.join(report.alarming or []) or 'none'}", ""]

    if report.errors is not None:
        e = report.errors
        lines.append(f"INGEST ERROR LINES, last 24 h (UTC): {e.total}")
        if e.total > len(e.shown):
            lines.append(f"  (newest {len(e.shown)} shown)")
        lines += [f"  {at:%Y-%m-%d %H:%M:%S}  {text}" for at, text in e.shown]
        lines.append("")

    if report.ingest is not None:
        lines += [
            "FRESHNESS PEAKS, last 24 h (minutes since the newest posting or heartbeat)",
            f"  {'name':16} {'peak':>8} {'stale after':>12}",
        ]
        for name, limit in report.thresholds.items():
            peak = report.ingest.freshness_peak.get(name)
            shown = f"{peak:>8.1f}" if peak is not None else f"{'n/a':>8}"
            lines.append(f"  {name:16} {shown} {limit:>12}")
        lines.append("")

    lines += [
        "expected = what ERCOT should have published by now (RT/SCED: interval ended >10 min ago;",
        "day-ahead prices: the whole day once 13:30 CT the day before has passed; daily actuals:",
        "the whole day once 08:00 CT the day after has passed; hourly reports: one posting per CT",
        "hour). A failed scheduled run is retried by the next one; only staleness alarms.",
    ]
    return "\n".join(lines)


def render_slack(report: DailyReport) -> dict[str, Any]:
    """AWS Chatbot custom notification: problems (if any) and KPIs, nothing else."""
    local = utc_to_ct(report.now)
    problems = report.problems()
    parts: list[str] = []
    if problems:
        shown = [f"• {p}" for p in problems[:SLACK_PROBLEMS]]
        if len(problems) > SLACK_PROBLEMS:
            shown.append(f"• +{len(problems) - SLACK_PROBLEMS} more in the email report")
        parts += ["*Needs attention*", *shown, ""]
    parts += ["*KPIs*", *[f"• {k}" for k in report.kpis()]]
    return {
        "version": "1.0",
        "source": "custom",
        "content": {
            "textType": "client-markdown",
            "title": f"{'⚠️' if problems else '✅'} ERCOT daily {local:%Y-%m-%d}: "
            f"{summary_line(report)}",
            "description": "\n".join(parts),
        },
    }


def publish(
    report: DailyReport, sns: SNSClient | None, *, email_topic: str | None, slack_topic: str | None
) -> list[str]:
    """Full text to the email topic, the short card to the Slack topic; either may be absent.
    Returns the channels sent to. With neither, the report is only logged."""
    local = utc_to_ct(report.now)
    for c in report.coverage:
        log.info("%s", c.as_dict())
    sent = []
    if email_topic and sns is not None:
        sns.publish(
            TopicArn=email_topic,
            Subject=f"ERCOT daily {local:%Y-%m-%d}: {summary_line(report)}"[:100],
            Message=render_email(report),
        )
        sent.append("email")
    if slack_topic and sns is not None:
        sns.publish(TopicArn=slack_topic, Message=json.dumps(render_slack(report)))
        sent.append("slack")
    if not sent:
        log.info("no report channel configured; the report:\n%s", render_email(report))
    return sent
