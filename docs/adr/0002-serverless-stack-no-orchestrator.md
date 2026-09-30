# 0002. Serverless stack, no orchestrator or warehouse

Date: 2026-09-03
Status: accepted

## Context

A handful of ERCOT reports, each a small fetch on a fixed cadence: daily, hourly, 15-minute or
5-minute. The whole lake grows by megabytes a day. Consumers such as forecasters, backtests and
dashboards read it in batches. The project should cost almost nothing when idle, and anyone
should be able to deploy it into their own AWS account.

## Decision

- EventBridge Scheduler + Lambda (one arm64 container image) + S3 + DynamoDB (watermarks) +
  DuckDB.
- Terraform for every resource.
- No Airflow, Kafka, RDS, Redshift or cache tier. The lake is the database: consumers read the
  Parquet with DuckDB.

## Consequences

- Idle cost is close to zero, with no always-on hosts. A deployment fits in the AWS free tier
  plus a few dollars of S3.
- Orchestration logic lives in code (per-product watermarks), not in a scheduler UI.
- Monitoring stays small: two CloudWatch alarms and one metric family, not an alarm per product.
- Any new managed service needs a written reason, in an ADR that supersedes this one.
