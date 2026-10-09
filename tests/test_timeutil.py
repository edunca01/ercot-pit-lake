"""ERCOT time conventions: parsing, and both 2026 DST transitions at every grain we collect."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise

import pytest

from ercot_lake.timeutil import ct_to_utc
from ingest import timeutil as tu

FALL_BACK = date(2026, 11, 1)  # 01:00-02:00 CT happens twice: hour ending 2 repeats
SPRING_FORWARD = date(2026, 3, 8)  # 02:00-03:00 CT does not happen: no hour ending 3
ORDINARY = date(2026, 9, 3)


def test_parse_post_datetime_is_ct() -> None:
    # 12:32:53 CDT (UTC-5) on 2026-09-03
    assert tu.parse_post_datetime("2026-09-03T12:32:53.000") == datetime(
        2026, 9, 3, 17, 32, 53, tzinfo=UTC
    )
    assert tu.parse_post_datetime("2026-01-15T12:00:00") == datetime(2026, 1, 15, 18, 0, tzinfo=UTC)


def test_parse_post_datetime_rejects_garbage_and_the_gap() -> None:
    with pytest.raises(ValueError, match="unrecognised postDatetime"):
        tu.parse_post_datetime("yesterday")
    with pytest.raises(ValueError, match="spring-forward gap"):
        tu.parse_post_datetime("2026-03-08T02:30:00")


def test_parse_post_datetime_in_the_repeated_hour_is_the_first_occurrence() -> None:
    # The listing has no flag; see the module docstring.
    assert tu.parse_post_datetime("2026-11-01T01:30:00") == datetime(2026, 11, 1, 6, 30, tzinfo=UTC)


@pytest.mark.parametrize("text", ["2026-09-03", "09/03/2026", " 09/03/2026 "])
def test_parse_delivery_date_both_formats(text: str) -> None:
    assert tu.parse_delivery_date(text) == date(2026, 9, 3)


def test_parse_delivery_date_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="unrecognised delivery date"):
        tu.parse_delivery_date("2026/09/03")


@pytest.mark.parametrize(("text", "hour"), [("01:00", 1), ("1:00", 1), ("24:00", 24), (13, 13)])
def test_parse_hour_ending(text: str | int, hour: int) -> None:
    assert tu.parse_hour_ending(text) == hour


@pytest.mark.parametrize("bad", ["00:00", "25:00"])
def test_hour_ending_out_of_range(bad: str) -> None:
    with pytest.raises(ValueError, match="out of range"):
        tu.parse_hour_ending(bad)


def test_hourly_interval_start_summer() -> None:
    # HE 01:00 on 2026-09-03 starts 00:00 CDT = 05:00 UTC
    assert tu.interval_start_utc(ORDINARY, 1, 1, 60, repeated_hour=False) == datetime(
        2026, 9, 3, 5, 0, tzinfo=UTC
    )
    # HE 24:00 starts 23:00 CDT = 04:00 UTC next day
    assert tu.interval_start_utc(ORDINARY, 24, 1, 60, repeated_hour=False) == datetime(
        2026, 9, 4, 4, 0, tzinfo=UTC
    )


def test_quarter_hour_interval_start() -> None:
    # hour 12, interval 2 -> 11:15 CDT = 16:15 UTC
    assert tu.interval_start_utc(date(2026, 9, 4), 12, 2, 15, repeated_hour=False) == datetime(
        2026, 9, 4, 16, 15, tzinfo=UTC
    )


@pytest.mark.parametrize(
    ("hour", "interval", "minutes", "msg"),
    [
        (0, 1, 60, "hour out of range"),
        (25, 1, 60, "hour out of range"),
        (1, 5, 15, "interval 5 out of range"),
        (1, 0, 15, "interval 0 out of range"),
    ],
)
def test_interval_arguments_are_checked(hour: int, interval: int, minutes: int, msg: str) -> None:
    with pytest.raises(ValueError, match=msg):
        tu.interval_start_utc(ORDINARY, hour, interval, minutes, repeated_hour=False)


def test_sced_timestamp_is_floored_to_the_grain() -> None:
    # 15:07:19 CDT -> the 5-min interval starting 15:05 CDT = 20:05 UTC; both source formats
    expected = datetime(2026, 9, 16, 20, 5, tzinfo=UTC)
    assert tu.sced_interval_start_utc("09/16/2026 15:07:19", 5, repeated_hour=False) == expected
    assert tu.sced_interval_start_utc("2026-09-16T15:07:19", 5, repeated_hour=False) == expected
    with pytest.raises(ValueError, match="unrecognised SCED timestamp"):
        tu.sced_interval_start_utc("15:07", 5, repeated_hour=False)


# -- DST: single cases ---------------------------------------------------------------------


def test_fall_back_repeated_hour() -> None:
    # 01:00-02:00 local happens twice: first CDT (06:00Z), repeated CST (07:00Z).
    first = tu.interval_start_utc(FALL_BACK, 2, 1, 60, repeated_hour=False)
    second = tu.interval_start_utc(FALL_BACK, 2, 1, 60, repeated_hour=True)
    assert first == datetime(2026, 11, 1, 6, 0, tzinfo=UTC)
    assert second == datetime(2026, 11, 1, 7, 0, tzinfo=UTC)
    # HE 03:00 is unambiguous CST after the change: 02:00 CST = 08:00Z
    assert tu.interval_start_utc(FALL_BACK, 3, 1, 60, repeated_hour=False) == datetime(
        2026, 11, 1, 8, 0, tzinfo=UTC
    )


def test_spring_forward_day_has_23_hours() -> None:
    # HE 02:00 starts 01:00 CST (07:00Z); HE 04:00 starts 03:00 CDT (08:00Z).
    he2 = tu.interval_start_utc(SPRING_FORWARD, 2, 1, 60, repeated_hour=False)
    he4 = tu.interval_start_utc(SPRING_FORWARD, 4, 1, 60, repeated_hour=False)
    assert he2 == datetime(2026, 3, 8, 7, 0, tzinfo=UTC)
    assert he4 == datetime(2026, 3, 8, 8, 0, tzinfo=UTC)


def test_spring_forward_hour_ending_takes_either_label() -> None:
    # The hour from 01:00 CST to 03:00 CDT is "hour ending 02:00" in most reports and "hour
    # ending 03:00" (named by its end) in the solar report: one interval either way.
    he2 = tu.hour_ending_start_utc(SPRING_FORWARD, 2, 60, repeated_hour=False)
    he3 = tu.hour_ending_start_utc(SPRING_FORWARD, 3, 60, repeated_hour=False)
    he4 = tu.hour_ending_start_utc(SPRING_FORWARD, 4, 60, repeated_hour=False)
    assert he2 == he3 == datetime(2026, 3, 8, 7, 0, tzinfo=UTC)
    assert he4 == datetime(2026, 3, 8, 8, 0, tzinfo=UTC)


@pytest.mark.parametrize("day", [ORDINARY, FALL_BACK])
def test_hour_ending_is_unchanged_on_other_days(day: date) -> None:
    for hour in range(1, 25):
        assert tu.hour_ending_start_utc(
            day, hour, 60, repeated_hour=False
        ) == tu.interval_start_utc(day, hour, 1, 60, repeated_hour=False)


def test_spring_forward_hour_ending_03_is_never_the_repeated_hour() -> None:
    with pytest.raises(ValueError, match="not ambiguous"):
        tu.hour_ending_start_utc(SPRING_FORWARD, 3, 60, repeated_hour=True)


@pytest.mark.parametrize("minutes", [60, 15])
def test_the_skipped_hour_is_refused(minutes: int) -> None:
    # Without the check HE 03:00 would land on 08:00Z, colliding with HE 04:00.
    with pytest.raises(ValueError, match="spring-forward gap"):
        tu.interval_start_utc(SPRING_FORWARD, 3, 1, minutes, repeated_hour=False)


def test_a_repeat_flag_outside_the_fall_back_hour_is_refused() -> None:
    # Without the check the flag would be ignored and the row would collide with HE 05's.
    with pytest.raises(ValueError, match="not ambiguous"):
        tu.interval_start_utc(FALL_BACK, 5, 1, 60, repeated_hour=True)
    with pytest.raises(ValueError, match="not ambiguous"):
        tu.interval_start_utc(ORDINARY, 2, 1, 60, repeated_hour=True)


def test_sced_across_both_transitions() -> None:
    first = tu.sced_interval_start_utc("11/01/2026 01:05:19", 5, repeated_hour=False)
    second = tu.sced_interval_start_utc("11/01/2026 01:05:19", 5, repeated_hour=True)
    assert second - first == timedelta(hours=1)
    with pytest.raises(ValueError, match="spring-forward gap"):
        tu.sced_interval_start_utc("03/08/2026 02:05:19", 5, repeated_hour=False)


# -- DST: every interval of the day, as ERCOT labels it ------------------------------------


def _ercot_hours(day: date) -> Iterator[tuple[int, bool]]:
    """(hour ending, repeated flag) in the order ERCOT publishes a delivery day."""
    for he in range(1, 25):
        if day == SPRING_FORWARD and he == 3:
            continue
        yield he, False
        if day == FALL_BACK and he == 2:
            yield he, True


def _starts(day: date, minutes: int) -> list[datetime]:
    if minutes == 5:
        # SCED: stamped by run time (a few seconds past each 5-minute boundary), with the flag
        out = []
        for he, repeated in _ercot_hours(day):
            for i in range(12):
                stamp = f"{day:%m/%d/%Y} {he - 1:02d}:{i * 5:02d}:19"
                out.append(tu.sced_interval_start_utc(stamp, 5, repeated_hour=repeated))
        return out
    return [
        tu.interval_start_utc(day, he, i, minutes, repeated_hour=repeated)
        for he, repeated in _ercot_hours(day)
        for i in range(1, 60 // minutes + 1)
    ]


@pytest.mark.parametrize("minutes", [60, 15, 5], ids=["hourly", "15-min", "5-min"])
@pytest.mark.parametrize(
    ("day", "hours"),
    [(ORDINARY, 24), (FALL_BACK, 25), (SPRING_FORWARD, 23)],
    ids=["ordinary", "fall-back", "spring-forward"],
)
def test_a_delivery_day_is_gapless_and_never_overlaps(day: date, hours: int, minutes: int) -> None:
    starts = _starts(day, minutes)
    step = timedelta(minutes=minutes)
    assert len(starts) == hours * 60 // minutes
    assert starts[0] == ct_to_utc(datetime.combine(day, datetime.min.time()))
    assert all(b - a == step for a, b in pairwise(starts))
    next_midnight = ct_to_utc(datetime.combine(day + timedelta(days=1), datetime.min.time()))
    assert starts[-1] + step == next_midnight
