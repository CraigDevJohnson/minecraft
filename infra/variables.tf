variable "account_id" {
  description = "Reviewed target AWS account, supplied through private deployment configuration."
  type        = string
  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "account_id must be a 12-digit AWS account ID."
  }
}

variable "operator_role_arn" {
  description = "Existing operator role to assume; provisioning credentials never belong to the host."
  type        = string
  validation {
    condition     = can(regex("^arn:aws:iam::[0-9]{12}:role/.+$", var.operator_role_arn)) && try(split(":", var.operator_role_arn)[4], "") == var.account_id
    error_message = "operator_role_arn must name a role in the target account."
  }
}

variable "environment" {
  description = "Deployment environment, distinct from a server instance ID."
  type        = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,31}$", var.environment))
    error_message = "environment must begin with a lowercase letter and contain at most 32 lowercase letters, digits, or hyphens."
  }
}

variable "resource_prefix" {
  description = "Stable environment resource naming prefix; changing it can replace protected resources."
  type        = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,39}$", var.resource_prefix))
    error_message = "resource_prefix must contain 3-40 lowercase letters, digits, or hyphens and begin with a letter."
  }
}

variable "region" {
  description = "Reviewed deployment AWS region."
  type        = string
  default     = "us-west-2"
  validation {
    condition     = can(regex("^[a-z]{2}-[a-z]+-[1-9]$", var.region))
    error_message = "region must be a standard AWS region name."
  }
}

variable "availability_zone" {
  description = "Explicit stable AZ shared by the host and retained data disk; changing it requires recovery into a new disk."
  type        = string
  validation {
    condition     = can(regex("^[a-z]{2}-[a-z]+-[1-9][a-z]$", var.availability_zone)) && startswith(var.availability_zone, var.region)
    error_message = "availability_zone must be an AZ in the configured region."
  }
}

variable "ami_id" {
  description = "Reviewed Ubuntu 24.04 LTS ARM64 EBS AMI with Python 3, util-linux, e2fsprogs, cloud-init, and SSM Agent preinstalled."
  type        = string
  validation {
    condition     = can(regex("^ami-[0-9a-f]{17}$", var.ami_id))
    error_message = "ami_id must be an explicit reviewed AMI ID."
  }
}

variable "ami_owner_account_id" {
  description = "Reviewed publisher account for the exact AMI; verify it independently during deployment."
  type        = string
  validation {
    condition     = can(regex("^[0-9]{12}$", var.ami_owner_account_id))
    error_message = "ami_owner_account_id must be a 12-digit AWS account ID."
  }
}

variable "data_filesystem_uuid" {
  description = "Privately generated ext4 UUID; explicit blank-disk initialization must use this identity."
  type        = string
  validation {
    condition     = can(regex("^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", var.data_filesystem_uuid))
    error_message = "data_filesystem_uuid must be a lowercase UUID."
  }
}

variable "backup_bucket_name" {
  description = "Globally unique private backup bucket name, separate from the state bucket."
  type        = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,61}[a-z0-9]$", var.backup_bucket_name))
    error_message = "backup_bucket_name must contain 3-63 lowercase letters, digits, or hyphens."
  }
}

variable "instance_ids" {
  description = "Instance IDs authorized for separate backup namespaces. Initially only survival; adding an ID does not deploy a runtime."
  type        = set(string)
  default     = ["survival"]
  validation {
    condition     = length(var.instance_ids) > 0 && alltrue([for id in var.instance_ids : can(regex("^[a-z][a-z0-9-]{0,31}$", id))])
    error_message = "Each instance ID must begin with a lowercase letter and contain at most 32 lowercase letters, digits, or hyphens."
  }
}

variable "monthly_budget_usd" {
  description = "Monthly account cost notification threshold in USD; a budget is not a spending cap."
  type        = number
  default     = 25
  validation {
    condition     = var.monthly_budget_usd > 0
    error_message = "monthly_budget_usd must be positive."
  }
}

variable "budget_notification_email" {
  description = "Required private recipient supplied by Craig during issue #7."
  type        = string
  sensitive   = true
  validation {
    condition     = can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.budget_notification_email))
    error_message = "budget_notification_email must be an email address."
  }
}
