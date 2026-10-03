data "aws_ami" "host" {
  count  = var.session_present ? 1 : 0
  owners = [var.ami_owner_account_id]
  filter {
    name   = "image-id"
    values = [var.ami_id]
  }
  filter {
    name   = "architecture"
    values = ["arm64"]
  }
  filter {
    name   = "root-device-type"
    values = ["ebs"]
  }
  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
  filter {
    name   = "state"
    values = ["available"]
  }
}

moved {
  from = aws_instance.host
  to   = aws_instance.host[0]
}

moved {
  from = aws_volume_attachment.data
  to   = aws_volume_attachment.data[0]
}

locals {
  data_volume_id = var.data_ownership == "retained" ? aws_ebs_volume.data[0].id : try(aws_ebs_volume.session_data[0].id, null)
}

moved {
  from = aws_ebs_volume.data
  to   = aws_ebs_volume.data[0]
}

resource "aws_instance" "host" {
  count                       = var.session_present ? 1 : 0
  ami                         = data.aws_ami.host[0].id
  instance_type               = "m7g.large"
  availability_zone           = var.availability_zone
  subnet_id                   = aws_subnet.public.id
  vpc_security_group_ids      = [aws_security_group.host.id]
  associate_public_ip_address = true
  iam_instance_profile        = aws_iam_instance_profile.host.name
  monitoring                  = false
  user_data_replace_on_change = true
  user_data_base64 = base64gzip(join("", [templatefile("${path.module}/../scripts/host-bootstrap.sh.tftpl", {
    storage_script_base64 = filebase64("${path.module}/../scripts/minecraft_storage.py")
    data_volume_id        = local.data_volume_id
    data_filesystem_uuid  = var.data_filesystem_uuid
    }), var.lifecycle_mode == "retained-host" ? "" : templatefile("${path.module}/../scripts/on-demand-bootstrap.sh.tftpl", {
    cli_installer_base64  = filebase64("${path.module}/../scripts/install_aws_cli.sh")
    release_script_base64 = filebase64("${path.module}/../scripts/minecraft_bootstrap_release.py")
  })]))

  root_block_device {
    encrypted             = true
    volume_type           = "gp3"
    volume_size           = 20
    delete_on_termination = true
    tags                  = { Name = "${var.resource_prefix}-root", Purpose = "replaceable-root" }
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
    instance_metadata_tags      = "disabled"
  }

  tags = { Name = "${var.resource_prefix}-host", Purpose = "replaceable-host" }
  lifecycle {
    # EC2 releases the auto-assigned address when the host stops, and the provider then
    # reads false: without this, any plan taken while the host is stopped replaces it.
    ignore_changes = [associate_public_ip_address]
  }
  depends_on = [
    aws_iam_role_policy.ssm,
    aws_iam_role_policy.backups,
    aws_iam_role_policy.bootstrap,
    aws_vpc_security_group_egress_rule.web,
    # First boot installs packages and registers with SSM through the public route.
    aws_route.internet,
    aws_route_table_association.public,
  ]
}

resource "aws_ebs_volume" "data" {
  count             = var.data_ownership == "retained" ? 1 : 0
  availability_zone = var.availability_zone
  encrypted         = true
  type              = "gp3"
  size              = var.data_volume_size_gib
  tags              = { Name = "${var.resource_prefix}-data", Purpose = "retained-instance-data" }
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_volume_attachment" "data" {
  count        = var.session_present ? 1 : 0
  device_name  = "/dev/sdf"
  volume_id    = local.data_volume_id
  instance_id  = aws_instance.host[0].id
  force_detach = false
  skip_destroy = false
}

# Import/move the existing disk here only through the separately reviewed no-change
# ownership/protection migration. The retained address above remains protected.
resource "aws_ebs_volume" "session_data" {
  count             = var.data_ownership == "session" && var.session_present ? 1 : 0
  availability_zone = var.availability_zone
  encrypted         = true
  type              = "gp3"
  size              = var.data_volume_size_gib
  # Preserve physical attributes/tags during ownership migration; the address and
  # reviewed controller policy carry session ownership, not a destructive retag.
  tags = { Name = "${var.resource_prefix}-data", Purpose = "retained-instance-data" }
}
