data "aws_ami" "host" {
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

resource "aws_instance" "host" {
  ami                         = data.aws_ami.host.id
  instance_type               = "m7g.large"
  availability_zone           = var.availability_zone
  subnet_id                   = aws_subnet.public.id
  vpc_security_group_ids      = [aws_security_group.host.id]
  associate_public_ip_address = true
  iam_instance_profile        = aws_iam_instance_profile.host.name
  monitoring                  = false
  user_data_replace_on_change = true
  user_data_base64 = base64gzip(templatefile("${path.module}/../scripts/host-bootstrap.sh.tftpl", {
    storage_script_base64 = filebase64("${path.module}/../scripts/minecraft_storage.py")
    data_volume_id        = aws_ebs_volume.data.id
    data_filesystem_uuid  = var.data_filesystem_uuid
  }))

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
    aws_vpc_security_group_egress_rule.web,
    # First boot installs packages and registers with SSM through the public route.
    aws_route.internet,
    aws_route_table_association.public,
  ]
}

resource "aws_ebs_volume" "data" {
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
  device_name  = "/dev/sdf"
  volume_id    = aws_ebs_volume.data.id
  instance_id  = aws_instance.host.id
  force_detach = false
  skip_destroy = false
}
