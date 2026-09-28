from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime, timedelta

import openpyxl

from ingest.archive import CsvMember, iter_csv_members, posted_local_from_name


def _zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_posted_local_from_filename() -> None:
    name = "cdr.00012301.0000000000000000.20260904.113201.SPPHLZNP6905_20260904_1130.csv"
    assert posted_local_from_name(name) == datetime(2026, 9, 4, 11, 32, 1)
    assert posted_local_from_name("nothing_here.csv") is None


def test_single_posting_zip() -> None:
    blob = _zip({"cdr.1.0.20260903.123253.X.csv": b"\xef\xbb\xbfa,b\n1,2\n"})
    members = iter_csv_members(blob)
    assert len(members) == 1
    assert members[0].text == "a,b\n1,2\n"  # BOM stripped
    listed = datetime(2026, 9, 3, 17, 32, 53, tzinfo=UTC)
    assert members[0].posted_at(listed) == listed  # 12:32:53 CDT


def test_nested_monthly_bundle_is_flattened_in_order() -> None:
    inner1 = _zip({"cdr.1.0.20260601.000201.A.csv": b"a\n1\n"})
    inner2 = _zip({"cdr.1.0.20260601.001701.A.csv": b"a\n2\n"})
    bundle = _zip({"first.zip": inner1, "second.zip": inner2, "readme.txt": b"ignored"})
    members = iter_csv_members(bundle)
    assert [m.text for m in members] == ["a\n1\n", "a\n2\n"]
    assert [m.posted_local for m in members] == [
        datetime(2026, 6, 1, 0, 2, 1),
        datetime(2026, 6, 1, 0, 17, 1),
    ]


def test_unknown_xlsx_is_ignored() -> None:
    assert iter_csv_members(_zip({"rpt.1.0.20260830.100040.OTHER.xlsx": b"PK"})) == []


def test_a_name_without_a_stamp_takes_the_listing_time() -> None:
    listed = datetime(2026, 9, 3, 17, 0, tzinfo=UTC)
    assert CsvMember("plain.csv", "", None).posted_at(listed) == listed


def test_filename_stamp_in_the_repeated_hour_follows_the_listing() -> None:
    """01:30:20 on 2026-11-01 happens twice; the stamp alone cannot say which. The listing
    (already resolved in posting order) decides."""
    member = CsvMember("cdr.1.0.20261101.013020.X.csv", "", datetime(2026, 11, 1, 1, 30, 20))
    first = datetime(2026, 11, 1, 6, 30, 20, tzinfo=UTC)  # CDT
    second = datetime(2026, 11, 1, 7, 30, 20, tzinfo=UTC)  # CST
    assert member.posted_at(first) == first
    assert member.posted_at(second) == second
    # a listing a few seconds off the stamp still picks the right pass
    assert member.posted_at(second + timedelta(seconds=40)) == second


# -- the yearly RT clearing-price workbook -------------------------------------------------

XLSX_NAME = "rpt.00025570.0000000000000000.20260830.100041.HIST_15Min_RTM_MCPC_2026.xlsx"


def _workbook() -> bytes:
    wb = openpyxl.Workbook()
    info = wb.active
    assert info is not None
    info.title = "Report Info"
    info.append(["Historical RTM Clearing Prices for Capacity", None, None])
    for month, days in (("Jul", [30, 31]), ("Aug", [1, 2])):
        ws = wb.create_sheet(month)
        ws.append([f"Historical ... - {month} 2026", None, None, None, None, None])
        ws.append(
            [
                "Delivery Date",
                "Delivery Hour",
                "Delivery Interval",
                "AS Type",
                "MCPC",
                "Repeated Hour Flag",
            ]
        )
        m = 7 if month == "Jul" else 8
        for d in days:
            for hour in (1, 2):
                for interval in (1, 2, 3, 4):
                    for as_type, price in (("REGUP", 1.5), ("RRS", 0.25)):
                        ws.append([datetime(2026, m, d), hour, interval, as_type, price, "N"])
        ws.append([None, None, None, None, None, None])
        ws.append(["Note: prices in $/MW", None, None, None, None, None])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_xlsx_sheets_become_archive_shaped_csv_members() -> None:
    members = iter_csv_members(_zip({XLSX_NAME: _workbook()}))
    assert [m.name.rsplit(".", 2)[-2] for m in members] == ["Jul", "Aug"]  # info sheet skipped
    jul = members[0]
    assert jul.posted_at(datetime(2026, 8, 30, 15, 0, tzinfo=UTC)) == datetime(
        2026, 8, 30, 15, 0, 41, tzinfo=UTC
    )  # 10:00:41 CDT
    lines = jul.text.splitlines()
    assert lines[0] == "DeliveryDate,DeliveryHour,DeliveryInterval,RepeatedHourFlag,ASType,MCPC"
    assert lines[1] == "07/30/2026,1,1,N,REGUP,1.5"
    assert len(lines) == 1 + 2 * 2 * 4 * 2  # header + days x hours x intervals x AS types
