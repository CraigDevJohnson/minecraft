locals {
  source_backup_bucket_arn = "arn:aws:s3:::${var.source_backup_bucket_name}"
}

resource "aws_iam_role" "recovery" {
  name = "${var.resource_prefix}-host"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "ec2.amazonaws.com" }
    }]
  })
  tags = { Name = "${var.resource_prefix}-host", Purpose = "recovery-rehearsal" }
}

resource "aws_iam_instance_profile" "recovery" {
  name = "${var.resource_prefix}-host"
  role = aws_iam_role.recovery.name
}

resource "aws_iam_role_policy" "ssm" {
  name = "session-manager"
  role = aws_iam_role.recovery.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "ssm:UpdateInstanceInformation",
        "ssmmessages:CreateControlChannel",
        "ssmmessages:CreateDataChannel",
        "ssmmessages:OpenControlChannel",
        "ssmmessages:OpenDataChannel",
      ]
      Resource = "*"
    }]
  })
}

resource "aws_iam_role_policy" "source_backup_read" {
  name = "source-backup-read-only"
  role = aws_iam_role.recovery.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ListSelectedInstanceBackups"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = [local.source_backup_bucket_arn]
        Condition = {
          StringLike = { "s3:prefix" = ["instances/${var.instance_id}/*"] }
        }
      },
      {
        Sid      = "ReadSelectedInstanceBackups"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = ["${local.source_backup_bucket_arn}/instances/${var.instance_id}/*"]
      },
      {
        Sid      = "LocateSourceBackupBucket"
        Effect   = "Allow"
        Action   = ["s3:GetBucketLocation"]
        Resource = [local.source_backup_bucket_arn]
      },
    ]
  })
}

resource "aws_iam_role_policy" "recovery_backup_read" {
  name = "temporary-recovery-backup-read"
  role = aws_iam_role.recovery.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ListTemporaryBackups"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = [local.recovery_backup_bucket_arn]
        Condition = {
          StringLike = { "s3:prefix" = ["instances/${var.instance_id}/*"] }
        }
      },
      {
        Sid      = "ReadTemporaryBackups"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = ["${local.recovery_backup_bucket_arn}/instances/${var.instance_id}/*"]
      },
      {
        Sid      = "LocateTemporaryBackupBucket"
        Effect   = "Allow"
        Action   = ["s3:GetBucketLocation"]
        Resource = [local.recovery_backup_bucket_arn]
      },
    ]
  })
}

resource "aws_iam_role_policy" "recovery_backup_writes" {
  count = var.backup_writes_enabled ? 1 : 0
  name  = "temporary-recovery-backup-writes"
  role  = aws_iam_role.recovery.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid    = "ManageTemporaryBackups"
      Effect = "Allow"
      Action = [
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:AbortMultipartUpload",
        "s3:ListMultipartUploadParts",
      ]
      Resource = ["${local.recovery_backup_bucket_arn}/instances/${var.instance_id}/*"]
    }]
  })
}
