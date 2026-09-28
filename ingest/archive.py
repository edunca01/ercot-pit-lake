"""ERCOT archive documents: zip -> CSV members with the posting time from their filename.

Individual postings are a zip with one CSV. Monthly bundles are a zip of those zips. Both are
flattened here. The inner filename encodes the posting time in CT, without a DST flag:
``cdr.00012301.0000000000000000.20260904.113201.SPPHLZNP6905_20260904_1130.csv``.

ERCOT's yearly workbook of 15-minute RT clearing prices (``…HIST_15Min_RTM_MCPC_<year>.xlsx``,
one sheet per month) is converted sheet by sheet into CSV members with the same header as the
live archive CSV, so the ordinary transform reads it and history needs no second code path.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime

from ercot_lake.timeutil import ct_to_utc

_STAMP_RE = re.compile(r"\.(\d{8})\.(\d{6})\.")


@dataclass(frozen=True)
class CsvMember:
    name: str
    text: str
    posted_local: datetime | None  # naive CT from the filename; None if the name has no stamp

    def posted_at(self, listed_at: datetime) -> datetime:
        """The member's posting time in UTC, or ``listed_at`` when the name has no stamp.

        ``listed_at`` is the posting time the archive listing gave for the document. Inside the
        repeated fall-back hour the filename stamp alone names two instants; the one nearer the
        listing is the right one (the stamp and the listing agree to the second otherwise).
        """
        if self.posted_local is None:
            return listed_at
        first = ct_to_utc(self.posted_local)
        second = ct_to_utc(self.posted_local, repeated_hour=True)
        return min((first, second), key=lambda t: abs(t - listed_at))


def posted_local_from_name(name: str) -> datetime | None:
    m = _STAMP_RE.search(name)
    if not m:
        return None
    return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")  # naive CT by definition


def iter_csv_members(zip_bytes: bytes) -> list[CsvMember]:
    """All CSV members of a possibly nested zip, in archive order; workbooks are converted."""
    out: list[CsvMember] = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for name in zf.namelist():
            lower = name.lower()
            if lower.endswith(".csv"):
                text = zf.read(name).decode("utf-8-sig")  # ERCOT CSVs start with a BOM
                out.append(CsvMember(name, text, posted_local_from_name(name)))
            elif lower.endswith(".zip"):
                out.extend(iter_csv_members(zf.read(name)))
            elif lower.endswith(".xlsx") and "hist_15min_rtm_mcpc" in lower:
                out.extend(hist_xlsx_members(name, zf.read(name)))
    return out


# The yearly RT clearing-price workbook: sheet columns, in order, and the archive CSV header
# they map to. Delivery Date is a datetime cell; the CSV wants MM/DD/YYYY.
_HIST_SHEET_HEADER = (
    "Delivery Date",
    "Delivery Hour",
    "Delivery Interval",
    "AS Type",
    "MCPC",
    "Repeated Hour Flag",
)
_HIST_CSV_HEADER = (
    "DeliveryDate",
    "DeliveryHour",
    "DeliveryInterval",
    "RepeatedHourFlag",
    "ASType",
    "MCPC",
)


def hist_xlsx_members(name: str, data: bytes) -> list[CsvMember]:
    """One CSV member per month sheet of a HIST_15Min_RTM_MCPC workbook."""
    import openpyxl  # noqa: PLC0415  (heavy import; only the history path needs it)

    posted = posted_local_from_name(name)
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    out: list[CsvMember] = []
    for ws in wb.worksheets:
        header_seen = False
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(_HIST_CSV_HEADER)
        n = 0
        for r in ws.iter_rows(values_only=True):
            if not header_seen:
                cells = tuple(str(c).strip() if c is not None else "" for c in r[:6])
                header_seen = cells == _HIST_SHEET_HEADER
                continue
            if not isinstance(r[0], datetime):
                continue  # trailing blanks and notes
            day, hour, interval, as_type, mcpc, flag = r[:6]
            w.writerow([f"{day:%m/%d/%Y}", int(str(hour)), int(str(interval)), flag, as_type, mcpc])
            n += 1
        if header_seen and n:
            out.append(CsvMember(f"{name}.{ws.title}.csv", buf.getvalue(), posted))
    return out
