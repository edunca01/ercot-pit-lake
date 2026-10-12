# 0009. A family of point-in-time lakes, and a forecaster that reads them

Date: 2026-09-30
Status: accepted

## Context

The ERCOT lake exists so that a model can be trained and judged on only what was known at the
time. Its first serious consumer is a price and load forecaster. Weather drives both load and
renewable output, so the forecaster also needs weather forecasts. Those weather forecasts
must obey the same rule: a feature for a decision at 10:03 may only use a forecast issued
before 10:03.

Weather comes from Open-Meteo (free for non-commercial use, CC BY 4.0). Its APIs differ in
what they can honestly provide:

- the forecast API returns the latest forecast. Polled on a schedule, it builds true
  vintages from the day collection starts;
- the Previous Runs API returns, for each valid time, the value forecast one, two, … days
  earlier: vintages by lead day, usable for backfill;
- the Historical Forecast API stitches the first hours of successive runs into one series.
  That is a very-short-lead forecast at every point, so as a feature for anything further
  ahead it leaks the future.

The forecaster is trained on a separate local machine that reads the lakes from S3, and is
served on a schedule in the cloud. Its forecasts may be shown by a frontend or feed other
systems later. Each is a separate concern.

## Decision

1. **Three repositories, one pattern.** `ercot-pit-lake` (this one), `weather-lake` and
   `ercot-forecaster`. Each is public code with a private deployment (ADR 0007), owns its
   Terraform state and bucket, and publishes cross-repository values through SSM Parameter
   Store under its own prefix. A frontend, if built, is a fourth consumer repository.
2. **Every lake keeps the same point-in-time contract shape:**
   - raw kept as received, before parsing;
   - curated Parquet with `interval_start`, `posted_at` and `ingested_at`;
   - a business key unique per posting;
   - the read rule `posted_at <= as_of`, then the latest posting per key;
   - manifests, a catalog, compaction, freshness and a `verify` command.

   Each lake versions its own contract and ships its own read library.
3. **In weather-lake, `posted_at` is never earlier than the forecast could have been known:**
   - live polling: `posted_at` is the fetch time;
   - Previous Runs backfill: `posted_at` is the valid time minus the lead, plus the source's
     publication lag. When the lag is uncertain, it errs later;
   - the Historical Forecast API is not used for forecast features;
   - observed weather is its own table, dated by when it was published.
4. **Weather is joined to ERCOT on ERCOT's weather zones.** weather-lake publishes zone
   aggregates under the eight zone names the ERCOT load series use (`Coast` … `West`). The
   forecaster joins on (`interval_start`, zone) and owns no mapping. Grids are not stored in
   curated; only the points that make up each zone are.
5. **The forecaster writes only to its own bucket:**
   - model artifacts record the training cutoff and the contract version of each lake read;
   - forecasts are themselves published point-in-time, with the issue time as `posted_at`,
     so any downstream replay sees what was forecast at the time.

   Nothing outside a lake writes to it (ADR 0007).
6. **Training reads a local mirror.** The training machine syncs the lakes' `curated/` and
   `manifests/` from S3 with read-only credentials, and reads the mirror with each lake's
   library. The read rule is the same on a local path. One sync costs a request per file
   rather than per query.
7. **Monitoring reuses the lake's hooks.** The forecaster's heartbeat is an `extra_watches`
   entry in the ERCOT lake, and its accuracy summary is a `report_sections` entry. The lake
   still names no consumer.
8. **Code is copied between lakes first and extracted later.** weather-lake starts from this
   repository's patterns. A shared point-in-time core (read rule, catalog with business keys,
   writer, compaction) is extracted once both lakes run, when the common parts are known
   rather than guessed.

## Consequences

- Leakage is prevented by the lakes, not by the forecaster's care. A backtest can only see
  what each lake held at its `as_of`.
- Weather history for honest training is limited to what Previous Runs covers plus what live
  polling has collected. Deeper history would mean a different source, or accepting leaky
  features; that would be a new ADR, never a quiet change.
- One reader across two lakes needs the catalog to carry each table's business key, a minor
  contract change for this lake after v1.0.0. Until then, the forecaster uses both libraries
  side by side.
- Three repositories, three deployments and three sets of alarms stay small because each
  follows the same serverless, free-tier shape (ADR 0002). Training compute is local and
  costs nothing in the cloud.
- The public repositories must credit Open-Meteo (CC BY 4.0) wherever weather data or
  samples appear.
