terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {}

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

data "aws_ami" "amazon_linux" {
  most_recent = true
  owners      = ["amazon"]
  filter {
    name   = "name"
    values = ["al2023-ami-2023.*-arm64"]
  }
}

resource "aws_iam_role" "runner" {
  name = "${var.name}-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.runner.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "runner" {
  name = "${var.name}-profile"
  role = aws_iam_role.runner.name
}

resource "aws_security_group" "runner" {
  name        = "${var.name}-egress"
  description = "Outbound-only migration runner"
  vpc_id      = data.aws_vpc.default.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_instance" "runner" {
  ami                    = data.aws_ami.amazon_linux.id
  instance_type          = var.instance_type
  subnet_id              = data.aws_subnets.default.ids[0]
  iam_instance_profile   = aws_iam_instance_profile.runner.name
  vpc_security_group_ids = [aws_security_group.runner.id]

  root_block_device {
    encrypted             = true
    volume_type           = "gp3"
    volume_size           = var.root_volume_gb
    iops                  = 3000
    throughput            = 250
    delete_on_termination = true
  }

  user_data = <<-EOT
    #!/bin/bash
    set -euxo pipefail
    dnf install -y git
    curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
    curl -fsSL https://bt.dev/cli/install.sh -o /tmp/install-bt.sh
    XDG_BIN_HOME=/usr/local/bin bash /tmp/install-bt.sh --version 0.14.0
    git clone --branch '${var.repo_ref}' '${var.repo_url}' /opt/opik-migrate
    cd /opt/opik-migrate
    UV_PROJECT_ENVIRONMENT=/opt/opik-migrate/.venv uv sync --frozen
  EOT

  tags = { Name = var.name }
}
