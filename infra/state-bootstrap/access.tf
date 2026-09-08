locals {
  bucket_arn = "arn:aws:s3:::${var.state_bucket_name}"
  state_arns = [for key in values(var.state_keys) : "${local.bucket_arn}/${key}"]
  lock_arns  = [for arn in local.state_arns : "${arn}.tflock"]
}

resource "aws_s3_bucket_policy" "state" {
  bucket = aws_s3_bucket.state.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "RequireTLS"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource  = [local.bucket_arn, "${local.bucket_arn}/*"]
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      },
      {
        Sid       = "OperatorOnly"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource  = [local.bucket_arn, "${local.bucket_arn}/*"]
        Condition = { ArnNotEquals = { "aws:PrincipalArn" = var.operator_role_arn } }
      },
      {
        Sid       = "RetainStateVersions"
        Effect    = "Deny"
        Principal = "*"
        Action    = ["s3:DeleteObject", "s3:DeleteObjectVersion"]
        Resource  = local.state_arns
      }
    ]
  })
  depends_on = [aws_s3_bucket_public_access_block.state]
  lifecycle {
    prevent_destroy = true
  }
}

output "operator_backend_policy" {
  description = "Review and attach to the existing operator role in #7; separate resource administration permissions are also needed."
  sensitive   = true
  value = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = [local.bucket_arn]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = local.state_arns
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
        Resource = local.lock_arns
      }
    ]
  })
}

output "backend_configs" {
  description = "Private backend settings; use only the default workspace and distinct state keys."
  sensitive   = true
  value = {
    for name, key in var.state_keys : name => {
      bucket              = var.state_bucket_name
      key                 = key
      region              = var.region
      encrypt             = true
      use_lockfile        = true
      allowed_account_ids = [var.account_id]
      assume_role         = { role_arn = var.operator_role_arn }
    }
  }
}
