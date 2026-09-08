variable "account_id" {
  description = "Private AWS target account; the provider refuses any other account."
  type        = string
  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "Supply the reviewed 12-digit AWS account ID."
  }
}

variable "operator_role_arn" {
  description = "Existing authorized operator role, also used for backend access."
  type        = string
  validation {
    condition     = can(regex("^arn:aws:iam::${var.account_id}:role/[A-Za-z0-9_+=,.@/-]+$", var.operator_role_arn))
    error_message = "Supply an operator role in the reviewed AWS account."
  }
}

variable "region" {
  description = "AWS deployment and state region."
  type        = string
  default     = "us-west-2"
}

variable "environment" {
  description = "Private environment label used in resource tags."
  type        = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,31}$", var.environment))
    error_message = "Use a lowercase environment name of at most 32 characters."
  }
}

variable "state_bucket_name" {
  description = "Private globally unique bucket name, separate from backups."
  type        = string
  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$", var.state_bucket_name))
    error_message = "Use a valid 3-63 character S3 bucket name without dots."
  }
}

variable "state_keys" {
  description = "Separate object keys for bootstrap and host infrastructure state."
  type = object({
    bootstrap      = string
    infrastructure = string
  })
  default = {
    bootstrap      = "bootstrap/terraform.tfstate"
    infrastructure = "infrastructure/terraform.tfstate"
  }
  validation {
    condition = var.state_keys.bootstrap != var.state_keys.infrastructure && alltrue([
      for key in values(var.state_keys) : can(regex("^[a-zA-Z0-9_-]+(/[a-zA-Z0-9_-]+)*/terraform[.]tfstate$", key))
    ])
    error_message = "Use distinct paths ending /terraform.tfstate; no wildcards or traversal."
  }
}
