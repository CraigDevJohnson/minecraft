mock_provider "aws" {}

variables {
  account_id                             = "111122223333"
  operator_role_arn                      = "arn:aws:iam::111122223333:role/minecraft-operator"
  environment                            = "family-recovery"
  resource_prefix                        = "minecraft-family-recovery"
  deployment_revision                    = "113b9e1c719a24fa0b65b6e7a33faf6573a394a5"
  availability_zone                      = "us-west-2a"
  ami_id                                 = "ami-0123456789abcdef0"
  ami_owner_account_id                   = "099720109477"
  source_vpc_id                          = "vpc-0123456789abcdef0"
  source_subnet_id                       = "subnet-0123456789abcdef0"
  source_backup_bucket_name              = "minecraft-family-source-backups"
  recovery_backup_bucket_name            = "minecraft-family-recovery-backups"
  restore_archive_size_bytes             = 17179869184
  restore_uncompressed_size_bytes        = 34359738368
  recovery_data_volume_size_gib          = 64
  max_recovery_backup_archive_size_bytes = 17179869184
  recovery_root_volume_size_gib          = 64
  source_data_filesystem_uuid            = "aaaaaaaa-1234-4234-8234-123456789abc"
  data_filesystem_uuid                   = "12345678-1234-4234-8234-123456789abc"
}

run "temporary_host_has_guarded_storage_and_endpoint" {
  command = plan

  assert {
    condition     = aws_instance.recovery.instance_type == "m7g.large" && aws_instance.recovery.root_block_device[0].encrypted && aws_instance.recovery.root_block_device[0].volume_size == 64 && aws_instance.recovery.root_block_device[0].delete_on_termination
    error_message = "The recovery host must use the reviewed ARM64 size and reviewed encrypted replaceable root disk."
  }

  assert {
    condition     = aws_ebs_volume.recovery.encrypted && aws_ebs_volume.recovery.type == "gp3" && aws_ebs_volume.recovery.size == 64
    error_message = "The recovery data disk must use the reviewed size and encrypted gp3 storage."
  }

  assert {
    condition     = aws_volume_attachment.recovery.force_detach == false && aws_volume_attachment.recovery.skip_destroy == false
    error_message = "Recovery cleanup must use an orderly detach."
  }

  assert {
    condition     = aws_instance.recovery.metadata_options[0].http_tokens == "required" && aws_instance.recovery.metadata_options[0].http_put_response_hop_limit == 1 && !aws_instance.recovery.associate_public_ip_address && aws_eip.recovery.domain == "vpc"
    error_message = "The recovery host must require IMDSv2 and use its explicit temporary EIP."
  }

  assert {
    condition     = length(aws_security_group.recovery.ingress) == 0 && length(aws_security_group.recovery.egress) == 0 && toset(aws_instance.recovery.vpc_security_group_ids) == toset([aws_security_group.recovery.id])
    error_message = "The recovery host must use its own security group without inline ingress or egress rules."
  }

  assert {
    condition     = length(aws_vpc_security_group_egress_rule.web) == 2 && toset([for rule in aws_vpc_security_group_egress_rule.web : rule.from_port]) == toset([80, 443]) && toset([for rule in aws_vpc_security_group_egress_rule.web : rule.to_port]) == toset([80, 443]) && alltrue([for rule in aws_vpc_security_group_egress_rule.web : rule.ip_protocol == "tcp" && rule.cidr_ipv4 == "0.0.0.0/0" && rule.security_group_id == aws_security_group.recovery.id])
    error_message = "The recovery security group may allow only TCP 80 and 443 egress."
  }
}

run "source_backup_access_is_read_only" {
  command = plan

  assert {
    condition     = jsondecode(aws_iam_role_policy.source_backup_read.policy).Statement[0].Condition.StringLike["s3:prefix"] == ["instances/survival/*"]
    error_message = "Source bucket listing must be limited to survival."
  }

  assert {
    condition     = jsondecode(aws_iam_role_policy.source_backup_read.policy).Statement[1].Action == ["s3:GetObject"] && jsondecode(aws_iam_role_policy.source_backup_read.policy).Statement[1].Resource == ["arn:aws:s3:::minecraft-family-source-backups/instances/survival/*"]
    error_message = "Recovery credentials may only read current objects in the selected source namespace."
  }

  assert {
    condition     = length(jsondecode(aws_iam_role_policy.source_backup_read.policy).Statement) == 3 && jsondecode(aws_iam_role_policy.source_backup_read.policy).Statement[2].Action == ["s3:GetBucketLocation"]
    error_message = "No source write, delete, version, multipart, or bucket-management permissions may be added."
  }
}

run "temporary_backups_are_private_and_writable_only_when_enabled" {
  command = plan

  assert {
    condition     = aws_s3_bucket.recovery.force_destroy && aws_s3_bucket_public_access_block.recovery.block_public_acls && aws_s3_bucket_public_access_block.recovery.block_public_policy && aws_s3_bucket_public_access_block.recovery.ignore_public_acls && aws_s3_bucket_public_access_block.recovery.restrict_public_buckets
    error_message = "The temporary backup bucket must reject public access and support complete authorized cleanup."
  }

  assert {
    condition     = one(aws_s3_bucket_server_side_encryption_configuration.recovery.rule).apply_server_side_encryption_by_default[0].sse_algorithm == "AES256" && aws_s3_bucket_versioning.recovery.versioning_configuration[0].status == "Enabled"
    error_message = "Temporary recovery backups must be encrypted and versioned."
  }

  assert {
    condition = (
      toset(flatten([for statement in jsondecode(aws_iam_role_policy.recovery_backup_read.policy).Statement : statement.Action])) == toset(["s3:GetBucketLocation", "s3:GetObject", "s3:ListBucket"]) &&
      jsondecode(aws_iam_role_policy.recovery_backup_read.policy).Statement[0].Resource == ["arn:aws:s3:::minecraft-family-recovery-backups"] &&
      jsondecode(aws_iam_role_policy.recovery_backup_read.policy).Statement[0].Condition.StringLike["s3:prefix"] == ["instances/survival/*"] &&
      jsondecode(aws_iam_role_policy.recovery_backup_read.policy).Statement[1].Resource == ["arn:aws:s3:::minecraft-family-recovery-backups/instances/survival/*"] &&
      jsondecode(aws_iam_role_policy.recovery_backup_read.policy).Statement[2].Resource == ["arn:aws:s3:::minecraft-family-recovery-backups"]
    )
    error_message = "Temporary backup reads must remain independently available only in the selected temporary namespace."
  }

  assert {
    condition     = length(aws_iam_role_policy.recovery_backup_writes) == 1 && toset(flatten([for statement in jsondecode(aws_iam_role_policy.recovery_backup_writes[0].policy).Statement : statement.Action])) == toset(["s3:AbortMultipartUpload", "s3:DeleteObject", "s3:ListMultipartUploadParts", "s3:PutObject"]) && jsondecode(aws_iam_role_policy.recovery_backup_writes[0].policy).Statement[0].Resource == ["arn:aws:s3:::minecraft-family-recovery-backups/instances/survival/*"]
    error_message = "Enabled temporary backup mutations must be limited to the reviewed actions and temporary instance namespace."
  }
}

run "private_outputs_identify_target_and_cleanup_boundary" {
  command = plan

  assert {
    condition     = output.deployment.instances.survival.data_path == "/srv/minecraft/instances/survival/data" && output.deployment.instances.survival.backup_prefix == "instances/survival/" && output.deployment.backup_bucket_name == "minecraft-family-recovery-backups"
    error_message = "The recovery deployment must use the existing named-instance contract."
  }

  assert {
    condition     = output.cleanup_inventory.purpose == "recovery-rehearsal" && output.cleanup_inventory.host_id == aws_instance.recovery.id && output.cleanup_inventory.data_volume_id == aws_ebs_volume.recovery.id && output.cleanup_inventory.elastic_ip_id == aws_eip.recovery.id && output.cleanup_inventory.security_group_id == aws_security_group.recovery.id && output.cleanup_inventory.backup_bucket_name == "minecraft-family-recovery-backups"
    error_message = "Cleanup output must enumerate only temporary recovery resources."
  }
}

run "failed_backup_mode_removes_only_temporary_write_policy" {
  command = plan
  variables {
    backup_writes_enabled = false
  }

  assert {
    condition     = toset(flatten([for statement in jsondecode(aws_iam_role_policy.recovery_backup_read.policy).Statement : statement.Action])) == toset(["s3:GetBucketLocation", "s3:GetObject", "s3:ListBucket"]) && length(aws_iam_role_policy.recovery_backup_writes) == 0 && aws_iam_role_policy.source_backup_read.name == "source-backup-read-only"
    error_message = "Failed-backup mode must retain temporary reads while removing only temporary writes."
  }
}

run "reject_cross_account_operator" {
  command = plan
  variables {
    operator_role_arn = "arn:aws:iam::999900001111:role/minecraft-operator"
  }
  expect_failures = [var.operator_role_arn]
}

run "reject_wrong_region_az" {
  command = plan
  variables {
    availability_zone = "us-east-1a"
  }
  expect_failures = [var.availability_zone]
}

run "reject_source_filesystem_identity_for_target" {
  command = plan
  variables {
    data_filesystem_uuid = "aaaaaaaa-1234-4234-8234-123456789abc"
  }
  expect_failures = [var.data_filesystem_uuid]
}

run "reject_insufficient_restore_staging_capacity" {
  command = plan
  variables {
    recovery_data_volume_size_gib = 52
  }
  expect_failures = [aws_ebs_volume.recovery]
}

run "reject_root_disk_too_small_for_backup_workspace" {
  command = plan
  variables {
    recovery_root_volume_size_gib = 55
  }
  expect_failures = [aws_instance.recovery]
}

run "larger_backup_ceiling_requires_larger_root_disk" {
  command = plan
  variables {
    max_recovery_backup_archive_size_bytes = 34359738368
  }
  expect_failures = [aws_instance.recovery]
}

run "reject_backup_ceiling_below_selected_archive" {
  command = plan
  variables {
    max_recovery_backup_archive_size_bytes = 17179869183
  }
  expect_failures = [var.max_recovery_backup_archive_size_bytes]
}

run "reject_backup_ceiling_above_program_limit" {
  command = plan
  variables {
    max_recovery_backup_archive_size_bytes = 107374182401
  }
  expect_failures = [var.max_recovery_backup_archive_size_bytes]
}
