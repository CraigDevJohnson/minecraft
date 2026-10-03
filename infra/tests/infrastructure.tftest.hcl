mock_provider "aws" {
  override_resource {
    target = aws_iam_role.idle_stop
    values = { arn = "arn:aws:iam::111122223333:role/minecraft-test-idle-stop" }
  }

  override_resource {
    target = aws_sns_topic.idle_stop_alerts
    values = { arn = "arn:aws:sns:us-west-2:111122223333:minecraft-test-idle-stop-alerts" }
  }

  override_resource {
    target = aws_lambda_function.idle_stop
    values = { arn = "arn:aws:lambda:us-west-2:111122223333:function:minecraft-test-idle-stop" }
  }

  override_resource {
    target = aws_cloudwatch_event_rule.idle_stop
    values = { arn = "arn:aws:events:us-west-2:111122223333:rule/minecraft-test-idle-stop" }
  }
}

variables {
  account_id                    = "111122223333"
  operator_role_arn             = "arn:aws:iam::111122223333:role/minecraft-operator"
  environment                   = "test"
  resource_prefix               = "minecraft-test"
  availability_zone             = "us-west-2a"
  ami_id                        = "ami-0123456789abcdef0"
  ami_owner_account_id          = "099720109477"
  data_filesystem_uuid          = "12345678-1234-4234-8234-123456789abc"
  data_volume_size_gib          = 5
  backup_bucket_name            = "minecraft-test-example-backups"
  budget_notification_email     = "operator@example.invalid"
  host_permissions_boundary_arn = "arn:aws:iam::111122223333:policy/minecraft-host-boundary"
  public_hostname               = "mc.example.com"
  cloudflare_zone_id            = "0123456789abcdef0123456789abcdef"
}

run "retained_encrypted_data_and_replaceable_root" {
  command = plan

  assert {
    condition     = aws_ebs_volume.data.encrypted && aws_ebs_volume.data.type == "gp3" && aws_ebs_volume.data.size == 5
    error_message = "The retained data disk must be encrypted gp3 at the configured 5 GiB."
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

run "retained_data_disk_size_follows_the_private_input" {
  command = plan
  variables {
    data_volume_size_gib = 8
  }
  assert {
    condition     = aws_ebs_volume.data.size == 8 && aws_instance.host.root_block_device[0].volume_size == 20
    error_message = "The retained data disk must take its size from data_volume_size_gib and leave the root disk at 20 GiB."
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
    condition     = aws_iam_role.host.permissions_boundary == var.host_permissions_boundary_arn
    error_message = "The host role must carry the administrator-owned permissions boundary that caps any operator-written policy."
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
    condition     = aws_vpc_security_group_egress_rule.webrtc.ip_protocol == "udp" && aws_vpc_security_group_egress_rule.webrtc.from_port == 1 && aws_vpc_security_group_egress_rule.webrtc.to_port == 65535 && aws_vpc_security_group_egress_rule.webrtc.cidr_ipv4 == "0.0.0.0/0" && aws_vpc_security_group_egress_rule.webrtc.security_group_id == aws_security_group.host.id
    error_message = "The full ICE agent needs UDP egress to arbitrary client/NAT candidate ports, with no extra TCP egress."
  }

  assert {
    condition     = length(aws_vpc_security_group_ingress_rule.game) == 4 && aws_vpc_security_group_ingress_rule.game["java"].ip_protocol == "tcp" && aws_vpc_security_group_ingress_rule.game["java"].from_port == 25565 && aws_vpc_security_group_ingress_rule.game["java"].to_port == 25565 && aws_vpc_security_group_ingress_rule.game["bedrock"].ip_protocol == "udp" && aws_vpc_security_group_ingress_rule.game["bedrock"].from_port == 19132 && aws_vpc_security_group_ingress_rule.game["bedrock"].to_port == 19132 && aws_vpc_security_group_ingress_rule.game["signaling"].ip_protocol == "tcp" && aws_vpc_security_group_ingress_rule.game["signaling"].from_port == 19132 && aws_vpc_security_group_ingress_rule.game["signaling"].to_port == 19132 && aws_vpc_security_group_ingress_rule.game["webrtc"].ip_protocol == "udp" && aws_vpc_security_group_ingress_rule.game["webrtc"].from_port == 19133 && aws_vpc_security_group_ingress_rule.game["webrtc"].to_port == 19133 && alltrue([for rule in aws_vpc_security_group_ingress_rule.game : rule.cidr_ipv4 == "0.0.0.0/0" && rule.security_group_id == aws_security_group.host.id])
    error_message = "Only Java TCP 25565, RakNet UDP 19132, signaling TCP 19132 and WebRTC UDP 19133 may accept inbound connections; SSH and RCON must remain closed."
  }

  assert {
    condition     = aws_instance.host.metadata_options[0].http_tokens == "required" && aws_instance.host.metadata_options[0].http_put_response_hop_limit == 1 && aws_instance.host.metadata_options[0].instance_metadata_tags == "disabled"
    error_message = "Host credentials must require IMDSv2 and must not be exposed through an extra container-network hop."
  }

  assert {
    condition     = !aws_subnet.public.map_public_ip_on_launch && aws_instance.host.associate_public_ip_address && aws_route.internet.destination_cidr_block == "0.0.0.0/0"
    error_message = "Only the host may take a public IPv4 address, assigned at launch and on each start, with a public internet route."
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

  assert {
    condition     = output.deployment.public_hostname == "mc.example.com" && output.deployment.cloudflare_zone_id == "0123456789abcdef0123456789abcdef" && !contains(keys(output.deployment), "elastic_ip") && !contains(keys(output.deployment), "elastic_ip_id")
    error_message = "The private deployment contract must name the hostname and zone the operator command updates, and no Elastic IP."
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

run "reject_a_host_permissions_boundary_in_another_account" {
  command = plan
  variables {
    host_permissions_boundary_arn = "arn:aws:iam::999900001111:policy/minecraft-host-boundary"
  }
  expect_failures = [var.host_permissions_boundary_arn]
}

run "reject_a_host_permissions_boundary_that_is_not_a_managed_policy" {
  command = plan
  variables {
    host_permissions_boundary_arn = "arn:aws:iam::111122223333:role/minecraft-host-boundary"
  }
  expect_failures = [var.host_permissions_boundary_arn]
}

run "reject_a_host_permissions_boundary_named_inside_the_resource_prefix" {
  command = plan
  variables {
    host_permissions_boundary_arn = "arn:aws:iam::111122223333:policy/minecraft-test-host-boundary"
  }
  expect_failures = [var.host_permissions_boundary_arn]
}

run "reject_a_host_permissions_boundary_whose_path_hides_a_prefixed_name" {
  command = plan
  variables {
    host_permissions_boundary_arn = "arn:aws:iam::111122223333:policy/boundaries/Minecraft-Test-Boundary"
  }
  expect_failures = [var.host_permissions_boundary_arn]
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

run "idle_stop_is_absent_unless_configured" {
  command = plan

  assert {
    condition     = length(aws_lambda_function.idle_stop) == 0 && length(aws_iam_role.idle_stop) == 0 && length(aws_cloudwatch_event_rule.idle_stop) == 0 && length(aws_sns_topic.idle_stop_alerts) == 0
    error_message = "The idle stop must deploy nothing until its package is configured."
  }
}

run "idle_stop_drains_and_stops_only_this_host" {
  command = plan

  variables {
    idle_stop = { package_path = "tests/fixtures/idle-stop.placeholder", cloudflare_token_secret_arn = "arn:aws:secretsmanager:us-west-2:111122223333:secret:cloudflare-token-abcdef" }
  }

  override_resource {
    target = aws_instance.host
    values = { id = "i-0123456789abcdef0" }
  }

  assert {
    condition     = jsondecode(aws_iam_role.idle_stop[0].assume_role_policy).Statement == [{ Action = "sts:AssumeRole", Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" } }]
    error_message = "Only Lambda may assume the idle-stop role."
  }

  assert {
    condition     = aws_iam_role.idle_stop[0].permissions_boundary == null
    error_message = "No boundary is applied unless one is configured."
  }

  assert {
    condition = toset([for statement in jsondecode(aws_iam_role_policy.idle_stop[0].policy).Statement : jsonencode(statement)]) == toset([
      jsonencode({ Sid = "ObserveHost", Effect = "Allow", Action = ["ec2:DescribeInstances"], Resource = "*" }),
      jsonencode({ Sid = "StopOnlyThisHost", Effect = "Allow", Action = ["ec2:StopInstances"], Resource = ["arn:aws:ec2:us-west-2:111122223333:instance/i-0123456789abcdef0"] }),
      jsonencode({ Sid = "DrainOnlyThisHostWithShellDocument", Effect = "Allow", Action = ["ssm:SendCommand"], Resource = ["arn:aws:ec2:us-west-2:111122223333:instance/i-0123456789abcdef0", "arn:aws:ssm:us-west-2::document/AWS-RunShellScript"] }),
      jsonencode({ Sid = "FollowDrain", Effect = "Allow", Action = ["ssm:GetCommandInvocation"], Resource = "*" }),
      jsonencode({ Sid = "OwnStateOnly", Effect = "Allow", Action = ["ssm:GetParameter", "ssm:PutParameter"], Resource = ["arn:aws:ssm:us-west-2:111122223333:parameter/minecraft-test/idle-stop/state"] }),
      jsonencode({ Sid = "ReadOnlyCloudflareToken", Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = ["arn:aws:secretsmanager:us-west-2:111122223333:secret:cloudflare-token-abcdef"] }),
      jsonencode({ Sid = "OwnLogsOnly", Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = ["arn:aws:logs:us-west-2:111122223333:log-group:/aws/lambda/minecraft-test-idle-stop:*"] }),
    ])
    error_message = "The idle stop may stop and drain only this host, and touch only its own state and logs."
  }

  assert {
    condition     = aws_lambda_function.idle_stop[0].environment[0].variables == tomap({ HOST_ID = "i-0123456789abcdef0", STATE_PARAMETER = "/minecraft-test/idle-stop/state", PUBLIC_HOSTNAME = "mc.example.com", CLOUDFLARE_ZONE_ID = "0123456789abcdef0123456789abcdef", CLOUDFLARE_TOKEN_SECRET_ARN = "arn:aws:secretsmanager:us-west-2:111122223333:secret:cloudflare-token-abcdef", IDLE_MINUTES = "30", INSTANCES = "survival" })
    error_message = "The function must target this host, its own state, the game endpoint and the configured instances, with a 30-minute default."
  }

  assert {
    condition     = aws_lambda_function.idle_stop[0].architectures == tolist(["arm64"]) && aws_lambda_function.idle_stop[0].runtime == "provided.al2023" && aws_lambda_function.idle_stop[0].timeout == 60 && aws_lambda_function.idle_stop[0].reserved_concurrent_executions == 1
    error_message = "The function must run the Go ARM64 bootstrap with a bounded timeout and serialized invocations."
  }

  assert {
    condition     = aws_cloudwatch_event_rule.idle_stop[0].schedule_expression == "rate(5 minutes)" && aws_lambda_permission.idle_stop[0].principal == "events.amazonaws.com"
    error_message = "Only the five-minute schedule may invoke the idle check."
  }

  assert {
    condition     = aws_cloudwatch_metric_alarm.idle_stop_errors[0].metric_name == "Errors" && aws_cloudwatch_metric_alarm.idle_stop_errors[0].treat_missing_data == "notBreaching" && aws_sns_topic_subscription.idle_stop_alerts[0].protocol == "email" && aws_sns_topic_subscription.idle_stop_alerts[0].endpoint == "operator@example.invalid"
    error_message = "A failing or blocked idle stop must email the private budget recipient."
  }

  assert {
    condition     = aws_ssm_parameter.idle_stop_state[0].type == "String" && aws_cloudwatch_log_group.idle_stop[0].retention_in_days == 14
    error_message = "State must be a plain parameter and logs must expire."
  }
}

run "reject_idle_stop_threshold_out_of_range" {
  command = plan
  variables {
    idle_stop = { package_path = "tests/fixtures/idle-stop.placeholder", cloudflare_token_secret_arn = "arn:aws:secretsmanager:us-west-2:111122223333:secret:cloudflare-token-abcdef", idle_minutes = 5 }
  }
  expect_failures = [var.idle_stop]
}

run "idle_stop_role_takes_a_configured_boundary" {
  command = plan
  variables {
    idle_stop = { package_path = "tests/fixtures/idle-stop.placeholder", cloudflare_token_secret_arn = "arn:aws:secretsmanager:us-west-2:111122223333:secret:cloudflare-token-abcdef", permissions_boundary_arn = "arn:aws:iam::111122223333:policy/idle-stop-boundary" }
  }

  assert {
    condition     = aws_iam_role.idle_stop[0].permissions_boundary == "arn:aws:iam::111122223333:policy/idle-stop-boundary"
    error_message = "A configured boundary must cap the idle-stop role."
  }
}

run "reject_idle_stop_boundary_in_another_account" {
  command = plan
  variables {
    idle_stop = { package_path = "tests/fixtures/idle-stop.placeholder", cloudflare_token_secret_arn = "arn:aws:secretsmanager:us-west-2:111122223333:secret:cloudflare-token-abcdef", permissions_boundary_arn = "arn:aws:iam::444455556666:policy/idle-stop-boundary" }
  }
  expect_failures = [var.idle_stop]
}

run "reject_a_data_disk_too_small_to_restore_onto" {
  command = plan
  variables {
    data_volume_size_gib = 1
  }
  expect_failures = [var.data_volume_size_gib]
}

run "reject_a_fractional_data_disk_size" {
  command = plan
  variables {
    data_volume_size_gib = 2.5
  }
  expect_failures = [var.data_volume_size_gib]
}

run "reject_a_data_disk_larger_than_backups_can_protect" {
  command = plan
  variables {
    data_volume_size_gib = 129
  }
  expect_failures = [var.data_volume_size_gib]
}

run "reject_a_public_hostname_given_as_a_url" {
  command = plan
  variables {
    public_hostname = "https://mc.example.com"
  }
  expect_failures = [var.public_hostname]
}

run "reject_a_public_hostname_without_a_domain" {
  command = plan
  variables {
    public_hostname = "mc"
  }
  expect_failures = [var.public_hostname]
}

run "reject_an_uppercase_public_hostname" {
  command = plan
  variables {
    public_hostname = "MC.example.com"
  }
  expect_failures = [var.public_hostname]
}

run "reject_an_overlong_public_hostname" {
  command = plan
  variables {
    public_hostname = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  }
  expect_failures = [var.public_hostname]
}

run "reject_an_ip_address_as_public_hostname" {
  command = plan
  variables {
    public_hostname = "192.0.2.1"
  }
  expect_failures = [var.public_hostname]
}

run "reject_a_malformed_cloudflare_zone_id" {
  command = plan
  variables {
    cloudflare_zone_id = "0123456789ABCDEF0123456789ABCDEF"
  }
  expect_failures = [var.cloudflare_zone_id]
}

run "reject_idle_stop_for_multiple_game_instances" {
  command = plan
  variables {
    instance_ids = ["survival", "creative"]
    idle_stop    = { package_path = "tests/fixtures/idle-stop.placeholder", cloudflare_token_secret_arn = "arn:aws:secretsmanager:us-west-2:111122223333:secret:cloudflare-token-abcdef" }
  }
  expect_failures = [var.idle_stop]
}

run "reject_idle_stop_secret_in_another_account" {
  command = plan
  variables {
    idle_stop = { package_path = "tests/fixtures/idle-stop.placeholder", cloudflare_token_secret_arn = "arn:aws:secretsmanager:us-west-2:444455556666:secret:cloudflare-token-abcdef" }
  }
  expect_failures = [var.idle_stop]
}

run "reject_idle_stop_secret_in_another_region" {
  command = plan
  variables {
    idle_stop = { package_path = "tests/fixtures/idle-stop.placeholder", cloudflare_token_secret_arn = "arn:aws:secretsmanager:us-east-1:111122223333:secret:cloudflare-token-abcdef" }
  }
  expect_failures = [var.idle_stop]
}

run "reject_wildcard_idle_stop_secret_permissions" {
  command = plan
  variables {
    idle_stop = { package_path = "tests/fixtures/idle-stop.placeholder", cloudflare_token_secret_arn = "arn:aws:secretsmanager:us-west-2:111122223333:secret:*" }
  }
  expect_failures = [var.idle_stop]
}
