"""CT/UTC helpers. The DST cases pin down both transitions of 2026."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from ercot_lake import timeutil as tu


def test_ct_to_utc_summer_and_winter() -> None:
    assert tu.ct_to_utc(datetime(2026, 9, 3, 12, 0)) == datetime(2026, 9, 3, 17, 0, tzinfo=UTC)
    assert tu.ct_to_utc(datetime(2026, 1, 15, 12, 0)) == datetime(2026, 1, 15, 18, 0, tzinfo=UTC)


def test_fall_back_repeated_hour() -> None:
    # 2026-11-01: 01:00-02:00 local happens twice, first CDT (06:00Z) then CST (07:00Z).
    local = datetime(2026, 11, 1, 1, 0)
    assert tu.ct_to_utc(local) == datetime(2026, 11, 1, 6, 0, tzinfo=UTC)
    assert tu.ct_to_utc(local, repeated_hour=True) == datetime(2026, 11, 1, 7, 0, tzinfo=UTC)


def test_spring_forward() -> None:
    # 2026-03-08: 01:59 CST is followed by 03:00 CDT, both 07:xx/08:00Z.
    assert tu.ct_to_utc(datetime(2026, 3, 8, 1, 0)) == datetime(2026, 3, 8, 7, 0, tzinfo=UTC)
    assert tu.ct_to_utc(datetime(2026, 3, 8, 3, 0)) == datetime(2026, 3, 8, 8, 0, tzinfo=UTC)


def test_naive_and_aware_are_checked() -> None:
    with pytest.raises(ValueError, match="naive"):
        tu.ct_to_utc(datetime(2026, 9, 3, 12, 0, tzinfo=UTC))
    with pytest.raises(ValueError, match="aware"):
        tu.utc_to_ct(datetime(2026, 9, 3, 12, 0))


def test_utc_to_ct() -> None:
    ct = tu.utc_to_ct(datetime(2026, 9, 3, 17, 0, tzinfo=UTC))
    assert (ct.hour, ct.utcoffset().total_seconds() / 3600) == (12, -5)  # type: ignore[union-attr]


def test_delivery_date_ct_crosses_midnight() -> None:
    # 04:30Z on Sep 4 is 23:30 CDT on Sep 3
    assert tu.delivery_date_ct(datetime(2026, 9, 4, 4, 30, tzinfo=UTC)) == date(2026, 9, 3)


def test_stamp_round_trip() -> None:
    ts = datetime(2026, 9, 3, 17, 32, 53, tzinfo=UTC)
    assert tu.stamp(ts) == "20260903T173253Z"
    assert tu.parse_stamp("20260903T173253Z") == ts
    with pytest.raises(ValueError, match="UTC"):
        tu.stamp(ts.astimezone(tu.CT))


def test_now_utc() -> None:
    now = tu.now_utc()
    assert now.tzinfo is UTC
    assert now.microsecond == 0
