variable "account_id" {
  type = string
}

variable "lake_bucket" {
  type = string
}

variable "lake_bucket_arn" {
  type = string
}

variable "state_table" {
  type = string
}

variable "state_table_arn" {
  type = string
}

variable "secret_arn" {
  type = string
}

variable "secret_name" {
  type = string
}

variable "image_tag" {
  description = "Image tag the Lambdas run. Resolved to a digest at plan time, so a re-tag is a visible change."
  type        = string
}

variable "products" {
  description = "Products from config.yaml: key -> { schedule = cron(...) }."
  type        = map(object({ schedule = string }))
}

variable "image_repository" {
  type    = string
  default = "ercot-ingest"
}

variable "ingest_name" {
  type    = string
  default = "ercot-ingest"
}

variable "backfill_name" {
  type    = string
  default = "ercot-backfill"
}

variable "compact_name" {
  type    = string
  default = "ercot-compact"
}

variable "schedule_group" {
  type    = string
  default = "ercot"
}

variable "schedule_timezone" {
  type    = string
  default = "America/Chicago"
}

variable "compact_schedule" {
  type    = string
  default = "cron(17 * * * ? *)" # hourly, off the minute products poll on
}

variable "lambda_memory_mb" {
  type    = number
  default = 1024
}

variable "lambda_timeout_s" {
  type    = number
  default = 600
}

variable "log_retention_days" {
  type    = number
  default = 30
}

variable "raw_prefix" {
  type    = string
  default = "raw"
}

variable "curated_prefix" {
  type    = string
  default = "curated"
}

variable "manifests_prefix" {
  type    = string
  default = "manifests"
}
