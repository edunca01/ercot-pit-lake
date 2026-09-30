# The example root against a mocked AWS provider: nothing is created and no credentials are
# needed. `apply` here only computes values, so assertions can see ARNs and names.

mock_provider "aws" {
  source = "./tests"
}

variables {
  name_prefix = "test"
}

run "defaults" {
  command = apply

  assert {
    condition     = output.lake_bucket == "test-lake-123456789012"
    error_message = "the bucket is <prefix>-lake-<account id>"
  }

  assert {
    condition     = length(output.schedules) == length(yamldecode(file("../../config.yaml")).products)
    error_message = "every product in config.yaml gets exactly one schedule"
  }

  assert {
    condition = alltrue([
      for k, p in yamldecode(file("../../config.yaml")).products : output.schedules[k] == p.schedule
    ])
    error_message = "schedules come verbatim from config.yaml"
  }

  assert {
    condition     = output.alarm_names == ["ercot-data-stale", "ercot-daily-report-invocation-errors"]
    error_message = "exactly two alarms"
  }

  assert {
    condition     = module.compute.image_uri == "123456789012.dkr.ecr.us-east-1.amazonaws.com/ercot-ingest@sha256:0000000000000000000000000000000000000000000000000000000000000000"
    error_message = "the Lambdas run an image pinned by digest, never a tag"
  }

  assert {
    condition = output.ssm_parameters == tolist([
      "/ercot-pit-lake/contract_version",
      "/ercot-pit-lake/lake_bucket",
      "/ercot-pit-lake/lake_bucket_arn",
      "/ercot-pit-lake/lake_read_policy_arn",
      "/ercot-pit-lake/region",
    ])
    error_message = "consumers find the lake through these parameters"
  }

  assert {
    condition     = can(regex("^[0-9]+\\.[0-9]+\\.[0-9]+$", local.contract_version))
    error_message = "the contract version is read from the library as SemVer"
  }

  assert {
    condition     = output.ci_roles == null
    error_message = "no CI roles without a GitHub subject"
  }
}

run "ci_roles_need_a_state_bucket" {
  command = plan

  variables {
    github_sub_prefix = "repo:owner/repo"
  }

  expect_failures = [var.tfstate_bucket]
}

run "with_ci" {
  command = apply

  variables {
    github_sub_prefix = "repo:owner/repo"
    tfstate_bucket    = "test-tfstate"
  }

  assert {
    condition     = output.ci_roles != null
    error_message = "the CI roles exist when a GitHub subject is given"
  }
}

run "extra_objects_from_config" {
  command = apply

  variables {
    config_path = "tests/fixtures/config.yaml"
  }

  assert {
    condition     = length(output.schedules) == 2
    error_message = "products come from the configured file"
  }

  assert {
    condition = local.extra_objects == [
      { bucket_arn = "arn:aws:s3:::test-lake-123456789012", key = "status/heartbeat.json" },
      { bucket_arn = "arn:aws:s3:::other-bucket", key = "status/other.json" },
      { bucket_arn = "arn:aws:s3:::other-bucket", key = "reports/daily.json" },
    ]
    error_message = "watch and section keys resolve against the lake unless they name a bucket"
  }
}
