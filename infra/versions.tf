terraform {
  backend "s3" {
    encrypt      = true
    use_lockfile = true
  }

  required_version = "= 1.12.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "= 6.63.0"
    }
  }
}
