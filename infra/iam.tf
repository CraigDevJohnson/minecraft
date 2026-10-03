resource "aws_iam_role" "host" {
  name = "${var.resource_prefix}-host"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "ec2.amazonaws.com" }
    }]
  })
  permissions_boundary = var.host_permissions_boundary_arn
  tags                 = { Purpose = "host-credentials" }
}

resource "aws_iam_instance_profile" "host" {
  name = "${var.resource_prefix}-host"
  role = aws_iam_role.host.name
}

resource "aws_iam_role_policy" "ssm" {
  name = "session-manager"
  role = aws_iam_role.host.id
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

resource "aws_iam_role_policy" "backups" {
  name = "instance-backups"
  role = aws_iam_role.host.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      {
        Sid      = "ListNamedInstanceBackups"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = [local.backup_bucket_arn]
        Condition = {
          StringLike = { "s3:prefix" = [for id in sort(tolist(var.instance_ids)) : "instances/${id}/*"] }
        }
      },
      {
        Sid    = "ManageCurrentNamedInstanceBackups"
        Effect = "Allow"
        Action = var.lifecycle_mode == "s3-only" ? [
          "s3:GetObject",
          "s3:GetObjectVersion",
          "s3:PutObject",
          "s3:AbortMultipartUpload",
          "s3:ListMultipartUploadParts",
          ] : [
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject",
          "s3:AbortMultipartUpload",
          "s3:ListMultipartUploadParts",
        ]
        Resource = var.lifecycle_mode == "s3-only" ? local.backup_point_objects : local.backup_instance_objects
      },
      {
        Sid      = "LocateBackupBucket"
        Effect   = "Allow"
        Action   = ["s3:GetBucketLocation"]
        Resource = [local.backup_bucket_arn]
      },
      ], var.lifecycle_mode == "s3-only" ? [{
        Sid      = "ReadExactRecoveryReceipts"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:GetObjectVersion"]
        Resource = local.backup_receipt_objects
    }] : [])
  })
}

resource "aws_iam_role_policy" "bootstrap" {
  count = var.lifecycle_mode == "retained-host" ? 0 : 1
  name  = "read-pinned-bootstrap"
  role  = aws_iam_role.host.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["s3:GetObject"]
      Resource = ["${local.backup_bucket_arn}/bootstrap/releases/${var.bootstrap_sha256}/*"]
    }]
  })
}
