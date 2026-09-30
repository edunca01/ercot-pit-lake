# 0003. Immutable raw and point-in-time curated

Date: 2026-09-03
Status: accepted

## Context

ERCOT reposts and corrects prices. A decision made at 10:03 must be judged against what could
be known at 10:03, not against a corrected value published later. A backtest that ignores this
is looking at the future. Backfilled history and live ingest must also look the same to
consumers.

## Decision

- Every ERCOT posting is written to `raw/` exactly as received, before it is parsed. Nothing
  changes or deletes it. ADR 0005 fixes its form, the posting's zip.
- Every curated row carries `interval_start`, `posted_at` (ERCOT's posting time), `ingested_at`
  (ours) and `source`.
- The same input maps to the same key, so re-runs overwrite. A product's watermark moves only
  after a success.
- Live ingest and backfill share one transform per product and one ingest loop.

## Consequences

- Readers keep rows with `posted_at <= as_of` and dedupe on the business key, taking the
  latest posting (`docs/CONTRACT.md` §7). `ercot_lake.LakeReader` applies this in one place
  and has no method without `as_of`.
- A curated partition may hold several `part-<posted>.parquet` files per delivery day.
  Compaction merges them into one file without changing any row. Point-in-time answers are the
  same before and after.
- `raw/` grows without bound. It moves to Standard-IA at 30 days and never expires, so any
  curated table can be rebuilt from it.
