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

## Develop

Open in a Codespace or the dev container, or locally with `uv` and Python 3.12:

```
make setup
make check        # lint, mypy strict, tests; offline, no credentials
```

## Contributions

This is a portfolio project and does not take pull requests. You are welcome to fork it and
run it on your own AWS account. Report security issues privately: [SECURITY.md](SECURITY.md).

## License

MIT for the code. ERCOT data in `samples/` is ERCOT public market information, subject to
ERCOT's terms of use.
