output "lake_bucket" {
  value = module.storage.lake_bucket
}

output "lake_read_policy_arn" {
  description = "Attach to a consumer's role to read curated/ and manifests/."
  value       = module.storage.lake_read_policy_arn
}

output "ssm_parameters" {
  value = module.storage.ssm_parameters
}

output "ecr_repository_url" {
  value = module.compute.ecr_repository_url
}

output "schedules" {
  value = module.compute.schedules
}

output "alarm_names" {
  value = module.observability.alarm_names
}

output "ci_roles" {
  value = var.github_sub_prefix == null ? null : {
    plan   = module.ci[0].plan_role_arn
    deploy = module.ci[0].deploy_role_arn
  }
}
