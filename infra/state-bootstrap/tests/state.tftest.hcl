mock_provider "aws" {}

variables {
  account_id        = "123456789012"
  operator_role_arn = "arn:aws:iam::123456789012:role/example-operator"
  environment       = "test"
  state_bucket_name = "example-minecraft-state-offline"
}

run "private_retained_state" {
  command = plan

  assert {
    condition     = aws_s3_bucket.state.force_destroy == false && aws_s3_bucket_versioning.state.versioning_configuration[0].status == "Enabled"
    error_message = "State must keep object versions and refuse forced emptying."
  }
  assert {
    condition     = one(aws_s3_bucket_server_side_encryption_configuration.state.rule).apply_server_side_encryption_by_default[0].sse_algorithm == "AES256"
    error_message = "State must be encrypted at rest."
  }
  assert {
    condition     = aws_s3_bucket_public_access_block.state.block_public_acls && aws_s3_bucket_public_access_block.state.block_public_policy && aws_s3_bucket_public_access_block.state.ignore_public_acls && aws_s3_bucket_public_access_block.state.restrict_public_buckets && aws_s3_bucket_ownership_controls.state.rule[0].object_ownership == "BucketOwnerEnforced"
    error_message = "State must reject public access and ACLs."
  }
}

run "only_operator_and_exact_locked_keys" {
  command = plan

  assert {
    condition     = jsondecode(aws_s3_bucket_policy.state.policy).Statement[1].Condition.ArnNotEquals["aws:PrincipalArn"] == var.operator_role_arn
    error_message = "Host credentials and all non-operator principals must be denied state access."
  }
  assert {
    condition     = jsondecode(aws_s3_bucket_policy.state.policy).Statement[0].Condition.Bool["aws:SecureTransport"] == "false"
    error_message = "State must reject unencrypted transport."
  }
  assert {
    condition = toset(jsondecode(output.operator_backend_policy).Statement[1].Resource) == toset([
      "arn:aws:s3:::example-minecraft-state-offline/bootstrap/terraform.tfstate",
      "arn:aws:s3:::example-minecraft-state-offline/infrastructure/terraform.tfstate",
      "arn:aws:s3:::example-minecraft-state-offline/recovery-rehearsal/terraform.tfstate"
    ]) && toset(jsondecode(output.operator_backend_policy).Statement[1].Action) == toset(["s3:GetObject", "s3:PutObject"])
    error_message = "Ordinary state access must be confined to three keys without deletion."
  }
  assert {
    condition = toset(jsondecode(output.operator_backend_policy).Statement[2].Resource) == toset([
      "arn:aws:s3:::example-minecraft-state-offline/bootstrap/terraform.tfstate.tflock",
      "arn:aws:s3:::example-minecraft-state-offline/infrastructure/terraform.tfstate.tflock",
      "arn:aws:s3:::example-minecraft-state-offline/recovery-rehearsal/terraform.tfstate.tflock"
    ]) && contains(jsondecode(output.operator_backend_policy).Statement[2].Action, "s3:DeleteObject")
    error_message = "Lock access must include lock release, scoped to the three lock objects."
  }
  assert {
    condition     = output.backend_configs.bootstrap.use_lockfile && output.backend_configs.infrastructure.use_lockfile && output.backend_configs.recovery_rehearsal.use_lockfile && output.backend_configs.bootstrap.encrypt && length(toset([output.backend_configs.bootstrap.key, output.backend_configs.infrastructure.key, output.backend_configs.recovery_rehearsal.key])) == 3
    error_message = "All states require encryption, native S3 locking, and separate keys."
  }
}
