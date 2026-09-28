"""Time helpers shared by the pipeline and its consumers.

Everything in the lake is stored in UTC. ERCOT publishes in Central Prevailing Time, and curated
partitions are named by the delivery date in that zone, so these conversions sit next to the
contract rather than in each consumer.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")


def ct_to_utc(naive_local: datetime, *, repeated_hour: bool = False) -> datetime:
    """A naive Central time -> aware UTC. On the fall-back day the hour 01:00-02:00 happens
    twice; ``repeated_hour=True`` selects the second occurrence (``fold=1``)."""
    if naive_local.tzinfo is not None:
        msg = "expected a naive CT datetime"
        raise ValueError(msg)
    return naive_local.replace(tzinfo=CT, fold=1 if repeated_hour else 0).astimezone(UTC)


def utc_to_ct(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        msg = "expected an aware datetime"
        raise ValueError(msg)
    return ts.astimezone(CT)


def delivery_date_ct(ts_utc: datetime) -> date:
    """The curated partition an interval belongs to: the CT calendar date it starts on."""
    return utc_to_ct(ts_utc).date()


def stamp(ts_utc: datetime) -> str:
    """Basic ISO 8601 for object keys, ``20260903T173253Z``: sortable and free of colons."""
    if ts_utc.tzinfo is None or ts_utc.utcoffset() != timedelta(0):
        msg = "stamp() expects a UTC datetime"
        raise ValueError(msg)
    return ts_utc.strftime("%Y%m%dT%H%M%SZ")


def parse_stamp(text: str) -> datetime:
    return datetime.strptime(text, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)
