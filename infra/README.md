# infra

Terraform for the whole pipeline: one module per layer, and an example root that wires them
together. Nothing here holds a backend or a deployment-specific value, and nothing in this
repository runs `apply`.

| Module | Creates |
|---|---|
| `modules/storage` | the lake bucket `<prefix>-lake-<account id>` (versioned, encrypted, private, `raw/` to Standard-IA after 30 days), the watermark table, the ERCOT credentials secret (container only), the `ercot-lake-read` consumer policy, and the SSM parameters consumers look the lake up by |
| `modules/compute` | the image repository, the ingest, backfill and compaction Lambdas with their own least-privilege roles, the `ercot` schedule group, one schedule per product and hourly compaction |
| `modules/observability` | the freshness Lambda, the daily report, exactly two alarms (`ercot-data-stale`, `ercot-daily-report-invocation-errors`) and two SNS topics; email and Slack are each optional |
| `modules/ci` | a GitHub OIDC provider, a read-only plan role and a scoped deploy role |

## What reads config.yaml

- **Schedules:** every product under `products:` gets one schedule with its `schedule` cron,
  in Central time. There is no switch per product: listed means collected.
- **Extra objects:** each `extra_watches` and `report_sections` entry grants the freshness and
  report role read access to exactly that key, in the lake bucket or in the entry's `bucket`.
- **Contract version:** read from `lake/src/ercot_lake/contract.py` and published as
  `/ercot-pit-lake/contract_version`.

## Consumers

A consumer in the same account reads these parameters and attaches the read policy to its own
role; it needs nothing else from this repository.

| Parameter | Value |
|---|---|
| `/ercot-pit-lake/lake_bucket` | bucket name |
| `/ercot-pit-lake/lake_bucket_arn` | bucket ARN |
| `/ercot-pit-lake/lake_read_policy_arn` | managed policy: list and get under `curated/` and `manifests/` |
| `/ercot-pit-lake/region` | region |
| `/ercot-pit-lake/contract_version` | the contract version the deployed code writes |

## Deploying into your own account

1. Create a versioned S3 bucket for Terraform state (by hand or a one-off root; it outlives
   everything else). Copy `examples/` to a directory of your own, add an S3 backend pointing at
   it with `use_lockfile = true`, and write a `terraform.tfvars` from
   `terraform.tfvars.example`.
2. The Lambdas pin the image by digest, which must exist before they can be planned. On a first
   deploy, create the repository alone, push, then apply the rest:
   ```
   terraform apply -target=module.compute.aws_ecr_repository.ingest
   make docker-build   # then tag and push to the repository URL
   terraform apply
   ```
3. Put the ERCOT credentials in the secret as `{username, password, subscription_key}`.
4. For Slack, authorise the workspace in AWS Chatbot once in the console, then set `slack`.
   An email subscription must be confirmed from the inbox.

## Checks

```
make tf-fmt        # formatting (TF_FIX=1 rewrites)
make tf-validate   # init without a backend, validate the example root and its modules
make tf-test       # terraform test against a mocked AWS provider: no account, no credentials
```

The tests in `examples/tests/` assert the stack's invariants: one schedule per configured
product, exactly two alarms, images pinned by digest, the published parameters, optional
channels and CI roles, and read grants that follow `config.yaml`.
