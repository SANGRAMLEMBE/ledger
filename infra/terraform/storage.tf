# S3 bucket for raw source files (statements, settlement reports) and batch
# artefacts.
#
# Everything about this bucket is closed by default: no public access, encrypted
# at rest, versioned. These are the settings people intend to add "later" and
# then don't — and a bucket of transaction data is precisely the thing that must
# not be world-readable.

resource "aws_s3_bucket" "artifacts" {
  bucket_prefix = "ledger-artifacts-"

  tags = { Name = "ledger-artifacts" }
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  versioning_configuration {
    # Versioning is the difference between "someone overwrote yesterday's
    # statement" being an inconvenience and being a lost audit trail.
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  rule {
    id     = "expire-old-versions"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = 30
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}
