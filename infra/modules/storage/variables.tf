variable "name_prefix" {
  description = "Prefix for globally unique names (S3). The bucket is <prefix>-lake-<account id>."
  type        = string
}

variable "account_id" {
  type = string
}

variable "region" {
  type = string
}

variable "contract_version" {
  description = "The lake contract version the deployed code writes, published for consumers."
  type        = string
}

variable "raw_ia_after_days" {
  description = "Days before objects under raw/ move to Standard-IA."
  type        = number
  default     = 30
}

variable "noncurrent_version_expire_days" {
  description = "Days to keep superseded object versions."
  type        = number
  default     = 30
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

variable "state_table_name" {
  type    = string
  default = "ercot-ingest-state"
}

variable "secret_name" {
  type    = string
  default = "ercot/public-api"
}

variable "read_policy_name" {
  type    = string
  default = "ercot-lake-read"
}

variable "ssm_prefix" {
  description = "Parameter Store path the cross-repository values are published under."
  type        = string
  default     = "ercot-pit-lake"
}
