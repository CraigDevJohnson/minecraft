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
    Statement = [
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
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject",
          "s3:AbortMultipartUpload",
          "s3:ListMultipartUploadParts",
        ]
        Resource = [for id in sort(tolist(var.instance_ids)) : "${local.backup_bucket_arn}/instances/${id}/*"]
      },
      {
        Sid      = "LocateBackupBucket"
        Effect   = "Allow"
        Action   = ["s3:GetBucketLocation"]
        Resource = [local.backup_bucket_arn]
      },
    ]
  })
}
