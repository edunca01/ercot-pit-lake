variable "region" {
  type    = string
  default = "us-east-1"
}

variable "name_prefix" {
  description = "Prefix for globally unique names. The lake bucket is <prefix>-lake-<account id>."
  type        = string
}

variable "image_tag" {
  description = "Tag of the pushed image; the Lambdas pin its digest."
  type        = string
  default     = "latest"
}

variable "default_tags" {
  type    = map(string)
  default = { project = "ercot-pit-lake" }
}

variable "alert_email" {
  description = "Email for alarms and the full daily report, or null."
  type        = string
  default     = null
}

variable "slack" {
  description = "AWS Chatbot Slack binding for alarms and the short report, or null."
  type = object({
    configuration_name = string
    team_id            = string
    channel_id         = string
  })
  default = null
}

variable "github_sub_prefix" {
  description = "OIDC subject prefix of the deploying repository; null skips the CI roles."
  type        = string
  default     = null
}

variable "tfstate_bucket" {
  description = "Bucket holding this root's state (the deploy role reads and writes it)."
  type        = string
  default     = null

  validation {
    condition     = var.github_sub_prefix == null || var.tfstate_bucket != null
    error_message = "tfstate_bucket is required when github_sub_prefix is set."
  }
}

variable "raw_ia_after_days" {
  type    = number
  default = 30
}

variable "noncurrent_version_expire_days" {
  type    = number
  default = 30
}

variable "config_path" {
  description = "config.yaml, relative to this root."
  type        = string
  default     = "../../config.yaml"
}

variable "contract_path" {
  description = "The library module defining CONTRACT_VERSION, relative to this root."
  type        = string
  default     = "../../lake/src/ercot_lake/contract.py"
}
