# ercot-pit-lake

A point-in-time data lake of ERCOT market data, and the serverless AWS pipeline that fills it.

Every ERCOT posting (settlement point prices, ancillary-service clearing prices, SCED LMPs,
load/wind/solar forecasts) is kept exactly as published. Curated Parquet carries both ERCOT's
posting time and ours. You can ask the lake **what was known at any moment**, corrections
and reposts included, which is the only honest input for backtesting a forecaster or a trading
strategy.

> **Status: early development.** The lake contract and the `ercot-lake` reader come first, then
> ingestion, then the Terraform to deploy it.

## Stack

EventBridge Scheduler → Lambda (arm64 container) → S3 (raw zips + curated Parquet) · DynamoDB
watermarks · DuckDB for reads · two CloudWatch alarms and a daily report · Terraform · GitHub
Actions with OIDC. Sized for the AWS free tier.

## Try it offline

No ERCOT account and no AWS needed: the committed samples are real ERCOT postings, and the
whole pipeline runs on them into a local lake.

```
make setup
make ingest OFFLINE=1          # ten products -> ./data (raw zips, curated Parquet, catalog)
```

```python
from datetime import UTC, date, datetime
from ercot_lake import LakeReader

with LakeReader("./data") as lake:
    before = lake.spp_by_date(
        "np4-190-cd", date(2026, 9, 29), as_of=datetime(2026, 9, 28, 18, 51, 10, tzinfo=UTC)
    )
    after = lake.spp_by_date(
        "np4-190-cd", date(2026, 9, 29), as_of=datetime(2026, 9, 28, 18, 51, 11, tzinfo=UTC)
    )
    print(before.num_rows, after.num_rows)  # 0 40: nothing is known before it was posted
```

## Develop

Open in a Codespace or the dev container, or locally with `uv` and Python 3.12:

```
make setup
make check        # lint, mypy strict, tests; offline, no credentials
make ingest       # live, from the watermark (needs ERCOT_* credentials, see .env.example)
uv run backfill --product np4-190-cd --from 2026-09-01 --to 2026-09-02 --source bundles
make docker-build # the Lambda image
```

## Contributions

This is a portfolio project and does not take pull requests. You are welcome to fork it and
run it on your own AWS account. Report security issues privately: [SECURITY.md](SECURITY.md).

## License

MIT for the code. ERCOT data in `samples/` is ERCOT public market information, subject to
ERCOT's terms of use.
