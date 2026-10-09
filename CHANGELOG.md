# Changelog

Format: [Keep a Changelog](https://keepachangelog.com). Versioning: SemVer on the lake contract
(`lake-vX.Y.Z`, docs/CONTRACT.md §1). Curated-schema changes always go under **Schema**.

## [Unreleased]

### Fixed
- The solar production report (`np4-737-cd`) labels the spring-forward hour (01:00 CST to
  03:00 CDT) hour ending 03:00, where the other hourly reports call it 02:00. Ingestion
  refused those postings as naming a time that does not exist; both labels now give the same
  `interval_start`. A posting that used both would repeat a business key and is still refused.

## [1.0.0] - 2026-09-30

The first public release: the lake contract 1.0.0 (`docs/CONTRACT.md`) and the `ercot-lake`
read library, tagged `lake-v1.0.0`.

### Schema
- Three curated tables, `spp`, `mcpc` and `series`, schema version 1, holding ten products:
  day-ahead and real-time settlement point prices, day-ahead and real-time ancillary-service
  clearing prices, SCED LMPs and system lambda, the seven-day load forecast, wind and solar
  production (actual and forecast), and actual load by weather zone.
- Every row carries `interval_start`, `posted_at` and `ingested_at`. A business key appears
  at most once per posting (ingestion refuses a posting that repeats one), so the
  point-in-time read (`posted_at <= as_of`, latest posting per key) is deterministic.
- `manifests/<product>/latest.json` and `manifests/_catalog.json`; a product removed from
  collection stays in the catalog as `live: false` while the lake holds its data.
- SSM parameters under `/ercot-pit-lake/` for consumers in the same account.

### Added
- `ercot-lake`: `LakeReader` (point-in-time reads by date or range and `postings`, `as_of`
  required, pyarrow results, local or S3 through DuckDB), the contract module, CT/UTC helpers
  and the catalog model.
- Ingestion from ERCOT's archive documents, live and backfill through one loop (archive,
  monthly bundles, the yearly RT clearing-price workbook), with watermarks, immutable raw
  postings, strict DST handling and schema-drift errors. Products are declared in
  `config.yaml`, each tested against a committed sample.
- Compaction of small curated files without changing a row, resuming across runs; `make
  verify` checks any lake against the contract.
- Freshness metrics for every product and for declared heartbeats of other systems, two
  alarms, and a daily report with sections other systems can fill; email and Slack optional.
- Terraform modules for storage, compute, observability and CI roles, an example root, and
  tests against a mocked AWS provider; the Lambda container image.
- ADRs 0001-0008.
