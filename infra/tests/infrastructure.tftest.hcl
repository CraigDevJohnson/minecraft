mock_provider "aws" {}

variables {
  account_id                = "111122223333"
  operator_role_arn         = "arn:aws:iam::111122223333:role/minecraft-operator"
  environment               = "test"
  resource_prefix           = "minecraft-test"
  availability_zone         = "us-west-2a"
  ami_id                    = "ami-0123456789abcdef0"
  ami_owner_account_id      = "099720109477"
  data_filesystem_uuid      = "12345678-1234-4234-8234-123456789abc"
  backup_bucket_name        = "minecraft-test-example-backups"
  budget_notification_email = "operator@example.invalid"
}

run "retained_encrypted_data_and_replaceable_root" {
  command = plan

  assert {
    condition     = aws_ebs_volume.data.encrypted && aws_ebs_volume.data.type == "gp3" && aws_ebs_volume.data.size == 40
    error_message = "The retained data disk must be encrypted 40 GiB gp3."
  }

  assert {
    condition     = aws_instance.host.root_block_device[0].encrypted && aws_instance.host.root_block_device[0].volume_size == 20 && aws_instance.host.root_block_device[0].delete_on_termination
    error_message = "The replaceable root disk must be encrypted 20 GiB and deleted on host termination."
  }

  assert {
    condition     = aws_volume_attachment.data.force_detach == false && aws_volume_attachment.data.skip_destroy == false
    error_message = "Normal host replacement must use an orderly detach of the independent retained disk."
  }
}

run "backups_remain_private_and_completed_points_do_not_age_out" {
  command = plan

  assert {
    condition     = !aws_s3_bucket.backups.force_destroy && aws_s3_bucket_public_access_block.backups.block_public_acls && aws_s3_bucket_public_access_block.backups.block_public_policy && aws_s3_bucket_public_access_block.backups.ignore_public_acls && aws_s3_bucket_public_access_block.backups.restrict_public_buckets
    error_message = "Backup storage must reject public access and refuse automatic object deletion."
  }

  assert {
    condition     = one(aws_s3_bucket_server_side_encryption_configuration.backups.rule).apply_server_side_encryption_by_default[0].sse_algorithm == "AES256" && aws_s3_bucket_versioning.backups.versioning_configuration[0].status == "Enabled" && aws_s3_bucket_ownership_controls.backups.rule[0].object_ownership == "BucketOwnerEnforced"
    error_message = "Backups must be encrypted and versioned with ACLs disabled."
  }

  assert {
    condition     = aws_s3_bucket_lifecycle_configuration.backups.rule[0].noncurrent_version_expiration[0].noncurrent_days == 14 && aws_s3_bucket_lifecycle_configuration.backups.rule[0].abort_incomplete_multipart_upload[0].days_after_initiation == 1 && length(aws_s3_bucket_lifecycle_configuration.backups.rule[0].expiration) == 0
    error_message = "Only superseded versions and incomplete uploads expire; current completed recovery points must survive idle periods."
  }

  assert {
    condition     = jsondecode(aws_s3_bucket_policy.backups.policy).Statement[0].Effect == "Deny" && jsondecode(aws_s3_bucket_policy.backups.policy).Statement[0].Condition.Bool["aws:SecureTransport"] == "false"
    error_message = "Backup access must require TLS."
  }
}

run "host_credentials_only_manage_named_backups_and_ssm_channels" {
  command = plan

  assert {
    condition     = jsondecode(aws_iam_role.host.assume_role_policy).Statement == [{ Action = "sts:AssumeRole", Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" } }]
    error_message = "Only EC2 may assume the host role for temporary instance-profile credentials."
  }

  assert {
    condition     = toset(jsondecode(aws_iam_role_policy.backups.policy).Statement[1].Resource) == toset(["arn:aws:s3:::minecraft-test-example-backups/instances/survival/*"]) && toset(jsondecode(aws_iam_role_policy.backups.policy).Statement[1].Action) == toset(["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"])
    error_message = "The host may manage current objects only in survival's backup namespace, without deleting retained versions or managing buckets."
  }

  assert {
    condition     = jsondecode(aws_iam_role_policy.backups.policy).Statement[0].Condition.StringLike["s3:prefix"] == ["instances/survival/*"] && jsondecode(aws_iam_role_policy.backups.policy).Statement[0].Action == ["s3:ListBucket"]
    error_message = "Backup listing must be restricted to the configured instance prefix."
  }

  assert {
    condition     = toset(jsondecode(aws_iam_role_policy.ssm.policy).Statement[0].Action) == toset(["ssm:UpdateInstanceInformation", "ssmmessages:CreateControlChannel", "ssmmessages:CreateDataChannel", "ssmmessages:OpenControlChannel", "ssmmessages:OpenDataChannel"]) && jsondecode(aws_iam_role_policy.ssm.policy).Statement[0].Resource == "*"
    error_message = "The wildcard host permissions must be limited to SSM registration and channel operations, with no parameter, IAM, EC2, or state access."
  }

  assert {
    condition     = length(jsondecode(aws_iam_role_policy.ssm.policy).Statement) == 1 && length(jsondecode(aws_iam_role_policy.backups.policy).Statement) == 3 && jsondecode(aws_iam_role_policy.backups.policy).Statement[2].Action == ["s3:GetBucketLocation"] && jsondecode(aws_iam_role_policy.backups.policy).Statement[2].Resource == ["arn:aws:s3:::minecraft-test-example-backups"] && jsondecode(aws_iam_role_policy.backups.policy).Statement[0].Resource == ["arn:aws:s3:::minecraft-test-example-backups"]
    error_message = "No additional host permissions may bypass the reviewed SSM and named-backup scopes."
  }
}

run "game_only_ingress_and_ssm_only_administration" {
  command = plan

  assert {
    condition     = length(aws_vpc_security_group_ingress_rule.game) == 2 && aws_vpc_security_group_ingress_rule.game["java"].ip_protocol == "tcp" && aws_vpc_security_group_ingress_rule.game["java"].from_port == 25565 && aws_vpc_security_group_ingress_rule.game["java"].to_port == 25565 && aws_vpc_security_group_ingress_rule.game["bedrock"].ip_protocol == "udp" && aws_vpc_security_group_ingress_rule.game["bedrock"].from_port == 19132 && aws_vpc_security_group_ingress_rule.game["bedrock"].to_port == 19132
    error_message = "Only Java TCP 25565 and Bedrock UDP 19132 may accept inbound connections; SSH and RCON must remain closed."
  }

  assert {
    condition     = aws_instance.host.metadata_options[0].http_tokens == "required" && aws_instance.host.metadata_options[0].http_put_response_hop_limit == 1 && aws_instance.host.metadata_options[0].instance_metadata_tags == "disabled"
    error_message = "Host credentials must require IMDSv2 and must not be exposed through an extra container-network hop."
  }

  assert {
    condition     = !aws_subnet.public.map_public_ip_on_launch && !aws_instance.host.associate_public_ip_address && aws_eip.host.domain == "vpc" && aws_route.internet.destination_cidr_block == "0.0.0.0/0"
    error_message = "The host must use the independently owned Elastic IP and public internet route."
  }
}

run "budget_notifies_on_account_cost_and_output_uses_instance_paths" {
  command = plan

  assert {
    condition     = aws_budgets_budget.monthly.budget_type == "COST" && aws_budgets_budget.monthly.limit_amount == "25" && aws_budgets_budget.monthly.limit_unit == "USD" && aws_budgets_budget.monthly.time_unit == "MONTHLY" && one(aws_budgets_budget.monthly.notification).notification_type == "ACTUAL" && one(aws_budgets_budget.monthly.notification).threshold == 100
    error_message = "The default monthly account budget must notify at $25 actual cost."
  }

  assert {
    condition     = output.deployment.region == "us-west-2" && output.deployment.mount_path == "/srv/minecraft" && output.deployment.instances.survival.data_path == "/srv/minecraft/instances/survival/data" && output.deployment.instances.survival.backup_prefix == "instances/survival/" && output.deployment.data_filesystem_uuid == "12345678-1234-4234-8234-123456789abc"
    error_message = "The private deployment contract must preserve filesystem identity and separate named-instance directories and backup prefixes."
  }
}

run "additional_named_instance_gets_separate_paths_and_prefix" {
  command = plan
  variables {
    instance_ids = ["survival", "creative-2"]
  }
  assert {
    condition     = output.deployment.instances["creative-2"].data_path == "/srv/minecraft/instances/creative-2/data" && output.deployment.instances["creative-2"].backup_prefix == "instances/creative-2/" && toset(jsondecode(aws_iam_role_policy.backups.policy).Statement[1].Resource) == toset(["arn:aws:s3:::minecraft-test-example-backups/instances/survival/*", "arn:aws:s3:::minecraft-test-example-backups/instances/creative-2/*"])
    error_message = "Adding a future instance ID must preserve separate data directories and authorize only its own backup prefix."
  }
}

run "reject_invalid_instance_id" {
  command = plan
  variables {
    instance_ids = ["../survival"]
  }
  expect_failures = [var.instance_ids]
}

run "reject_an_operator_role_in_another_account" {
  command = plan
  variables {
    operator_role_arn = "arn:aws:iam::999900001111:role/minecraft-operator"
  }
  expect_failures = [var.operator_role_arn]
}

run "reject_data_disk_az_outside_region" {
  command = plan
  variables {
    availability_zone = "us-east-1a"
  }
  expect_failures = [var.availability_zone]
}

run "reject_ambiguous_filesystem_identity" {
  command = plan
  variables {
    data_filesystem_uuid = "auto"
  }
  expect_failures = [var.data_filesystem_uuid]
}
