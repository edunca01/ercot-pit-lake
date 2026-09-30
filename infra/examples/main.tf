# The whole stack from one root: storage, compute, observability and, optionally, the CI roles.
# config.yaml is the single source of products, schedules and the extra objects the freshness
# and report Lambdas read, so adding a product never touches Terraform.

data "aws_caller_identity" "current" {}

locals {
  config = yamldecode(file("${path.module}/${var.config_path}"))

  products = { for k, p in local.config.products : k => { schedule = p.schedule } }

  # The version the deployed code writes, read from the library so it cannot drift.
  contract_version = regex(
    "CONTRACT_VERSION: Final = \"([^\"]+)\"",
    file("${path.module}/${var.contract_path}"),
  )[0]

  account_id = data.aws_caller_identity.current.account_id

  # Objects outside curated/ and manifests/ the freshness and report Lambdas read: keys are
  # relative to the lake root unless the entry names its own bucket.
  extra_objects = [
    for o in concat(
      try(local.config.extra_watches, []),
      try(local.config.report_sections, []),
      ) : {
      bucket_arn = try(o.bucket, null) == null ? module.storage.lake_bucket_arn : "arn:aws:s3:::${o.bucket}"
      key        = o.key
    }
  ]
}

module "storage" {
  source = "../modules/storage"

  name_prefix                    = var.name_prefix
  account_id                     = local.account_id
  region                         = var.region
  contract_version               = local.contract_version
  raw_ia_after_days              = var.raw_ia_after_days
  noncurrent_version_expire_days = var.noncurrent_version_expire_days
}

module "compute" {
  source = "../modules/compute"

  account_id      = local.account_id
  lake_bucket     = module.storage.lake_bucket
  lake_bucket_arn = module.storage.lake_bucket_arn
  state_table     = module.storage.state_table
  state_table_arn = module.storage.state_table_arn
  secret_arn      = module.storage.secret_arn
  secret_name     = module.storage.secret_name
  image_tag       = var.image_tag
  products        = local.products
}

module "observability" {
  source = "../modules/observability"

  account_id           = local.account_id
  alert_email          = var.alert_email
  slack                = var.slack
  image_uri            = module.compute.image_uri
  ingest_function_name = module.compute.ingest_function_name
  ingest_log_group     = module.compute.ingest_log_group
  ingest_log_group_arn = module.compute.ingest_log_group_arn
  lake_bucket          = module.storage.lake_bucket
  lake_bucket_arn      = module.storage.lake_bucket_arn
  schedule_group       = module.compute.schedule_group
  extra_objects        = local.extra_objects
}

module "ci" {
  source = "../modules/ci"
  count  = var.github_sub_prefix == null ? 0 : 1

  account_id          = local.account_id
  region              = var.region
  github_sub_prefixes = [var.github_sub_prefix]
  tfstate_bucket      = var.tfstate_bucket
  lake_bucket_arn     = module.storage.lake_bucket_arn
  state_table_arn     = module.storage.state_table_arn
  secret_arn          = module.storage.secret_arn
  ecr_repository_arns = [module.compute.ecr_repository_arn]
}
