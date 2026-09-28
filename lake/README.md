# ercot-lake

The read side of [ercot-pit-lake](../README.md): the lake contract (layout, schemas, business
keys) as code, and a DuckDB reader that only ever answers "what was known at `as_of`".

```
uv add "ercot-lake @ git+https://github.com/<owner>/ercot-pit-lake@lake-v1.0.0#subdirectory=lake"
```

Dependencies: `duckdb`, `pyarrow`, `pydantic`. No AWS SDK: S3 goes through DuckDB, with
credentials from the standard AWS chain (environment, profile, SSO or instance role).

## Read as of a moment

```python
from datetime import UTC, date, datetime

from ercot_lake import LakeReader

with LakeReader("s3://my-ercot-lake") as lake:  # or a local directory
    # Day-ahead prices for Sep 3, as they stood at noon CT on Sep 2 (before the DAM cleared:
    # no rows). Every query requires as_of; there is no "latest, ignoring time".
    da = lake.spp_by_date(
        "np4-190-cd", date(2026, 9, 3), as_of=datetime(2026, 9, 2, 17, tzinfo=UTC)
    )

    # Real-time prices for an interval range, including any later corrections up to as_of.
    rt = lake.spp_by_range(
        "np6-905-cd",
        datetime(2026, 9, 3, 20, tzinfo=UTC),
        datetime(2026, 9, 3, 21, tzinfo=UTC),
        as_of=datetime(2026, 9, 4, tzinfo=UTC),
        points=["HB_NORTH"],
    )

    # Every version of every row, for replaying revisions as they were published.
    history = lake.postings(
        "np6-905-cd",
        datetime(2026, 9, 3, 20, tzinfo=UTC),
        datetime(2026, 9, 3, 21, tzinfo=UTC),
        as_of=datetime(2026, 9, 4, tzinfo=UTC),
    )

rows = rt.to_pylist()  # results are pyarrow Tables (.to_pandas() if you have pandas)
```

| Method | Returns |
|---|---|
| `spp_by_date`, `spp_by_range` | settlement point prices, filter `points=` |
| `mcpc_by_date`, `mcpc_by_range` | ancillary-service clearing prices, filter `as_types=` |
| `series_by_date`, `series_by_range` | load, wind, solar and system series, filter `names=` |
| `postings` | every posting up to `as_of`, with `ingested_at` and `source` |
| `products()` | the products this lake holds, from its catalog |

A point-in-time read keeps rows with `posted_at <= as_of`, then the latest posting per
business key. Ranges are half-open, `[start, end)`. Timestamps are timezone-aware UTC.
