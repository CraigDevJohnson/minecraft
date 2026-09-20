data "aws_ami" "recovery" {
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

resource "aws_instance" "recovery" {
  ami                         = data.aws_ami.recovery.id
  instance_type               = "m7g.large"
  availability_zone           = var.availability_zone
  subnet_id                   = var.source_subnet_id
  vpc_security_group_ids      = [var.source_security_group_id]
  associate_public_ip_address = false
  iam_instance_profile        = aws_iam_instance_profile.recovery.name
  monitoring                  = false
  user_data_replace_on_change = true
  user_data_base64 = base64gzip(templatefile("${path.module}/../../scripts/host-bootstrap.sh.tftpl", {
    storage_script_base64 = filebase64("${path.module}/../../scripts/minecraft_storage.py")
    data_volume_id        = aws_ebs_volume.recovery.id
    data_filesystem_uuid  = var.data_filesystem_uuid
  }))

  root_block_device {
    encrypted             = true
    volume_type           = "gp3"
    volume_size           = 20
    delete_on_termination = true
    tags                  = { Name = "${var.resource_prefix}-root", Purpose = "recovery-rehearsal" }
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
    instance_metadata_tags      = "disabled"
  }

  tags       = { Name = "${var.resource_prefix}-host", Purpose = "recovery-rehearsal" }
  depends_on = [aws_iam_role_policy.ssm, aws_iam_role_policy.source_backup_read, aws_iam_role_policy.recovery_backups]
}

resource "aws_ebs_volume" "recovery" {
  availability_zone = var.availability_zone
  encrypted         = true
  type              = "gp3"
  size              = 40
  tags              = { Name = "${var.resource_prefix}-data", Purpose = "recovery-rehearsal" }
}

resource "aws_volume_attachment" "recovery" {
  device_name  = "/dev/sdf"
  volume_id    = aws_ebs_volume.recovery.id
  instance_id  = aws_instance.recovery.id
  force_detach = false
  skip_destroy = false
}

resource "aws_eip" "recovery" {
  domain = "vpc"
  tags   = { Name = "${var.resource_prefix}-address", Purpose = "recovery-rehearsal" }
}

resource "aws_eip_association" "recovery" {
  allocation_id = aws_eip.recovery.id
  instance_id   = aws_instance.recovery.id
}
