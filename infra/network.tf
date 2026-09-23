resource "aws_vpc" "minecraft" {
  cidr_block           = "10.42.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = "${var.resource_prefix}-network", Purpose = "network" }
}

resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.minecraft.id
  cidr_block              = "10.42.1.0/24"
  availability_zone       = var.availability_zone
  map_public_ip_on_launch = false
  tags                    = { Name = "${var.resource_prefix}-public", Purpose = "network" }
}

resource "aws_internet_gateway" "public" {
  vpc_id = aws_vpc.minecraft.id
  tags   = { Name = "${var.resource_prefix}-internet", Purpose = "network" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.minecraft.id
  tags   = { Name = "${var.resource_prefix}-public", Purpose = "network" }
}

resource "aws_route" "internet" {
  route_table_id         = aws_route_table.public.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.public.id
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

resource "aws_security_group" "host" {
  name        = "${var.resource_prefix}-host"
  description = "Game ingress; administration through outbound SSM sessions only"
  vpc_id      = aws_vpc.minecraft.id
  tags        = { Name = "${var.resource_prefix}-host", Purpose = "host" }
}

resource "aws_vpc_security_group_ingress_rule" "game" {
  for_each = {
    java    = { protocol = "tcp", port = 25565 }
    bedrock = { protocol = "udp", port = 19132 }
  }
  security_group_id = aws_security_group.host.id
  description       = "${each.key} clients; runtime family allowlist is required"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = each.value.protocol
  from_port         = each.value.port
  to_port           = each.value.port
}

resource "aws_vpc_security_group_egress_rule" "web" {
  for_each          = toset(["80", "443"])
  security_group_id = aws_security_group.host.id
  description       = "Package, image, AWS API, and SSM traffic"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = tonumber(each.value)
  to_port           = tonumber(each.value)
}

resource "aws_eip" "host" {
  domain = "vpc"
  tags   = { Name = "${var.resource_prefix}-address", Purpose = "endpoint" }
}

resource "aws_eip_association" "host" {
  allocation_id = aws_eip.host.id
  instance_id   = aws_instance.host.id
  depends_on    = [aws_internet_gateway.public, aws_route.internet, aws_route_table_association.public]
}
