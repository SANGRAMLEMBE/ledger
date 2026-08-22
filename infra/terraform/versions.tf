terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.40"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # State backend is deliberately left local for now.
  #
  # A remote S3 backend with DynamoDB locking is the right answer the moment more
  # than one person runs apply, because two concurrent applies against local
  # state will corrupt it. It is not the right answer today: the bucket has to
  # exist before it can hold the state that creates it, so bootstrapping it is a
  # separate chicken-and-egg step. Do that when the first deploy actually
  # happens, not before.
  #
  # backend "s3" {
  #   bucket         = "ledger-tfstate-<account-id>"
  #   key            = "ledger/terraform.tfstate"
  #   region         = "ap-south-1"
  #   dynamodb_table = "ledger-tflock"
  #   encrypt        = true
  # }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "ledger"
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}
