output "lake_bucket" {
  value = aws_s3_bucket.lake.bucket
}

output "lake_bucket_arn" {
  value = aws_s3_bucket.lake.arn
}

output "state_table" {
  value = aws_dynamodb_table.state.name
}

output "state_table_arn" {
  value = aws_dynamodb_table.state.arn
}

output "secret_arn" {
  value = aws_secretsmanager_secret.ercot.arn
}

output "secret_name" {
  value = aws_secretsmanager_secret.ercot.name
}

output "lake_read_policy_arn" {
  value = aws_iam_policy.lake_read.arn
}

output "ssm_parameters" {
  value = sort([for p in aws_ssm_parameter.published : p.name])
}
