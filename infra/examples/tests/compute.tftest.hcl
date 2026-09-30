# The compute module on its own.

mock_provider "aws" {
  source = "./tests"
}

variables {
  account_id      = "123456789012"
  lake_bucket     = "test-lake"
  lake_bucket_arn = "arn:aws:s3:::test-lake"
  state_table     = "state"
  state_table_arn = "arn:aws:dynamodb:us-east-1:123456789012:table/state"
  secret_arn      = "arn:aws:secretsmanager:us-east-1:123456789012:secret:x"
  secret_name     = "x"
  image_tag       = "t"
  products        = { every-minute = { schedule = "cron(* * * * ? *)" } }
}

run "scheduled_runs_are_never_retried_late" {
  command = apply

  module {
    source = "../modules/compute"
  }

  # A slow ERCOT must not turn into a queue of stale runs holding every concurrent slot:
  # the next scheduled run is the retry.
  assert {
    condition = (
      aws_lambda_function_event_invoke_config.ingest.maximum_retry_attempts == 0
      && aws_lambda_function_event_invoke_config.compact.maximum_retry_attempts == 0
    )
    error_message = "no asynchronous retries"
  }

  assert {
    condition     = aws_lambda_function_event_invoke_config.ingest.maximum_event_age_in_seconds < 180
    error_message = "a queued ingest event older than a few polling intervals is dropped"
  }
}
