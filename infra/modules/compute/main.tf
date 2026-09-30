# Compute layer: the image registry, the ingest, backfill and compaction Lambdas (one image,
# the handler chosen by the image command), their least-privilege roles, and the schedules:
# one per product, read from config.yaml, plus hourly compaction. Freshness and the daily
# report are the observability layer.

# -- Image registry -------------------------------------------------------------------------

resource "aws_ecr_repository" "ingest" {
  name                 = var.image_repository
  image_tag_mutability = "MUTABLE" # `latest` moves; the Lambdas pin a digest, not a tag

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

resource "aws_ecr_lifecycle_policy" "ingest" {
  repository = aws_ecr_repository.ingest.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "keep the last 10 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 10
      }
      action = { type = "expire" }
    }]
  })
}

# Resolved at plan time, so a plan always shows when the running code would change. It fails
# until an image with this tag exists: the first deploy creates the repository alone, pushes,
# then plans the rest.
data "aws_ecr_image" "ingest" {
  repository_name = aws_ecr_repository.ingest.name
  image_tag       = var.image_tag
}

locals {
  image_uri = "${aws_ecr_repository.ingest.repository_url}@${data.aws_ecr_image.ingest.image_digest}"
}

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

# -- Ingest role: secret read, lake read/write under raw/ curated/ manifests/, watermarks, logs.
# It deletes nothing: raw/ is immutable and curated/ only ever gains postings.

resource "aws_iam_role" "lambda" {
  name               = "${var.ingest_name}-lambda"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "lambda" {
  statement {
    sid       = "ReadErcotSecret"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [var.secret_arn]
  }

  statement {
    sid       = "ListLake"
    actions   = ["s3:ListBucket"]
    resources = [var.lake_bucket_arn]
  }

  statement {
    sid     = "WriteLake"
    actions = ["s3:GetObject", "s3:PutObject"]
    resources = [
      "${var.lake_bucket_arn}/${var.raw_prefix}/*",
      "${var.lake_bucket_arn}/${var.curated_prefix}/*",
      "${var.lake_bucket_arn}/${var.manifests_prefix}/*",
    ]
  }

  statement {
    sid       = "Watermarks"
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem"]
    resources = [var.state_table_arn]
  }

  statement {
    sid     = "Logs"
    actions = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = [
      "${aws_cloudwatch_log_group.ingest.arn}:*",
      "${aws_cloudwatch_log_group.backfill.arn}:*",
    ]
  }
}

resource "aws_iam_role_policy" "lambda" {
  name   = "${var.ingest_name}-lambda"
  role   = aws_iam_role.lambda.id
  policy = data.aws_iam_policy_document.lambda.json
}

locals {
  ingest_env = {
    LAKE_ROOT       = "s3://${var.lake_bucket}"
    ERCOT_SECRET_ID = var.secret_name
    STATE_BACKEND   = "dynamodb"
    STATE_TABLE     = var.state_table
  }
}

# -- Ingest: every scheduled run -------------------------------------------------------------

resource "aws_cloudwatch_log_group" "ingest" {
  name              = "/aws/lambda/${var.ingest_name}"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "ingest" {
  function_name = var.ingest_name
  description   = "ERCOT ingest: one invocation per scheduled product run, payload {\"product\": ...}"
  role          = aws_iam_role.lambda.arn
  package_type  = "Image"
  image_uri     = local.image_uri
  architectures = ["arm64"]
  memory_size   = var.lambda_memory_mb
  timeout       = var.lambda_timeout_s

  environment {
    variables = local.ingest_env
  }

  # JSON logs: the daily report finds ERROR lines by field, not by text.
  logging_config {
    log_format = "JSON"
    log_group  = aws_cloudwatch_log_group.ingest.name
  }

  depends_on = [aws_iam_role_policy.lambda]
}

# -- Backfill: invoked by hand with a posting window and a source ----------------------------

resource "aws_cloudwatch_log_group" "backfill" {
  name              = "/aws/lambda/${var.backfill_name}"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "backfill" {
  function_name = var.backfill_name
  description   = "ERCOT backfill: payload {\"product\", \"from\", \"to\", \"source\"?, \"delivery_from\"?, \"delivery_to\"?}"
  role          = aws_iam_role.lambda.arn
  package_type  = "Image"
  image_uri     = local.image_uri
  architectures = ["arm64"]
  memory_size   = var.lambda_memory_mb
  timeout       = 900 # the Lambda maximum; long windows are split into several invocations

  image_config {
    command = ["ingest.handler.backfill"]
  }

  environment {
    variables = local.ingest_env
  }

  logging_config {
    log_format = "JSON"
    log_group  = aws_cloudwatch_log_group.backfill.name
  }

  depends_on = [aws_iam_role_policy.lambda]
}

# -- Compaction: its own role, the only one that may delete, and only under curated/ ---------

resource "aws_iam_role" "compact" {
  name               = "${var.compact_name}-lambda"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "compact" {
  statement {
    sid       = "ListCurated"
    actions   = ["s3:ListBucket"]
    resources = [var.lake_bucket_arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["${var.curated_prefix}/*"]
    }
  }

  statement {
    sid       = "RewriteCurated"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["${var.lake_bucket_arn}/${var.curated_prefix}/*"]
  }

  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.compact.arn}:*"]
  }
}

resource "aws_iam_role_policy" "compact" {
  name   = "${var.compact_name}-lambda"
  role   = aws_iam_role.compact.id
  policy = data.aws_iam_policy_document.compact.json
}

resource "aws_cloudwatch_log_group" "compact" {
  name              = "/aws/lambda/${var.compact_name}"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "compact" {
  function_name = var.compact_name
  description   = "Merges curated files older than an hour into one file per partition"
  role          = aws_iam_role.compact.arn
  package_type  = "Image"
  image_uri     = local.image_uri
  architectures = ["arm64"]
  memory_size   = var.lambda_memory_mb
  timeout       = var.lambda_timeout_s

  image_config {
    command = ["ingest.handler.compact"]
  }

  environment {
    variables = { LAKE_ROOT = "s3://${var.lake_bucket}" }
  }

  logging_config {
    log_format = "JSON"
    log_group  = aws_cloudwatch_log_group.compact.name
  }

  depends_on = [aws_iam_role_policy.compact]
}

# -- Schedules: one per product (cron from config.yaml, Central time) and hourly compaction --

resource "aws_scheduler_schedule_group" "ercot" {
  name = var.schedule_group
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
  name               = "${var.ingest_name}-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

data "aws_iam_policy_document" "scheduler" {
  statement {
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.ingest.arn, aws_lambda_function.compact.arn]
  }
}

resource "aws_iam_role_policy" "scheduler" {
  name   = "${var.ingest_name}-scheduler"
  role   = aws_iam_role.scheduler.id
  policy = data.aws_iam_policy_document.scheduler.json
}

resource "aws_scheduler_schedule" "product" {
  for_each = var.products

  name                         = "${var.ingest_name}-${each.key}"
  group_name                   = aws_scheduler_schedule_group.ercot.name
  description                  = "ingest ${each.key}"
  schedule_expression          = each.value.schedule
  schedule_expression_timezone = var.schedule_timezone

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.ingest.arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({ product = each.key })

    # A failed run is retried once soon after; anything older is stale and the next scheduled
    # run covers it (the watermark makes catch-up automatic).
    retry_policy {
      maximum_retry_attempts       = 1
      maximum_event_age_in_seconds = 600
    }
  }
}

resource "aws_scheduler_schedule" "compact" {
  name                         = var.compact_name
  group_name                   = aws_scheduler_schedule_group.ercot.name
  description                  = "merge small curated files"
  schedule_expression          = var.compact_schedule
  schedule_expression_timezone = var.schedule_timezone

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.compact.arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({})
  }
}
