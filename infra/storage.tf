locals {
  backup_bucket_arn       = "arn:aws:s3:::${var.backup_bucket_name}"
  backup_instance_objects = [for id in sort(tolist(var.instance_ids)) : "${local.backup_bucket_arn}/instances/${id}/*"]
  backup_point_objects = flatten([for id in sort(tolist(var.instance_ids)) : [
    "${local.backup_bucket_arn}/instances/${id}/completed/*",
    "${local.backup_bucket_arn}/instances/${id}/objects/*",
  ]])
  backup_receipt_objects = [for id in sort(tolist(var.instance_ids)) : "${local.backup_bucket_arn}/instances/${id}/receipts/*"]
  # One '?' matches one character. The receipt ID mask excludes current.json.
  immutable_receipt_objects  = [for id in sort(tolist(var.instance_ids)) : "${local.backup_bucket_arn}/instances/${id}/receipts/${join("", [for index in range(64) : "?"])}.json"]
  backup_selection_objects   = [for id in sort(tolist(var.instance_ids)) : "${local.backup_bucket_arn}/instances/${id}/receipts/current.json"]
  recovery_retention_objects = concat(local.backup_point_objects, local.backup_receipt_objects)
  host_principal_arn         = "arn:aws:iam::${var.account_id}:role/${var.resource_prefix}-host"
  s3_only_bucket_guards = [
    {
      Sid       = "RequireImmutableRecoveryPublication"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:PutObject"
      Resource  = concat(local.backup_point_objects, local.immutable_receipt_objects)
      Condition = {
        StringNotEquals = { "s3:if-none-match" = "*" }
        Bool            = { "s3:ObjectCreationOperation" = "true" }
      }
    },
    {
      Sid       = "RequireConditionalCurrentSelection"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:PutObject"
      Resource  = local.backup_selection_objects
      Condition = {
        Null = { "s3:if-match" = "true", "s3:if-none-match" = "true" }
        Bool = { "s3:ObjectCreationOperation" = "true" }
      }
    },
    {
      Sid       = "NeverDeleteCurrentSelection"
      Effect    = "Deny"
      Principal = "*"
      Action    = ["s3:DeleteObject", "s3:DeleteObjectVersion"]
      Resource  = local.backup_selection_objects
    },
    {
      Sid       = "LifecycleOwnsRetiredVersionDeletion"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:DeleteObjectVersion"
      Resource  = local.backup_instance_objects
    },
    {
      Sid       = "RuntimeCannotRetireRecoveryObjects"
      Effect    = "Deny"
      Principal = "*"
      Action = [
        "s3:DeleteObject", "s3:DeleteObjectVersion",
        "s3:PutObjectTagging", "s3:PutObjectVersionTagging",
        "s3:DeleteObjectTagging", "s3:DeleteObjectVersionTagging",
      ]
      Resource  = local.backup_instance_objects
      Condition = { ArnEquals = { "aws:PrincipalArn" = local.host_principal_arn } }
    },
    {
      Sid       = "RuntimeCannotPublishReceipts"
      Effect    = "Deny"
      Principal = "*"
      Action    = ["s3:PutObject", "s3:AbortMultipartUpload"]
      Resource  = local.backup_receipt_objects
      Condition = { ArnEquals = { "aws:PrincipalArn" = local.host_principal_arn } }
    },
    {
      Sid       = "RequireRetentionTagValue"
      Effect    = "Deny"
      Principal = "*"
      Action    = ["s3:PutObjectTagging", "s3:PutObjectVersionTagging"]
      Resource  = local.recovery_retention_objects
      Condition = { StringNotEquals = { "s3:RequestObjectTag/minecraft-retention" = ["keep", "retired"] } }
    },
    {
      Sid       = "RequireRetentionTagKeys"
      Effect    = "Deny"
      Principal = "*"
      Action    = ["s3:PutObjectTagging", "s3:PutObjectVersionTagging"]
      Resource  = local.recovery_retention_objects
      Condition = { Null = { "s3:RequestObjectTagKeys" = "true" } }
    },
    {
      Sid       = "RejectUnownedRetentionTagKeys"
      Effect    = "Deny"
      Principal = "*"
      Action    = ["s3:PutObjectTagging", "s3:PutObjectVersionTagging"]
      Resource  = local.recovery_retention_objects
      Condition = { "ForAnyValue:StringNotEquals" = { "s3:RequestObjectTagKeys" = ["minecraft-retention"] } }
    },
    {
      Sid       = "NeverRemoveRecoveryRetentionTags"
      Effect    = "Deny"
      Principal = "*"
      Action    = ["s3:DeleteObjectTagging", "s3:DeleteObjectVersionTagging"]
      Resource  = local.recovery_retention_objects
    },
  ]
}

resource "aws_s3_bucket" "backups" {
  bucket        = var.backup_bucket_name
  force_destroy = false
  tags          = { Name = "${var.resource_prefix}-backups", Purpose = "retained-backups" }
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_public_access_block" "backups" {
  bucket                  = aws_s3_bucket.backups.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_ownership_controls" "backups" {
  bucket = aws_s3_bucket.backups.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "backups" {
  bucket = aws_s3_bucket.backups.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "backups" {
  bucket = aws_s3_bucket.backups.id
  versioning_configuration {
    status = "Enabled"
  }
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "backups" {
  bucket     = aws_s3_bucket.backups.id
  depends_on = [aws_s3_bucket_versioning.backups]
  dynamic "rule" {
    for_each = var.lifecycle_mode == "s3-only" ? [] : [1]
    content {
      id     = "remove-superseded-versions-and-incomplete-uploads"
      status = "Enabled"
      filter {}
      noncurrent_version_expiration {
        noncurrent_days = 14
      }
      abort_incomplete_multipart_upload {
        days_after_initiation = 1
      }
    }
  }
  dynamic "rule" {
    for_each = var.lifecycle_mode == "s3-only" ? var.instance_ids : []
    content {
      id     = "retire-verified-${rule.value}"
      status = "Enabled"
      filter {
        and {
          prefix = "instances/${rule.value}/"
          tags   = { minecraft-retention = "retired" }
        }
      }
      expiration {
        days = 14
      }
      noncurrent_version_expiration {
        noncurrent_days = 14
      }
    }
  }
  dynamic "rule" {
    for_each = var.lifecycle_mode == "s3-only" ? var.instance_ids : []
    content {
      id     = "abort-incomplete-${rule.value}"
      status = "Enabled"
      filter {
        prefix = "instances/${rule.value}/"
      }
      abort_incomplete_multipart_upload {
        days_after_initiation = 1
      }
    }
  }
  dynamic "rule" {
    for_each = var.lifecycle_mode == "s3-only" ? var.instance_ids : []
    content {
      id     = "remove-expired-markers-${rule.value}"
      status = "Enabled"
      filter {
        prefix = "instances/${rule.value}/"
      }
      expiration {
        expired_object_delete_marker = true
      }
    }
  }
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_policy" "backups" {
  bucket = aws_s3_bucket.backups.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [local.backup_bucket_arn, "${local.backup_bucket_arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }], [for guard in local.s3_only_bucket_guards : guard if var.lifecycle_mode == "s3-only"])
  })
  lifecycle {
    prevent_destroy = true
  }
}
