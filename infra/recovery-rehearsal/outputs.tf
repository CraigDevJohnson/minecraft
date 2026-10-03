output "deployment" {
  description = "Private target runtime handoff; save with mode 0600 outside Git."
  sensitive   = true
  value = {
    account_id           = var.account_id
    environment          = var.environment
    region               = var.region
    availability_zone    = var.availability_zone
    vpc_id               = var.source_vpc_id
    subnet_id            = var.source_subnet_id
    security_group_id    = aws_security_group.recovery.id
    host_id              = aws_instance.recovery.id
    host_role_arn        = aws_iam_role.recovery.arn
    data_volume_id       = aws_ebs_volume.recovery.id
    data_filesystem_uuid = var.data_filesystem_uuid
    backup_bucket_name   = var.recovery_backup_bucket_name
    mount_path           = "/srv/minecraft"
    instances = {
      (var.instance_id) = {
        data_path     = "/srv/minecraft/instances/${var.instance_id}/data"
        backup_prefix = "instances/${var.instance_id}/"
      }
    }
  }
}

output "cleanup_inventory" {
  description = "Exact temporary resources eligible for the separately approved cleanup."
  sensitive   = true
  value = {
    purpose               = "recovery-rehearsal"
    host_id               = aws_instance.recovery.id
    root_volume_id        = one(aws_instance.recovery.root_block_device).volume_id
    data_volume_id        = aws_ebs_volume.recovery.id
    security_group_id     = aws_security_group.recovery.id
    backup_bucket_name    = var.recovery_backup_bucket_name
    iam_role_name         = aws_iam_role.recovery.name
    instance_profile_name = aws_iam_instance_profile.recovery.name
  }
}
