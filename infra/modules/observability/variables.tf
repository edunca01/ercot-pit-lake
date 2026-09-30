variable "account_id" {
  type = string
}

variable "alert_email" {
  description = "Email subscribed to the alerts topic, or null for no email."
  type        = string
  default     = null
}

variable "slack" {
  description = "AWS Chatbot Slack channel binding, or null for no Slack."
  type = object({
    configuration_name = string
    team_id            = string
    channel_id         = string
  })
  default = null
}

variable "slack_recoveries" {
  description = "Send alarm recoveries to Slack too (they always go to email)."
  type        = bool
  default     = true
}

variable "image_uri" {
  description = "The project image, pinned by digest (from the compute layer)."
  type        = string
}

variable "ingest_function_name" {
  type = string
}

variable "ingest_log_group" {
  type = string
}

variable "ingest_log_group_arn" {
  type = string
}

variable "lake_bucket" {
  type = string
}

variable "lake_bucket_arn" {
  type = string
}

variable "extra_objects" {
  description = "Objects the freshness and report Lambdas read beyond the lake: extra watches and report sections from config.yaml."
  type        = list(object({ bucket_arn = string, key = string }))
  default     = []
}

variable "schedule_group" {
  type    = string
  default = "ercot"
}

variable "alarm_prefix" {
  type    = string
  default = "ercot"
}

variable "alerts_topic" {
  type    = string
  default = "ercot-alerts"
}

variable "actions_topic" {
  type    = string
  default = "ercot-actions"
}

variable "freshness_name" {
  type    = string
  default = "ercot-freshness"
}

variable "report_name" {
  type    = string
  default = "ercot-daily-report"
}

variable "freshness_schedule" {
  type    = string
  default = "cron(*/5 * * * ? *)"
}

variable "report_schedule" {
  description = "Daily report time, in report_timezone."
  type        = string
  default     = "cron(0 21 * * ? *)"
}

variable "report_timezone" {
  type    = string
  default = "America/New_York"
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
