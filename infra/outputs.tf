output "deployment" {
  description = "Private runtime/operator handoff; save with umask 077 outside Git. State-bootstrap has its own private output."
  sensitive   = true
  value = {
    schema_version        = 2
    lifecycle_mode        = var.lifecycle_mode
    session_present       = var.session_present
    bootstrap_sha256      = var.bootstrap_sha256
    operator_role_arn     = var.operator_role_arn
    host_instance_profile = aws_iam_instance_profile.host.name
    ami_id                = var.ami_id
    account_id            = var.account_id
    environment           = var.environment
    resource_prefix       = var.resource_prefix
    region                = var.region
    availability_zone     = var.availability_zone
    vpc_id                = aws_vpc.minecraft.id
    subnet_id             = aws_subnet.public.id
    security_group_id     = aws_security_group.host.id
    host_id               = try(aws_instance.host[0].id, null)
    host_role_arn         = aws_iam_role.host.arn
    public_hostname       = var.public_hostname
    cloudflare_zone_id    = var.cloudflare_zone_id
    data_volume_id        = aws_ebs_volume.data.id
    data_filesystem_uuid  = var.data_filesystem_uuid
    backup_bucket_name    = aws_s3_bucket.backups.bucket
    mount_path            = "/srv/minecraft"
    instances = {
      for id in sort(tolist(var.instance_ids)) : id => {
        data_path     = "/srv/minecraft/instances/${id}/data"
        backup_prefix = "instances/${id}/"
      }
    }
  }
}
