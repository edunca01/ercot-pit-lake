variable "account_id" {
  type = string
}

variable "region" {
  type = string
}

variable "github_sub_prefixes" {
  description = <<-EOT
    Prefixes of the OIDC `sub` claim GitHub sends for the deploying repositories; usually one,
    two while a deployment moves between repositories. With GitHub's immutable subjects (the
    default for new repositories) a prefix is `repo:<owner>@<owner id>/<repo>@<repo id>`, not
    `repo:<owner>/<repo>`. Read it from
    `gh api repos/<owner>/<repo>/actions/oidc/customization/sub` (`sub_claim_prefix`).
  EOT
  type        = list(string)

  validation {
    condition     = length(var.github_sub_prefixes) > 0
    error_message = "at least one repository subject"
  }
}

variable "deploy_environment" {
  description = "GitHub environment the deploy job runs in."
  type        = string
  default     = "production"
}

variable "tfstate_bucket" {
  type = string
}

variable "lake_bucket_arn" {
  type = string
}

variable "state_table_arn" {
  type = string
}

variable "secret_arn" {
  type = string
}

variable "ecr_repository_arns" {
  description = "Repositories the deploy role may push to."
  type        = list(string)
}

variable "name_prefix" {
  description = "Prefix of every IAM role, policy and Lambda the deploy role may manage."
  type        = string
  default     = "ercot"
}

variable "schedule_group" {
  type    = string
  default = "ercot"
}

variable "ssm_prefix" {
  type    = string
  default = "ercot-pit-lake"
}
