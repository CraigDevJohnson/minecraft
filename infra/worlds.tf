locals {
  # The pinned server content names the worlds; each gets a host of its own (ADR 0009).
  world_ids           = [for world in jsondecode(file("${path.module}/../runtime/crafty/server.json")).worlds : world.id]
  world_host_arns     = [for id in local.world_ids : "arn:aws:ec2:${var.region}:${var.account_id}:instance/${aws_instance.world[id].id}"]
  world_launcher_keys = var.launcher == null ? [] : local.world_ids
  world_idle_keys     = var.idle_stop == null ? [] : local.world_ids
  parameter_arn_base  = "arn:aws:ssm:${var.region}:${var.account_id}:parameter"
  # What the launcher and the idle stop both read as WORLDS: where each world runs, the address its players
  # use, and the two records of its starts and stops. A function that is not deployed leaves its record empty.
  worlds = jsonencode({
    for id in local.world_ids : id => {
      host_id                   = aws_instance.world[id].id
      hostname                  = "${id}.${var.public_hostname}"
      launcher_state_parameter  = var.launcher == null ? "" : aws_ssm_parameter.world_launcher_state[id].name
      idle_stop_state_parameter = var.idle_stop == null ? "" : aws_ssm_parameter.world_idle_stop_state[id].name
    }
  })
}

# A world lives on its host's own root disk: there is no data disk, and the archive stored at every stop is
# the recovery point. The user data holds nothing from server.json, so a content change never replaces a host;
# a replaced host sets itself up again and restores the newest archive at its world's next start.
resource "aws_instance" "world" {
  for_each                             = toset(local.world_ids)
  ami                                  = data.aws_ami.host.id
  instance_type                        = "m7g.large"
  availability_zone                    = var.availability_zone
  subnet_id                            = aws_subnet.public.id
  vpc_security_group_ids               = [aws_security_group.host.id]
  associate_public_ip_address          = true
  iam_instance_profile                 = aws_iam_instance_profile.host.name
  monitoring                           = false
  hibernation                          = false
  instance_initiated_shutdown_behavior = "stop"
  user_data_replace_on_change          = true
  user_data_base64 = base64gzip(templatefile("${path.module}/../scripts/world-bootstrap.sh.tftpl", {
    aws_cli_script_base64 = filebase64("${path.module}/../scripts/install_aws_cli.sh")
    world_id              = each.key
    backup_bucket         = var.backup_bucket_name
    region                = var.region
  }))

  root_block_device {
    encrypted             = true
    volume_type           = "gp3"
    volume_size           = 20
    delete_on_termination = true
    tags                  = { Name = "${var.resource_prefix}-${each.key}-root", Purpose = "replaceable-root" }
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
    instance_metadata_tags      = "disabled"
  }

  tags = { Name = "${var.resource_prefix}-${each.key}", World = each.key, Purpose = "replaceable-host" }
  lifecycle {
    # EC2 releases the auto-assigned address when the host stops, and the provider then
    # reads false: without this, any plan taken while the host is stopped replaces it.
    ignore_changes = [associate_public_ip_address]
  }
  depends_on = [
    aws_iam_role_policy.ssm,
    aws_iam_role_policy.backups,
    aws_vpc_security_group_egress_rule.web,
    # First boot installs packages and registers with SSM through the public route.
    aws_route.internet,
    aws_route_table_association.public,
  ]
}

# Every site's function shares a world's record, as they share its host: a start from one site shows as
# starting on the others.
resource "aws_ssm_parameter" "world_launcher_state" {
  for_each    = toset(local.world_launcher_keys)
  name        = "/${var.resource_prefix}/launcher/worlds/${each.key}/state"
  description = "The launcher's last start or stop of this world and its outcome; owned by the function after creation."
  type        = "String"
  value       = "{}"
  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_ssm_parameter" "world_idle_stop_state" {
  for_each    = toset(local.world_idle_keys)
  name        = "/${var.resource_prefix}/idle-stop/worlds/${each.key}/state"
  description = "This world's idle-stop timer and drain progress; owned by the function after creation."
  type        = "String"
  value       = "{}"
  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_ssm_parameter" "launcher_mods" {
  count       = local.launcher_count
  name        = "/${var.resource_prefix}/launcher/mods"
  description = "Which mods a Member switched away from the pinned default, per world; owned by the launcher after creation."
  type        = "String"
  value       = "{}"
  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_ssm_parameter" "launcher_allowlist" {
  count       = local.launcher_count
  name        = "/${var.resource_prefix}/launcher/allowlist"
  description = "The family allowlist every world takes at its start; owned by the launcher after creation."
  type        = "String"
  value       = "[]"
  lifecycle {
    ignore_changes = [value]
  }
}
