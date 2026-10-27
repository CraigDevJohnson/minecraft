output "deployment" {
  description = "Private runtime/operator handoff; save with umask 077 outside Git. State-bootstrap has its own private output."
  sensitive   = true
  value = {
    schema_version        = 2
    data_volume_size_gib  = var.data_volume_size_gib
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
    host_id               = aws_instance.host.id
    host_role_arn         = aws_iam_role.host.arn
    public_hostname       = var.public_hostname
    cloudflare_zone_id    = var.cloudflare_zone_id
    data_volume_id        = local.data_volume_id
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

output "launchers" {
  description = "Per Lobby environment the launcher serves, what that site's deployment needs: url is the Function URL (the-lobby's minecraft_backend_url) and function_arn the function behind it (minecraft_backend_function_arn). Only that environment's site role may call it. Empty when the launcher is not deployed."
  value = {
    for environment, function_url in aws_lambda_function_url.launcher : environment => {
      url          = function_url.function_url
      function_arn = local.launcher_arns[environment]
    }
  }
}
