# Lake contract

Contract version: **1.0.0** (`ercot_lake.contract.CONTRACT_VERSION`). Curated schema version: **1**
(`SCHEMA_VERSION`, stored on every row).

This is the only interface between this repository and its consumers. Anything not written
here (module names, the watermark table, raw zip internals, log formats) is private and may
change without notice.

`tests/test_contract_doc.py` parses the tables in §3–§4 and compares them to the code.
Keep their format: one row per column, backticked names.

## 1. Versioning

- **Contract version** (SemVer, released as `lake-vX.Y.Z`):
  - **major**: a column removed, renamed or retyped; a key format changed; a business key
    changed.
  - **minor**: a product or a nullable column added.
  - **patch**: documentation only.
- **`schema_version`** (integer, per row) goes up whenever a table's columns change. Readers
  may meet several versions in one partition. The reader library normalizes every version it
  knows and refuses unknown ones.
- A major bump is announced in CHANGELOG under **Schema**, with a migration note.

## 2. Layout

```
<root>/
  raw/<product>/date=<posting date, CT>/posted=<stamp>.zip
  curated/<product>/date=<delivery date, CT>/part-<stamp>.parquet
  curated/<product>/date=<delivery date, CT>/merged-<stamp>.parquet
  manifests/<product>/latest.json
  manifests/_catalog.json
```

- `<root>` is `s3://<bucket>` deployed, or any local directory.
- `<product>` is the lower-case ERCOT report ID, e.g. `np6-905-cd`.
- `<stamp>` is a UTC basic ISO 8601 time, `YYYYMMDDTHHMMSSZ` (e.g. `20260903T173253Z`).
  - On `part-` files it is ERCOT's posting time.
  - On `merged-` files it is the compaction time.
- A `date=` value is `YYYY-MM-DD`.
  - Raw is partitioned by **posting** date, because raw lands before parsing.
  - Curated is partitioned by **delivery** date in America/Chicago.
- A posting that spans several delivery days writes one `part-` file into each of them.
- Consumers must treat the set of files in a curated partition as unordered and changing:
  compaction replaces `part-` files with a `merged-` file. Rows are never changed or dropped,
  so a point-in-time read (§7) gives the same answer before and after.
- `raw/` is readable only by the pipeline. It is not part of the consumer IAM policy.

## 3. Common columns (every curated table)

| column | type | nullable | meaning |
|---|---|---|---|
| `interval_start` | timestamp[us, UTC] | no | start of the delivery interval |
| `interval_minutes` | int32 | no | 60 (DAM, hourly), 15 (RT), 5 (SCED) |
| `posted_at` | timestamp[us, UTC] | no | ERCOT's posting time of the document the row came from |
| `ingested_at` | timestamp[us, UTC] | no | when this pipeline wrote the row |
| `source` | string | no | `api` (live document) or `archive` (bundle, workbook, backfill) |
| `schema_version` | int32 | no | curated schema version of the row |

## 4. Tables

The products listed under each table are the ones the lake collects today. New products join
an existing table (a minor release, §1); the catalog (§6) is the authoritative list.

Within one posting a business key appears at most once. Ingestion refuses a posting that
repeats one, so it is never written; a single `posted_at` therefore never holds two values for
the same key.

### `spp`: `np4-190-cd`, `np6-905-cd`, `np6-788-cd`

| column | type | nullable | meaning |
|---|---|---|---|
| `settlement_point` | string | no | e.g. `HB_NORTH`, `LZ_HOUSTON` |
| `settlement_point_type` | string | yes | `HU`, `LZ`, `LZEW`, `RN`, `PUN`, … ; NULL for DAM (`np4-190-cd`) and SCED (`np6-788-cd`) (ADR 0005 §7) |
| `price_mwh` | float64 | no | $/MWh |
| `dst_flag` | bool | no | true on the second occurrence of the repeated fall-back hour |

Business key: (`interval_start`, `settlement_point`, `settlement_point_type`). The type is
part of the key: load zones appear as both `LZ` and `LZEW` in each interval (ADR 0005 §9).

### `mcpc`: `np4-188-cd`, `np6-331-cd`

| column | type | nullable | meaning |
|---|---|---|---|
| `as_type` | string | no | `REGUP`, `REGDN`, `RRS`, `ECRS`, `NSPIN` |
| `mcpc_mw` | float64 | no | $/MW |
| `dst_flag` | bool | no | as above |

Business key: (`interval_start`, `as_type`).

### `series`: `np3-565-cd`, `np4-732-cd`, `np4-737-cd`, `np6-322-cd`, `np6-345-cd`

Every numeric column of the ERCOT report, melted to one row per interval and series.

| column | type | nullable | meaning |
|---|---|---|---|
| `series` | string | no | `<report>:<column>`, e.g. `load_fcst:SystemTotal`, `wind:STWPF_SYSTEM_WIDE` |
| `value` | float64 | no | in the report's own unit |
| `dst_flag` | bool | no | as above |

Business key: (`interval_start`, `series`). A new source column is a schema-version bump,
never a silent extra series.

## 5. `manifests/<product>/latest.json`

Written after each successful run. The data fields keep their previous values when a run
found nothing new.

| field | type | meaning |
|---|---|---|
| `product` | string | product key |
| `last_posted_at` | ISO 8601 UTC \| null | newest posting in the lake |
| `latest_interval_start` | ISO 8601 UTC \| null | newest delivery interval in the lake |
| `partitions` | string[] | `date=` partitions the last productive run wrote |
| `schema_version` | int | curated schema version the pipeline writes now |
| `started_at`, `duration_s`, `docs`, `rows_written`, `status` | | last run, informational |

Other fields may appear; consumers must ignore unknown fields.

## 6. `manifests/_catalog.json`

```json
{
  "contract_version": "1.0.0",
  "generated_at": "2026-09-23T00:00:00Z",
  "timezone": "America/Chicago",
  "products": {
    "np6-905-cd": {
      "name": "RT settlement point prices, 15-min",
      "table": "spp",
      "interval_minutes": 15,
      "schema_version": 1,
      "collected_from": null,
      "live": true
    }
  }
}
```

`collected_from`: the first interval the lake is expected to hold. `null` means the product
is backfilled. Consumers resolve a product's table from here, not from `config.yaml`.

The lake collects every product the pipeline is configured with. The catalog is the list of
what this lake holds:
- `live: true`: the product is collected now.
- `live: false`: the product was retired from collection. Its data stays readable.
- A product missing from the catalog is not in this lake.

## 7. The point-in-time read rule

The answer to "what was known at `as_of`" for a product and a delivery range is:

1. Read every curated file of the delivery-date partitions covering the range.
2. Keep only rows where `posted_at <= as_of`.
3. Within each business key, keep the row with the greatest `posted_at`. Because a posting
   holds each business key once (§4), this row is unique: the answer is deterministic.

```sql
SELECT *
FROM read_parquet(<files>, hive_partitioning = true)
WHERE posted_at <= $as_of
QUALIFY row_number() OVER (PARTITION BY <business key> ORDER BY posted_at DESC) = 1
```

- A read without an `as_of` is looking at the future. `ercot_lake.LakeReader` has no method
  that omits it.
- `ingested_at` is the second clock. A replay that needs "what *this system* had at T" filters
  on `ingested_at <= T` as well (a system-time cut). `posted_at` is the knowledge-time cut.
  Backfilled rows have `ingested_at` much later than `posted_at`.
- List files per delivery day. A `date=*` glob over S3 lists the whole product prefix.

## 8. Cross-repository outputs (SSM Parameter Store)

| parameter | value |
|---|---|
| `/ercot-pit-lake/lake_bucket` | bucket name |
| `/ercot-pit-lake/lake_bucket_arn` | bucket ARN |
| `/ercot-pit-lake/lake_read_policy_arn` | managed policy: GetObject/ListBucket on `curated/*`, `manifests/*` |
| `/ercot-pit-lake/region` | region |
| `/ercot-pit-lake/contract_version` | e.g. `1.0.0` |

Consumers attach `lake_read_policy_arn` to their roles. They never receive write access to
`raw/`, `curated/` or `manifests/`.
