output "vpc_id" {
  description = "VPC the stack runs in."
  value       = aws_vpc.main.id
}

output "public_subnet_ids" {
  description = "Subnets for application compute."
  value       = aws_subnet.public[*].id
}

output "artifacts_bucket" {
  description = "S3 bucket for raw source files and batch artefacts."
  value       = aws_s3_bucket.artifacts.bucket
}

output "db_endpoint" {
  description = "Postgres endpoint. Host only — credentials live in Secrets Manager."
  value       = aws_db_instance.main.address
}

output "db_secret_arn" {
  description = <<-EOT
    Secrets Manager ARN holding the database credentials. The application reads
    the secret by ARN at runtime; the password itself is never an output, never
    in state you would paste into chat, and never in the repository.
  EOT
  value       = aws_secretsmanager_secret.db.arn
}

output "app_security_group_id" {
  description = "Attach application compute to this to reach the database."
  value       = aws_security_group.app.id
}
