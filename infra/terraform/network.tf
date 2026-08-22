# Network foundation.
#
# COST NOTE, stated up front because it is the classic hackathon mistake:
# there is no NAT Gateway here. A NAT Gateway costs roughly $32/month plus data
# processing, runs whether or not anything uses it, and is the single most common
# way a student AWS account quietly drains. Private subnets exist for the
# database, which needs no outbound internet. If a workload later genuinely needs
# egress from a private subnet, add VPC endpoints for the specific services
# first — they are cheaper and tighter than a NAT.

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  # Two AZs: the minimum RDS will accept for a subnet group, even single-AZ.
  azs = slice(data.aws_availability_zones.available.names, 0, 2)
}

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = "ledger-vpc" }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "ledger-igw" }
}

# --- public subnets: application compute, reachable from outside ------------

resource "aws_subnet" "public" {
  count                   = length(local.azs)
  vpc_id                  = aws_vpc.main.id
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, count.index)
  availability_zone       = local.azs[count.index]
  map_public_ip_on_launch = true

  tags = { Name = "ledger-public-${local.azs[count.index]}" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = { Name = "ledger-public-rt" }
}

resource "aws_route_table_association" "public" {
  count          = length(aws_subnet.public)
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# --- private subnets: the database, no route to the internet ---------------

resource "aws_subnet" "private" {
  count             = length(local.azs)
  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, count.index + 10)
  availability_zone = local.azs[count.index]

  tags = { Name = "ledger-private-${local.azs[count.index]}" }
}

# --- security groups --------------------------------------------------------

resource "aws_security_group" "app" {
  name        = "ledger-app"
  description = "Application compute"
  vpc_id      = aws_vpc.main.id

  egress {
    description = "Outbound to anywhere (package installs, Razorpay API)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "ledger-app-sg" }
}

resource "aws_security_group" "db" {
  name        = "ledger-db"
  description = "Postgres. Reachable from the app tier only."
  vpc_id      = aws_vpc.main.id

  # The app tier reaches the database by security-group reference rather than by
  # CIDR. This stays correct as subnets change, and it cannot accidentally widen
  # the way a hand-maintained CIDR list can.
  ingress {
    description     = "Postgres from the app security group"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.app.id]
  }

  # Optional direct access, empty by default. See the variable's comment.
  dynamic "ingress" {
    for_each = length(var.allowed_ingress_cidrs) > 0 ? [1] : []
    content {
      description = "Explicitly allowed operator access"
      from_port   = 5432
      to_port     = 5432
      protocol    = "tcp"
      cidr_blocks = var.allowed_ingress_cidrs
    }
  }

  tags = { Name = "ledger-db-sg" }
}
