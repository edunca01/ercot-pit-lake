# Storage layer: the lake, the watermark table, the ERCOT credential secret, the consumer read
# policy, and the values other repositories read to find the lake. Nothing here is compute, and
# nothing here is destroyed casually: raw/ is the lake's immutable record.

# -- Lake -----------------------------------------------------------------------------------

resource "aws_s3_bucket" "lake" {
  bucket = "${var.name_prefix}-lake-${var.account_id}"

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "lake" {
  bucket = aws_s3_bucket.lake.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "lake" {
  bucket = aws_s3_bucket.lake.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "lake" {
  bucket = aws_s3_bucket.lake.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "lake" {
  bucket = aws_s3_bucket.lake.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "lake" {
  bucket = aws_s3_bucket.lake.id

  depends_on = [aws_s3_bucket_versioning.lake]

  # raw/ is written once and read rarely after the transform: Standard-IA halves its cost.
  rule {
    id     = "raw-to-ia"
    status = "Enabled"

    filter {
      prefix = "${var.raw_prefix}/"
    }

    transition {
      days          = var.raw_ia_after_days
      storage_class = "STANDARD_IA"
    }
  }

  # Idempotent re-runs overwrite the same key and versioning keeps the old copy; compaction
  # deletes small files. Both leave noncurrent versions, which this bounds.
  rule {
    id     = "expire-noncurrent"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = var.noncurrent_version_expire_days
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

# -- Watermark table ------------------------------------------------------------------------

resource "aws_dynamodb_table" "state" {
  name         = var.state_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "product"

  attribute {
    name = "product"
    type = "S"
  }

  point_in_time_recovery {
    enabled = false # a watermark is re-derivable from the lake; not worth the cost
  }
}

# -- ERCOT credentials ----------------------------------------------------------------------
# Only the container is managed here. The value {username, password, subscription_key} is
# entered in the console by hand, so it never passes through Terraform state or git.

resource "aws_secretsmanager_secret" "ercot" {
  name        = var.secret_name
  description = "ERCOT Public API credentials: {username, password, subscription_key}"

  recovery_window_in_days = 7
}

# -- Consumer read policy -------------------------------------------------------------------
# Every reader of the lake gets this and nothing more: curated data and manifests, never raw/.
# Only this project's own functions write to the lake.

data "aws_iam_policy_document" "lake_read" {
  statement {
    sid       = "ListLake"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.lake.arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values = [
        "${var.curated_prefix}/*",
        "${var.manifests_prefix}/*",
      ]
    }
  }

  statement {
    sid     = "ReadCuratedAndManifests"
    actions = ["s3:GetObject"]
    resources = [
      "${aws_s3_bucket.lake.arn}/${var.curated_prefix}/*",
      "${aws_s3_bucket.lake.arn}/${var.manifests_prefix}/*",
    ]
  }
}

resource "aws_iam_policy" "lake_read" {
  name        = var.read_policy_name
  description = "Read-only access to curated/ and manifests/ on the ERCOT lake"
  policy      = data.aws_iam_policy_document.lake_read.json
}

# -- What other repositories read to find the lake ------------------------------------------
# Consumers look these up with `data "aws_ssm_parameter"`, never with another repository's
# Terraform state, so each repository deploys on its own.

locals {
  published = {
    lake_bucket          = aws_s3_bucket.lake.bucket
    lake_bucket_arn      = aws_s3_bucket.lake.arn
    lake_read_policy_arn = aws_iam_policy.lake_read.arn
    region               = var.region
    contract_version     = var.contract_version
  }
}

resource "aws_ssm_parameter" "published" {
  for_each = local.published

  name  = "/${var.ssm_prefix}/${each.key}"
  type  = "String"
  value = each.value
}
