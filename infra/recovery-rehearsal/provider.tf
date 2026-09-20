provider "aws" {
  region              = var.region
  allowed_account_ids = [var.account_id]
  assume_role {
    role_arn = var.operator_role_arn
  }
  default_tags {
    tags = {
      Project            = "minecraft"
      Environment        = var.environment
      ManagedBy          = "OpenTofu"
      Purpose            = "recovery-rehearsal"
      DeploymentRevision = var.deployment_revision
      SourceInstance     = var.instance_id
    }
  }
}
