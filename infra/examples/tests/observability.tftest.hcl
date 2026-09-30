# The observability module on its own: both channels optional, grants follow extra_objects.

mock_provider "aws" {
  source = "./tests"
}

variables {
  account_id           = "123456789012"
  image_uri            = "repo@sha256:0"
  ingest_function_name = "ercot-ingest"
  ingest_log_group     = "/aws/lambda/ercot-ingest"
  ingest_log_group_arn = "arn:aws:logs:us-east-1:123456789012:log-group:/aws/lambda/ercot-ingest"
  lake_bucket          = "test-lake"
  lake_bucket_arn      = "arn:aws:s3:::test-lake"
}

run "no_channels" {
  command = apply

  module {
    source = "../modules/observability"
  }

  assert {
    condition     = length(aws_sns_topic_subscription.email) == 0 && length(aws_chatbot_slack_channel_configuration.alerts) == 0
    error_message = "no email subscription or Slack binding unless configured"
  }

  assert {
    condition = (
      aws_lambda_function.daily_report.environment[0].variables.ALERTS_TOPIC_ARN == ""
      && aws_lambda_function.daily_report.environment[0].variables.ACTIONS_TOPIC_ARN == ""
    )
    error_message = "the report sends nowhere when no channel is configured"
  }

  assert {
    condition     = length([for s in data.aws_iam_policy_document.freshness.statement : s if s.sid == "ReadExtraObjects"]) == 0
    error_message = "no extra read grant without extra objects"
  }

  assert {
    condition = (
      aws_lambda_function_event_invoke_config.freshness.maximum_retry_attempts == 0
      && aws_lambda_function_event_invoke_config.daily_report.maximum_retry_attempts == 0
      && aws_lambda_function_event_invoke_config.freshness.maximum_event_age_in_seconds < 300
    )
    error_message = "no asynchronous retries, and a queued freshness check dies before the next one"
  }

  assert {
    condition     = aws_cloudwatch_metric_alarm.data_stale.treat_missing_data == "breaching"
    error_message = "a freshness Lambda that stops publishing must fire the alarm"
  }
}

run "both_channels_and_extra_objects" {
  command = apply

  module {
    source = "../modules/observability"
  }

  variables {
    alert_email = "alerts@example.com"
    slack = {
      configuration_name = "alerts"
      team_id            = "T0"
      channel_id         = "C0"
    }
    extra_objects = [
      { bucket_arn = "arn:aws:s3:::test-lake", key = "status/a.json" },
      { bucket_arn = "arn:aws:s3:::other", key = "status/b.json" },
      { bucket_arn = "arn:aws:s3:::other", key = "reports/c.json" },
    ]
  }

  assert {
    condition     = length(aws_sns_topic_subscription.email) == 1 && length(aws_chatbot_slack_channel_configuration.alerts) == 1
    error_message = "each configured channel is created"
  }

  assert {
    condition = one([
      for s in data.aws_iam_policy_document.freshness.statement : s.resources
      if s.sid == "ReadExtraObjects"
      ]) == toset([
      "arn:aws:s3:::test-lake/status/a.json",
      "arn:aws:s3:::other/status/b.json",
      "arn:aws:s3:::other/reports/c.json",
    ])
    error_message = "exactly the declared objects are readable"
  }

  assert {
    condition = length([
      for s in data.aws_iam_policy_document.freshness.statement : s
      if startswith(coalesce(s.sid, ""), "ListExtra")
    ]) == 2
    error_message = "one listing grant per extra bucket"
  }
}
