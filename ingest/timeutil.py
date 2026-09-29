"""ERCOT time conventions -> UTC.

All ERCOT timestamps are Central Prevailing Time with no offset. Delivery intervals are given as
(delivery date, hour ending 1..24, interval 1..n) plus a flag marking the repeated hour on the
fall-back day. This module is the only place those conventions are interpreted; the generic
CT/UTC helpers live in ``ercot_lake.timeutil``.

The flag (``DSTFlag`` / ``RepeatedHourFlag`` = Y) marks the *second* occurrence of the repeated
hour, i.e. ``fold=1``. A local time that cannot exist (the spring-forward gap) or a flag on an
hour that is not repeated raises: either would silently give two intervals the same UTC start,
and one of them would be lost when readers dedupe on the business key.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from ercot_lake.timeutil import ct_to_utc, utc_to_ct

_POST_FORMATS = ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S")
_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y")
_SCED_FORMATS = ("%m/%d/%Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S")


def local_to_utc(local: datetime, *, repeated_hour: bool) -> datetime:
    """A naive CT wall-clock time from ERCOT -> aware UTC, refusing times that are not real."""
    utc = ct_to_utc(local, repeated_hour=repeated_hour)
    if utc_to_ct(utc).replace(tzinfo=None) != local:
        msg = f"{local:%Y-%m-%d %H:%M} does not exist in Central time (spring-forward gap)"
        raise ValueError(msg)
    if repeated_hour and ct_to_utc(local) == utc:
        msg = f"{local:%Y-%m-%d %H:%M} is flagged as the repeated hour but is not ambiguous"
        raise ValueError(msg)
    return utc


def parse_post_local(text: str) -> datetime:
    """ERCOT listing ``postDatetime`` (``2026-09-03T12:32:53.000``) -> naive CT wall-clock time.

    The listing carries no DST flag, so inside the repeated fall-back hour the same text means
    two instants; ``ingest.ercot_api`` tells them apart by listing order.
    """
    for fmt in _POST_FORMATS:
        try:
            return datetime.strptime(text, fmt)  # naive CT by definition
        except ValueError:
            continue
    msg = f"unrecognised postDatetime {text!r}"
    raise ValueError(msg)


def parse_post_datetime(text: str) -> datetime:
    """``postDatetime`` -> aware UTC, reading the repeated fall-back hour as its first pass."""
    return local_to_utc(parse_post_local(text), repeated_hour=False)


def is_repeated_local(local: datetime) -> bool:
    """True when a CT wall-clock time happens twice (the fall-back hour)."""
    return ct_to_utc(local) != ct_to_utc(local, repeated_hour=True)


def parse_delivery_date(text: str) -> date:
    """``2026-09-03`` (API) or ``09/03/2026`` (archive CSV)."""
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text.strip(), fmt).date()
        except ValueError:
            continue
    msg = f"unrecognised delivery date {text!r}"
    raise ValueError(msg)


def parse_hour_ending(text: str | int) -> int:
    """``'01:00'`` / ``'24:00'`` / ``'1:00'`` / ``1`` -> 1..24."""
    hour = int(str(text).split(":")[0])
    if not 1 <= hour <= 24:
        msg = f"hour ending out of range: {text!r}"
        raise ValueError(msg)
    return hour


def interval_start_utc(
    delivery_date: date,
    hour: int,
    interval: int,
    interval_minutes: int,
    *,
    repeated_hour: bool,
) -> datetime:
    """Start of delivery interval ``interval`` (1-based) within delivery ``hour`` (1..24 = hour
    ending), as aware UTC. In the fall hour ending 2 (local 01:00-02:00) happens twice; in the
    spring hour ending 3 (local 02:00-03:00) does not happen at all."""
    if not 1 <= hour <= 24:
        msg = f"hour out of range: {hour}"
        raise ValueError(msg)
    per_hour = 60 // interval_minutes
    if not 1 <= interval <= per_hour:
        msg = f"interval {interval} out of range for {interval_minutes}-minute product"
        raise ValueError(msg)
    local = datetime.combine(delivery_date, time.min) + timedelta(
        hours=hour - 1, minutes=(interval - 1) * interval_minutes
    )
    return local_to_utc(local, repeated_hour=repeated_hour)


def sced_interval_start_utc(text: str, interval_minutes: int, *, repeated_hour: bool) -> datetime:
    """SCED runs are stamped by their run time in CT (``09/16/2026 15:05:19`` in the archive,
    ``2026-09-16T15:05:19`` on the API); the result applies to the interval that starts at the
    stamp floored to the product's grain."""
    raw = text.strip()
    for fmt in _SCED_FORMATS:
        try:
            local = datetime.strptime(raw, fmt)
            break
        except ValueError:
            continue
    else:
        msg = f"unrecognised SCED timestamp {text!r}"
        raise ValueError(msg)
    floored = local.replace(
        minute=local.minute - local.minute % interval_minutes, second=0, microsecond=0
    )
    return local_to_utc(floored, repeated_hour=repeated_hour)
