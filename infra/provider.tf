provider "aws" {
  region              = var.region
  allowed_account_ids = [var.account_id]
  # The Deploy workflow already runs as the operator role through GitHub OIDC (ADR 0006); the workstation
  # names it here and assumes it from the administrator's SSO session.
  dynamic "assume_role" {
    for_each = var.operator_role_arn == null ? [] : [var.operator_role_arn]
    content {
      role_arn = assume_role.value
    }
  }
  default_tags {
    tags = {
      Project     = "minecraft"
      Environment = var.environment
      ManagedBy   = "OpenTofu"
    }
  }
}
