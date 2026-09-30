# 0005. Ingest from archive documents, not the row API

Date: 2026-09-04
Status: accepted

## Context

Point-in-time is a non-negotiable: every curated row needs `posted_at`, ERCOT's posting time.
ADR 0004 found that the JSON row endpoints (`/np6-905-cd/spp_node_zone_hub` etc.) return rows
with no posting timestamp at all. They also page at 50–1000 rows under a rate limit that
returns 429 within a handful of requests; one day of `np6-905-cd` is ~218k rows.

Verified 2026-09-04 against the live API:

- `GET /archive/<ID>?postDatetimeFrom=&postDatetimeTo=&page=&size=` lists one document per
  posting with `docId`, `postDatetime` (CT, no offset) and `friendlyName`, sorted
  `postDatetime DESC`.
- `GET /archive/<ID>?download=<docId>` returns the posting as a zip holding one CSV. The inner
  filename carries the posting timestamp: `cdr.<reportTypeId>.<...>.<YYYYMMDD>.<HHMMSS>.<name>.csv`
  and matches the listing's `postDatetime` to the second.
- `GET /bundle/<id>` lists **monthly** zips of every posting for that product
  (`SPPHLZNP6905_2026-06`), downloadable the same way. Bundles lag roughly two months.
- DAM products post once per day between ~12:30 and ~13:00 CT (observed on 5 days), earlier
  than the ~13:30 that was assumed. A 15-min RT posting is ~8 KB.

## Decision

1. **The unit of ingestion is an ERCOT document**, not a row query. Live ingest lists archive
   documents with `postDatetime > watermark`, downloads each, and transforms the CSV inside.
   Backfill iterates the same documents over an explicit posting-time window (individual docs
   for recent months, monthly bundles for older ones). One code path: `run_product()` consumes
   an iterator of documents and does not know which listing produced them.
2. **`posted_at` comes from the document**, parsed from the inner CSV filename, with the
   listing's `postDatetime` as fallback. Both are CT and converted to UTC.
3. **Raw is the zip as received**: `raw/<product>/date=<posting date CT>/posted=<UTC stamp>.zip`.
   The `date=` partition on raw is the *posting* date, because delivery date is only known after
   parsing and raw must land before parsing can fail. Curated stays partitioned by *delivery*
   date (`docs/CONTRACT.md` §2). The stamp is basic ISO 8601 (`20260903T173253Z`) so keys carry no colons.
4. **Watermark advances per document**, after that document's raw and curated writes succeed.
   A failure leaves the watermark at the last good document, so the next run resumes there.
   So a failure never moves the watermark past unwritten data, and a 600 s Lambda can drain
   a backlog over several runs.
5. **The row API stays in the client** as a query surface (spot checks, the `samples/` fixtures)
   and the transforms accept both API JSON and archive CSV.
6. **Schema drift fails loudly.** A transform declares the exact source columns it expects; a
   missing or unexpected column raises `SchemaDriftError`. Fixing it means bumping
   `schema_version` and a CHANGELOG entry, never a silent coercion.
7. **DAM SPP has no settlement point type** in the source. `settlement_point_type` is nullable and
   is NULL for `np4-190-cd`; consumers needing it join to `np6-905-cd`'s point list. Deriving it
   from the name prefix was rejected: RT data shows types (`PUN`, `LCCRN`, `PCCRN`) that a
   prefix rule cannot recover.
8. **DST**: ERCOT's `DSTFlag` / `RepeatedHourFlag` = Y marks the *second* occurrence of the
   repeated hour on the fall-back day. It maps to `fold=1` in `zoneinfo`. To be confirmed
   against November 2026 data; the assumption is isolated in `ingest/timeutil.py`.

9. **SPP business key includes the type.** Verified in live data: every load zone appears
   twice per interval in `np6-905-cd`, as `LZ` and `LZEW` (energy-weighted), with different
   prices. Readers dedupe on (`interval_start`, `settlement_point`, `settlement_point_type`);
   MCPC tables on (`interval_start`, `as_type`). Defined once in `ercot_lake.contract`.

## Consequences

- Live ingest for a 15-min product is two requests per run (list, download). Backfilling a
  year is ~12 bundle downloads per product plus individual docs for the trailing months.
- `raw/` holds zips rather than JSON (`docs/CONTRACT.md` §2).
- Reposts and corrections appear as additional documents with later `posted_at` and land as
  extra `part-*.parquet` files in the same delivery-date partition. Readers dedupe on the
  business key taking the latest `posted_at <= as_of`.
- The client paces requests (`min_interval_s`) instead of scripts sleeping.
