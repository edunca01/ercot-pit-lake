output "alerts_topic_arn" {
  value = aws_sns_topic.alerts.arn
}

output "actions_topic_arn" {
  value = aws_sns_topic.actions.arn
}

output "alarm_names" {
  value = [
    aws_cloudwatch_metric_alarm.data_stale.alarm_name,
    aws_cloudwatch_metric_alarm.report_failed.alarm_name,
  ]
}

output "freshness_function_name" {
  value = aws_lambda_function.freshness.function_name
}

output "report_function_name" {
  value = aws_lambda_function.daily_report.function_name
}
