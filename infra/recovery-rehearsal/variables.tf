variable "account_id" {
  description = "Reviewed target AWS account."
  type        = string
  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "account_id must be a 12-digit AWS account ID."
  }
}

variable "operator_role_arn" {
  description = "Existing operator role used only for the approved rehearsal plan and apply."
  type        = string
  validation {
    condition     = can(regex("^arn:aws:iam::[0-9]{12}:role/.+$", var.operator_role_arn)) && try(split(":", var.operator_role_arn)[4], "") == var.account_id
    error_message = "operator_role_arn must name a role in the target account."
  }
}

variable "host_permissions_boundary_arn" {
  description = "Administrator-owned managed policy, named outside the operator's IAM prefix, that caps the recovery host role like every other host role the operator can create or pass."
  type        = string
  validation {
    condition     = can(regex("^arn:aws:iam::[0-9]{12}:policy/.+$", var.host_permissions_boundary_arn)) && try(split(":", var.host_permissions_boundary_arn)[4], "") == var.account_id
    error_message = "host_permissions_boundary_arn must name a managed policy in the target account."
  }
}

variable "environment" {
  description = "Temporary recovery environment name."
  type        = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,31}$", var.environment))
    error_message = "environment must be a valid lowercase environment name."
  }
}

variable "resource_prefix" {
  description = "Unique prefix for resources that will be removed after the rehearsal."
  type        = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,39}$", var.resource_prefix))
    error_message = "resource_prefix must contain 3-40 lowercase letters, digits, or hyphens."
  }
}

variable "deployment_revision" {
  description = "Full 40-character Git revision installed and tested."
  type        = string
  validation {
    condition     = can(regex("^[0-9a-f]{40}$", var.deployment_revision))
    error_message = "deployment_revision must be a full lowercase Git SHA."
  }
}

variable "region" {
  description = "Source deployment AWS region."
  type        = string
  default     = "us-west-2"
  validation {
    condition     = can(regex("^[a-z]{2}-[a-z]+-[1-9]$", var.region))
    error_message = "region must be a standard AWS region name."
  }
}

variable "availability_zone" {
  description = "Source deployment AZ; the temporary disk and host must share it."
  type        = string
  validation {
    condition     = can(regex("^[a-z]{2}-[a-z]+-[1-9][a-z]$", var.availability_zone)) && startswith(var.availability_zone, var.region)
    error_message = "availability_zone must be in the configured region."
  }
}

variable "ami_id" {
  description = "The exact reviewed ARM64 Ubuntu AMI used for the source deployment."
  type        = string
  validation {
    condition     = can(regex("^ami-[0-9a-f]{17}$", var.ami_id))
    error_message = "ami_id must be an explicit AMI ID."
  }
}

variable "ami_owner_account_id" {
  description = "Reviewed publisher account for the exact AMI."
  type        = string
  validation {
    condition     = can(regex("^[0-9]{12}$", var.ami_owner_account_id))
    error_message = "ami_owner_account_id must be a 12-digit AWS account ID."
  }
}

variable "source_vpc_id" {
  description = "Existing source VPC, consumed but never managed by this root."
  type        = string
  validation {
    condition     = can(regex("^vpc-[0-9a-f]{8,17}$", var.source_vpc_id))
    error_message = "source_vpc_id must be a VPC ID."
  }
}

variable "source_subnet_id" {
  description = "Existing source public subnet, consumed but never managed by this root."
  type        = string
  validation {
    condition     = can(regex("^subnet-[0-9a-f]{8,17}$", var.source_subnet_id))
    error_message = "source_subnet_id must be a subnet ID."
  }
}

variable "source_backup_bucket_name" {
  description = "Existing source backup bucket; this host receives read-only access to one instance prefix."
  type        = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,61}[a-z0-9]$", var.source_backup_bucket_name))
    error_message = "source_backup_bucket_name must be a valid bucket name."
  }
}

variable "recovery_backup_bucket_name" {
  description = "Unique temporary bucket for recovery-host backups; cleanup deletes it and all versions."
  type        = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,61}[a-z0-9]$", var.recovery_backup_bucket_name)) && var.recovery_backup_bucket_name != var.source_backup_bucket_name
    error_message = "recovery_backup_bucket_name must be a valid bucket name distinct from the source bucket."
  }
}

variable "restore_archive_size_bytes" {
  description = "Selected completed marker archive.size_bytes, measured before the recovery plan."
  type        = number
  validation {
    condition     = var.restore_archive_size_bytes > 0 && floor(var.restore_archive_size_bytes) == var.restore_archive_size_bytes
    error_message = "restore_archive_size_bytes must be a positive whole number of bytes."
  }
}

variable "restore_uncompressed_size_bytes" {
  description = "Sum of size_bytes for file entries in the selected completed marker."
  type        = number
  validation {
    condition     = var.restore_uncompressed_size_bytes > 0 && floor(var.restore_uncompressed_size_bytes) == var.restore_uncompressed_size_bytes
    error_message = "restore_uncompressed_size_bytes must be a positive whole number of bytes."
  }
}

variable "max_recovery_backup_archive_size_bytes" {
  description = "Operator-reviewed maximum archive size for a new backup of the restored world; at least the selected completed marker archive.size_bytes and no more than the backup program's 100 GiB limit."
  type        = number
  validation {
    condition     = var.max_recovery_backup_archive_size_bytes >= var.restore_archive_size_bytes && var.max_recovery_backup_archive_size_bytes <= 100 * 1073741824 && floor(var.max_recovery_backup_archive_size_bytes) == var.max_recovery_backup_archive_size_bytes
    error_message = "max_recovery_backup_archive_size_bytes must be whole bytes from the selected archive size through 100 GiB."
  }
}

variable "recovery_root_volume_size_gib" {
  description = "Encrypted replaceable root disk size in GiB; the plan reserves 20 GiB plus 10 percent headroom for the captured and verification copies of the maximum expected backup archive."
  type        = number
  validation {
    condition     = var.recovery_root_volume_size_gib >= 20 && floor(var.recovery_root_volume_size_gib) == var.recovery_root_volume_size_gib
    error_message = "recovery_root_volume_size_gib must be a whole number of at least 20 GiB."
  }
}

variable "recovery_data_volume_size_gib" {
  description = "Temporary target disk size in GiB; the recovery plan requires 10 percent staging headroom beyond the measured archive and extracted files."
  type        = number
  validation {
    condition     = var.recovery_data_volume_size_gib >= 40 && floor(var.recovery_data_volume_size_gib) == var.recovery_data_volume_size_gib
    error_message = "recovery_data_volume_size_gib must be a whole number of at least 40 GiB."
  }
}

variable "backup_writes_enabled" {
  description = "Grant writes to the temporary recovery bucket; set false in a separately reviewed plan for the failed-backup test."
  type        = bool
  default     = true
}

variable "instance_id" {
  description = "Named source instance restored in isolation."
  type        = string
  default     = "survival"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,31}$", var.instance_id))
    error_message = "instance_id must be a valid named instance ID."
  }
}

variable "data_filesystem_uuid" {
  description = "Fresh target ext4 UUID; it must differ from the source filesystem UUID."
  type        = string
  validation {
    condition     = can(regex("^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", var.data_filesystem_uuid)) && var.data_filesystem_uuid != var.source_data_filesystem_uuid
    error_message = "data_filesystem_uuid must be a lowercase UUID distinct from the source filesystem."
  }
}

variable "source_data_filesystem_uuid" {
  description = "Source filesystem UUID used only to reject a cloned or reused target identity."
  type        = string
  validation {
    condition     = can(regex("^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", var.source_data_filesystem_uuid))
    error_message = "source_data_filesystem_uuid must be the source deployment's lowercase UUID."
  }
}
