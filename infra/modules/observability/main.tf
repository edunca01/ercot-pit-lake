# Observability layer, kept inside CloudWatch's free tier (10 alarms, 10 custom metrics): a
# freshness Lambda that turns manifests and heartbeats into metrics, TWO alarms, and a daily
# report. Both channels are optional, so a copy of this project can deploy with neither.
#
#   actions topic -> Slack   the two alarms (something in AWS needs a person) + the short report
#   alerts topic  -> email   the same two alarms + the full daily report, which carries the
#                            trace (ingest ERROR lines, alarm transitions) that would otherwise
#                            need an alarm per event

locals {
  namespace = "ErcotIngest"

  email_enabled = var.alert_email != null
  slack_enabled = var.slack != null

  notify    = [aws_sns_topic.alerts.arn, aws_sns_topic.actions.arn]
  notify_ok = var.slack_recoveries ? local.notify : [aws_sns_topic.alerts.arn]

  # Heartbeats and report sections declared in config.yaml, by bucket: each key is readable,
  # and listable so that a missing object reads as missing rather than as access denied.
  extra_buckets = { for o in var.extra_objects : o.bucket_arn => o.key... }
}

# -- Alert fan-out ------------------------------------------------------------------------------
# Both topics always exist: the alarms route to them, and an unsubscribed topic costs nothing.

resource "aws_sns_topic" "alerts" {
  name = var.alerts_topic
}

resource "aws_sns_topic" "actions" {
  name = var.actions_topic
}

resource "aws_sns_topic_subscription" "email" {
  count = local.email_enabled ? 1 : 0

  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

data "aws_iam_policy_document" "chatbot_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["chatbot.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [var.account_id]
    }
  }
}

resource "aws_iam_role" "chatbot" {
  count = local.slack_enabled ? 1 : 0

  name               = "${var.alarm_prefix}-alerts-chatbot"
  assume_role_policy = data.aws_iam_policy_document.chatbot_assume.json
}

data "aws_iam_policy_document" "chatbot" {
  # What Chatbot needs to render an alarm notification with context; no write actions.
  statement {
    actions = [
      "cloudwatch:Describe*",
      "cloudwatch:Get*",
      "cloudwatch:List*",
      "logs:Describe*",
      "logs:Get*",
      "logs:FilterLogEvents",
      "sns:Get*",
      "sns:List*",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "chatbot" {
  count = local.slack_enabled ? 1 : 0

  name   = "notifications-only"
  role   = aws_iam_role.chatbot[0].id
  policy = data.aws_iam_policy_document.chatbot.json
}

# Authorising the Slack workspace in AWS Chatbot is a console-only step, done once.
resource "aws_chatbot_slack_channel_configuration" "alerts" {
  count = local.slack_enabled ? 1 : 0

  configuration_name    = var.slack.configuration_name
  slack_team_id         = var.slack.team_id
  slack_channel_id      = var.slack.channel_id
  iam_role_arn          = aws_iam_role.chatbot[0].arn
  sns_topic_arns        = [aws_sns_topic.actions.arn]
  guardrail_policy_arns = ["arn:aws:iam::aws:policy/ReadOnlyAccess"]
  logging_level         = "ERROR"
}

# -- Freshness and daily report: one role -------------------------------------------------------

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "freshness" {
  name               = "${var.freshness_name}-lambda"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_cloudwatch_log_group" "freshness" {
  name              = "/aws/lambda/${var.freshness_name}"
  retention_in_days = var.log_retention_days
}

# Read-only on manifests/ and curated/, listing on raw/ (posting counts), the declared extra
# objects, metrics in one namespace; for the report, alarm history, metrics, the ingest log's
# ERROR lines, and publishing to both topics.
data "aws_iam_policy_document" "freshness" {
  statement {
    sid       = "ListLake"
    actions   = ["s3:ListBucket"]
    resources = [var.lake_bucket_arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values = [
        "${var.manifests_prefix}/*", "${var.curated_prefix}/*", "${var.raw_prefix}/*",
      ]
    }
  }

  statement {
    sid     = "ReadManifestsCurated"
    actions = ["s3:GetObject"]
    resources = [
      "${var.lake_bucket_arn}/${var.manifests_prefix}/*",
      "${var.lake_bucket_arn}/${var.curated_prefix}/*",
    ]
  }

  dynamic "statement" {
    for_each = local.extra_buckets
    content {
      sid       = "ListExtra${substr(sha1(statement.key), 0, 8)}"
      actions   = ["s3:ListBucket"]
      resources = [statement.key]
      condition {
        test     = "StringLike"
        variable = "s3:prefix"
        values   = statement.value
      }
    }
  }

  dynamic "statement" {
    for_each = length(var.extra_objects) > 0 ? [1] : []
    content {
      sid       = "ReadExtraObjects"
      actions   = ["s3:GetObject"]
      resources = [for o in var.extra_objects : "${o.bucket_arn}/${o.key}"]
    }
  }

  statement {
    sid       = "PublishReport"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.alerts.arn, aws_sns_topic.actions.arn]
  }

  statement {
    sid = "ReadAlarmsAndMetrics" # the report's alarm log and KPIs; these actions take no resource
    actions = [
      "cloudwatch:DescribeAlarmHistory",
      "cloudwatch:DescribeAlarms",
      "cloudwatch:GetMetricData",
    ]
    resources = ["*"]
  }

  statement {
    sid       = "PutMetrics"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = [local.namespace]
    }
  }

  statement {
    sid       = "ReadIngestErrors"
    actions   = ["logs:FilterLogEvents"]
    resources = ["${var.ingest_log_group_arn}:*"]
  }

  statement {
    sid     = "Logs"
    actions = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = [
      "${aws_cloudwatch_log_group.freshness.arn}:*",
      "${aws_cloudwatch_log_group.daily_report.arn}:*",
    ]
  }
}

resource "aws_iam_role_policy" "freshness" {
  name   = "${var.freshness_name}-lambda"
  role   = aws_iam_role.freshness.id
  policy = data.aws_iam_policy_document.freshness.json
}

resource "aws_lambda_function" "freshness" {
  function_name = var.freshness_name
  description   = "Publishes ${local.namespace} StaleProducts and FreshnessMinutes from manifests and heartbeats"
  role          = aws_iam_role.freshness.arn
  package_type  = "Image"
  image_uri     = var.image_uri
  architectures = ["arm64"]
  memory_size   = 512
  timeout       = 60

  image_config {
    command = ["ingest.handler.freshness"]
  }

  environment {
    variables = {
      LAKE_ROOT = "s3://${var.lake_bucket}"
    }
  }

  logging_config {
    log_format = "JSON"
    log_group  = aws_cloudwatch_log_group.freshness.name
  }

  depends_on = [aws_iam_role_policy.freshness]
}

# Asynchronous invocations: drop failed and stale events rather than retry them later. A
# missed freshness check is covered by the next one five minutes on, and a failed report is
# the report alarm's job, not a late duplicate's.
resource "aws_lambda_function_event_invoke_config" "freshness" {
  function_name                = aws_lambda_function.freshness.function_name
  maximum_retry_attempts       = 0
  maximum_event_age_in_seconds = 240
}

resource "aws_lambda_function_event_invoke_config" "daily_report" {
  function_name                = aws_lambda_function.daily_report.function_name
  maximum_retry_attempts       = 0
  maximum_event_age_in_seconds = 3600
}

data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [var.account_id]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${var.freshness_name}-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

resource "aws_iam_role_policy" "scheduler" {
  name = "${var.freshness_name}-scheduler"
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow", Action = "lambda:InvokeFunction",
      Resource = [aws_lambda_function.freshness.arn, aws_lambda_function.daily_report.arn]
    }]
  })
}

resource "aws_scheduler_schedule" "freshness" {
  name                         = var.freshness_name
  group_name                   = var.schedule_group
  description                  = "publish freshness metrics"
  schedule_expression          = var.freshness_schedule
  schedule_expression_timezone = "America/Chicago"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.freshness.arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({})
  }
}

# -- Daily report -------------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "daily_report" {
  name              = "/aws/lambda/${var.report_name}"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "daily_report" {
  function_name = var.report_name
  description   = "Daily report: full record by email, problems and KPIs to Slack; either channel optional"
  role          = aws_iam_role.freshness.arn
  package_type  = "Image"
  image_uri     = var.image_uri
  architectures = ["arm64"]
  memory_size   = 1024
  timeout       = 300 # before compaction a 5-minute product is 288 small files a day, for three days

  image_config {
    command = ["ingest.handler.daily_report"]
  }

  environment {
    variables = {
      LAKE_ROOT            = "s3://${var.lake_bucket}"
      ALERTS_TOPIC_ARN     = local.email_enabled ? aws_sns_topic.alerts.arn : ""
      ACTIONS_TOPIC_ARN    = local.slack_enabled ? aws_sns_topic.actions.arn : ""
      INGEST_FUNCTION_NAME = var.ingest_function_name
      INGEST_LOG_GROUP     = var.ingest_log_group
      ALARM_PREFIX         = var.alarm_prefix
    }
  }

  logging_config {
    log_format = "JSON"
    log_group  = aws_cloudwatch_log_group.daily_report.name
  }

  depends_on = [aws_iam_role_policy.freshness]
}

resource "aws_scheduler_schedule" "daily_report" {
  name                         = var.report_name
  group_name                   = var.schedule_group
  description                  = "daily report (email: full record; Slack: problems and KPIs)"
  schedule_expression          = var.report_schedule
  schedule_expression_timezone = var.report_timezone

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.daily_report.arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({})
  }
}

# -- Alarms: exactly two ------------------------------------------------------------------------
# Both mean "someone has to look at AWS". Everything else (a failed run the next run retried,
# ERCOT running late for a while) is in the daily email. Thresholds live in config.yaml and the
# freshness Lambda does the comparing, so every product shares one metric and one alarm.

# Data is going stale, or is not being measured. Three consecutive 5-minute checks; missing
# data breaches, so the freshness Lambda not running fires this too.
resource "aws_cloudwatch_metric_alarm" "data_stale" {
  alarm_name          = "${var.alarm_prefix}-data-stale"
  alarm_description   = "At least one product or heartbeat is past its stale_after_min (ERCOT is down, or ingest is broken and a gap is forming), or ${var.freshness_name} has stopped publishing. Which one: the 'stale:' line in /aws/lambda/${var.freshness_name}."
  namespace           = local.namespace
  metric_name         = "StaleProducts"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 3
  datapoints_to_alarm = 3
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "breaching"
  alarm_actions       = local.notify
  ok_actions          = local.notify_ok
}

# The daily report is what makes a quiet channel trustworthy, so its failure is an alarm.
resource "aws_cloudwatch_metric_alarm" "report_failed" {
  alarm_name          = "${var.report_name}-invocation-errors"
  alarm_description   = "The daily report did not go out. Check /aws/lambda/${var.report_name}, then re-run: aws lambda invoke --function-name ${var.report_name} --payload '{}' /dev/stdout"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.daily_report.function_name }
  statistic           = "Sum"
  period              = 900
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.notify
  ok_actions          = local.notify_ok
}
