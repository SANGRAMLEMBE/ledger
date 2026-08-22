# Postgres — the reconciled ledger, exceptions, and the append-only audit log.
#
# The master password is generated here and stored in Secrets Manager. It is
# never written to a .tf file, never passed as a variable, and never printed.
# A password in version control is not a password; it is a published credential
# that also has to be rotated once someone notices.

resource "random_password" "db" {
  length  = 32
  special = true
  # RDS rejects these in a master password.
  override_special = "!#$%&*()-_=+[]{}<>:?"
}

resource "aws_secretsmanager_secret" "db" {
  name_prefix             = "ledger/db/master-"
  description             = "Ledger Postgres master credentials"
  recovery_window_in_days = var.environment == "prod" ? 30 : 0
}

resource "aws_secretsmanager_secret_version" "db" {
  secret_id = aws_secretsmanager_secret.db.id
  secret_string = jsonencode({
    username = var.db_username
    password = random_password.db.result
    engine   = "postgres"
    host     = aws_db_instance.main.address
    port     = aws_db_instance.main.port
    dbname   = aws_db_instance.main.db_name
  })
}

resource "aws_db_subnet_group" "main" {
  name       = "ledger-db-subnets"
  subnet_ids = aws_subnet.private[*].id

  tags = { Name = "ledger-db-subnets" }
}

resource "aws_db_instance" "main" {
  identifier     = "ledger-${var.environment}"
  engine         = "postgres"
  engine_version = "16"

  instance_class    = var.db_instance_class
  allocated_storage = var.db_allocated_storage_gb
  storage_type      = "gp3"
  storage_encrypted = true # non-negotiable for financial records

  db_name  = "ledger"
  username = var.db_username
  password = random_password.db.result

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.db.id]
  publicly_accessible    = false

  # Single-AZ in dev is a deliberate cost choice, not an oversight: multi-AZ
  # doubles the bill for a resilience property a hackathon does not need.
  multi_az = var.environment == "prod"

  backup_retention_period = var.environment == "prod" ? 7 : 1
  skip_final_snapshot     = var.environment != "prod"
  deletion_protection     = var.environment == "prod"

  # Surface slow queries — the matching cascade will be the thing that gets slow.
  enabled_cloudwatch_logs_exports = ["postgresql"]

  # Minor versions carry security fixes; there is no reason to hold them back.
  auto_minor_version_upgrade = true

  tags = { Name = "ledger-postgres" }
}
