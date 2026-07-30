output "instance_id" {
  description = "Use this ID with AWS Systems Manager Session Manager."
  value       = aws_instance.runner.id
}

output "connect_command" {
  value = "aws ssm start-session --target ${aws_instance.runner.id}"
}
