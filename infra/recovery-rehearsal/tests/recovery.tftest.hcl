mock_provider "aws" {}

variables {
  account_id                  = "111122223333"
  operator_role_arn           = "arn:aws:iam::111122223333:role/minecraft-operator"
  environment                 = "family-recovery"
  resource_prefix             = "minecraft-family-recovery"
  deployment_revision         = "113b9e1c719a24fa0b65b6e7a33faf6573a394a5"
  availability_zone           = "us-west-2a"
  ami_id                      = "ami-0123456789abcdef0"
  ami_owner_account_id        = "099720109477"
  source_vpc_id               = "vpc-0123456789abcdef0"
  source_subnet_id            = "subnet-0123456789abcdef0"
  source_security_group_id    = "sg-0123456789abcdef0"
  source_backup_bucket_name   = "minecraft-family-source-backups"
  recovery_backup_bucket_name = "minecraft-family-recovery-backups"
  source_data_filesystem_uuid = "aaaaaaaa-1234-4234-8234-123456789abc"
  data_filesystem_uuid        = "12345678-1234-4234-8234-123456789abc"
}

run "temporary_host_has_guarded_storage_and_endpoint" {
  command = plan

  assert {
    condition     = aws_instance.recovery.instance_type == "m7g.large" && aws_instance.recovery.root_block_device[0].encrypted && aws_instance.recovery.root_block_device[0].delete_on_termination
    error_message = "The recovery host must use the reviewed ARM64 size and an encrypted replaceable root disk."
  }

  assert {
    condition     = aws_ebs_volume.recovery.encrypted && aws_ebs_volume.recovery.type == "gp3" && aws_ebs_volume.recovery.size == 40
    error_message = "The recovery data disk must be encrypted 40 GiB gp3."
  }

  assert {
    condition     = aws_volume_attachment.recovery.force_detach == false && aws_volume_attachment.recovery.skip_destroy == false
    error_message = "Recovery cleanup must use an orderly detach."
  }

  assert {
    condition     = aws_instance.recovery.metadata_options[0].http_tokens == "required" && aws_instance.recovery.metadata_options[0].http_put_response_hop_limit == 1 && !aws_instance.recovery.associate_public_ip_address && aws_eip.recovery.domain == "vpc"
    error_message = "The recovery host must require IMDSv2 and use its explicit temporary EIP."
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
    condition     = length(aws_iam_role_policy.recovery_backups) == 1 && jsondecode(aws_iam_role_policy.recovery_backups[0].policy).Statement[1].Resource == ["arn:aws:s3:::minecraft-family-recovery-backups/instances/survival/*"]
    error_message = "Enabled backup writes must target only the temporary recovery bucket."
  }
}

run "private_outputs_identify_target_and_cleanup_boundary" {
  command = plan

  assert {
    condition     = output.deployment.instances.survival.data_path == "/srv/minecraft/instances/survival/data" && output.deployment.instances.survival.backup_prefix == "instances/survival/" && output.deployment.backup_bucket_name == "minecraft-family-recovery-backups"
    error_message = "The recovery deployment must use the existing named-instance contract."
  }

  assert {
    condition     = output.cleanup_inventory.purpose == "recovery-rehearsal" && output.cleanup_inventory.host_id == aws_instance.recovery.id && output.cleanup_inventory.data_volume_id == aws_ebs_volume.recovery.id && output.cleanup_inventory.elastic_ip_id == aws_eip.recovery.id && output.cleanup_inventory.backup_bucket_name == "minecraft-family-recovery-backups"
    error_message = "Cleanup output must enumerate only temporary recovery resources."
  }
}

run "failed_backup_mode_removes_only_temporary_write_policy" {
  command = plan
  variables {
    backup_writes_enabled = false
  }

  assert {
    condition     = length(aws_iam_role_policy.recovery_backups) == 0 && aws_iam_role_policy.source_backup_read.name == "source-backup-read-only"
    error_message = "Failed-backup mode must remove temporary writes while retaining source read access."
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
