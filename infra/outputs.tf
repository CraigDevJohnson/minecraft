output "deployment" {
  description = "Private runtime/operator handoff; save with umask 077 outside Git. State-bootstrap has its own private output."
  sensitive   = true
  value = {
    account_id           = var.account_id
    environment          = var.environment
    region               = var.region
    availability_zone    = var.availability_zone
    vpc_id               = aws_vpc.minecraft.id
    subnet_id            = aws_subnet.public.id
    security_group_id    = aws_security_group.host.id
    host_id              = aws_instance.host.id
    host_role_arn        = aws_iam_role.host.arn
    public_hostname      = var.public_hostname
    cloudflare_zone_id   = var.cloudflare_zone_id
    data_volume_id       = aws_ebs_volume.data.id
    data_filesystem_uuid = var.data_filesystem_uuid
    backup_bucket_name   = aws_s3_bucket.backups.bucket
    mount_path           = "/srv/minecraft"
    instances = {
      for id in sort(tolist(var.instance_ids)) : id => {
        data_path     = "/srv/minecraft/instances/${id}/data"
        backup_prefix = "instances/${id}/"
      }
    }
  }
}
