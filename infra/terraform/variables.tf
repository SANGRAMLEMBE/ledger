variable "aws_region" {
  description = "AWS region. ap-south-1 (Mumbai) keeps data in-country, which matters for Indian financial records."
  type        = string
  default     = "ap-south-1"
}

variable "environment" {
  description = "Deployment environment. Drives sizing and retention."
  type        = string
  default     = "dev"

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of: dev, staging, prod."
  }
}

variable "vpc_cidr" {
  description = "CIDR block for the VPC."
  type        = string
  default     = "10.20.0.0/16"
}

variable "db_instance_class" {
  description = <<-EOT
    RDS instance class. db.t4g.micro is the cheapest Postgres that runs, and is
    correct for a project measured on a 50k-record batch — the workload is a
    short burst, not sustained load. Size up deliberately when a measurement
    says to, not in advance.
  EOT
  type        = string
  default     = "db.t4g.micro"
}

variable "db_allocated_storage_gb" {
  description = "Initial RDS storage in GB. gp3 grows without downtime, so start small."
  type        = number
  default     = 20
}

variable "db_username" {
  description = "Master username for the Postgres instance."
  type        = string
  default     = "ledger_admin"
}

variable "allowed_ingress_cidrs" {
  description = <<-EOT
    CIDRs permitted to reach the database. Empty by default and it must stay that
    way: an RDS instance reachable from 0.0.0.0/0 holding transaction data is a
    breach waiting to be discovered. Add your own address explicitly when you
    need direct access, and remove it afterwards.
  EOT
  type        = list(string)
  default     = []
}
