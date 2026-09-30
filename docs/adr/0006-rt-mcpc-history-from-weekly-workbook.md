# 0006. RT MCPC gaps are filled from ERCOT's weekly historical workbook

Date: 2026-09-15
Status: accepted

## Context

The 2025-12-05 → 2026-09-14 backfill found that ERCOT published **no** `np6-331-cd`
postings between 2026-07-30 13:47 CT and 2026-08-27 14:02 CT: the archive listing has 56
documents for a 28-day window where `np6-905-cd` has 2,732, the monthly bundle for 2026-08
holds only Aug 27–31, and the row API returns zero rows for those delivery days. Two further
single intervals were absent from the archive (2025-12-05 00:00 CT, 2026-01-28 15:30 CT).

`NP6-796-ER` ("Historical RTM Clearing Prices for Capacity by 15-Minute Settlement Interval",
a cumulative xlsx per year, re-posted weekly, ADR 0004) contains every one of those days,
complete (96 intervals × 5 AS types). Its columns are the live archive CSV's columns in a
different order, with Delivery Date as a datetime cell. The 2025 workbook's first row is
2025-12-05 hour 1: there is no RT MCPC before RTC+B, so history starts there by construction.

## Decision

1. The weekly workbook is a **third document source** (`backfill --source hist`,
   `Product.fallback_archive_id`). One workbook posting = one `Doc`, so `raw/` holds the zip
   exactly as received, keyed by the workbook's posting stamp (`rpt.….20260830.100041.…xlsx`).
2. `iter_csv_members` converts each month sheet of a `HIST_15Min_RTM_MCPC` workbook into a CSV
   member with the archive header, so the unchanged `np6-331-cd` transform produces the curated
   rows. Other workbooks are ignored.
3. **Point in time stays honest.** Rows from a workbook carry `posted_at` = the workbook's
   posting time, and the fill uses the *earliest* weekly workbook that contained the missing
   days (2026-08-30 for Jul 30–Aug 27; 2025-12-08 and 2026-02-01 for the singles). A consumer
   with `as_of` before that date correctly sees the gap ERCOT actually had.
4. `Run.delivery_range` (`--delivery-from/--delivery-to`) restricts a cumulative workbook to
   the delivery days being filled, so days that have regular postings keep them as the only
   rows until `as_of` passes the workbook's posting time. Filtering is per delivery day, not
   per interval: the two single-interval fills rewrote their whole day from the workbook.
5. Values are not reconciled between sources. Spot checks agree; the workbook is ERCOT's own
   restatement of the same clearing prices.

## Consequences

- RT MCPC coverage is complete from 2025-12-05. The daily report and `scripts/coverage.py`
  count workbook rows like any other.
- `openpyxl` is a runtime dependency (only imported on the history path).
- If ERCOT skips RT MCPC postings again, the fix is a one-line `backfill --source hist` run
  after the next weekly workbook; the freshness alarm will have fired in the meantime.
- Readers that care about provenance can distinguish sources by `posted_at` clustering (a
  whole day sharing one `posted_at` at ~10:00 CT on a Sunday is a workbook fill); a
  `source_doc` column is a possible schema_version 2 addition if that ever matters.
