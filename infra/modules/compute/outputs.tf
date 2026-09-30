output "ecr_repository_url" {
  value = aws_ecr_repository.ingest.repository_url
}

output "ecr_repository_arn" {
  value = aws_ecr_repository.ingest.arn
}

output "image_uri" {
  description = "The image, pinned by digest, that every Lambda of this project runs."
  value       = local.image_uri
}

output "ingest_function_name" {
  value = aws_lambda_function.ingest.function_name
}

output "ingest_log_group" {
  value = aws_cloudwatch_log_group.ingest.name
}

output "ingest_log_group_arn" {
  value = aws_cloudwatch_log_group.ingest.arn
}

output "backfill_function_name" {
  value = aws_lambda_function.backfill.function_name
}

output "compact_function_name" {
  value = aws_lambda_function.compact.function_name
}

output "schedule_group" {
  value = aws_scheduler_schedule_group.ercot.name
}

output "schedules" {
  value = { for k, s in aws_scheduler_schedule.product : k => s.schedule_expression }
}
