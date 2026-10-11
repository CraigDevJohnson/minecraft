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

variable "host_permissions_boundary_arn" {
  description = "Administrator-owned managed policy, named outside the operator's IAM prefix, that caps every host role the operator can create or pass."
  type        = string
  validation {
    condition     = can(regex("^arn:aws:iam::[0-9]{12}:policy/.+$", var.host_permissions_boundary_arn)) && try(split(":", var.host_permissions_boundary_arn)[4], "") == var.account_id
    error_message = "host_permissions_boundary_arn must name a managed policy in the target account."
  }
  validation {
    condition     = !startswith(lower(reverse(split("/", var.host_permissions_boundary_arn))[0]), "${var.resource_prefix}-")
    error_message = "host_permissions_boundary_arn must name a policy outside the resource prefix, so prefix-scoped operator permissions never cover it."
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

variable "data_volume_size_gib" {
  description = "Retained data disk size in GiB. EBS grows online but never shrinks: only ever increase this for an existing disk (docs/operations/data-disk-capacity.md). Recommended start: 5."
  type        = number
  nullable    = false
  validation {
    condition     = var.data_volume_size_gib >= 2 && var.data_volume_size_gib <= 128 && floor(var.data_volume_size_gib) == var.data_volume_size_gib
    error_message = "data_volume_size_gib must be a whole number of GiB from 2 through 128."
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

variable "bedrock_transport" {
  description = "Bedrock network transport matching the selected runtime. Both includes built-in signaling and WebRTC; raknet preserves legacy UDP only."
  type        = string
  default     = "both"
  nullable    = false
  validation {
    condition     = contains(["both", "raknet"], var.bedrock_transport)
    error_message = "bedrock_transport must be both or raknet."
  }
}

variable "additional_game_ingress" {
  description = "Named game listeners for additional independent server instances on this host."
  type        = map(object({ protocol = string, port = number }))
  default     = {}
  validation {
    condition = alltrue([for name, listener in var.additional_game_ingress :
      can(regex("^[a-z][a-z0-9_-]{0,31}$", name)) &&
      !contains(["java", "bedrock", "signaling", "webrtc"], name) &&
      contains(["tcp", "udp"], listener.protocol) &&
      listener.port >= 1024 && listener.port <= 65535 && floor(listener.port) == listener.port && listener.port != 25575
    ])
    error_message = "Additional listeners require distinct names, TCP/UDP and integer game ports 1024-65535, excluding RCON."
  }
  validation {
    condition = length(toset(concat(
      ["tcp:25565", "udp:19132", "tcp:19132", "udp:19133"],
      [for listener in values(var.additional_game_ingress) : "${listener.protocol}:${listener.port}"]
    ))) == 4 + length(var.additional_game_ingress)
    error_message = "Additional game listeners must not duplicate existing or additional protocol/port bindings."
  }
}

variable "public_hostname" {
  description = "Private DNS hostname that players use. The operator command points its existing Cloudflare A record at the running host and parks it while the host is stopped."
  type        = string
  validation {
    condition     = length(var.public_hostname) <= 253 && can(regex("^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\\.)+[a-z]([a-z0-9-]{0,61}[a-z0-9])?$", var.public_hostname))
    error_message = "public_hostname must be a lowercase DNS hostname of at most 253 characters with at least one dot and no scheme, port, or trailing dot."
  }
}

variable "cloudflare_zone_id" {
  description = "Cloudflare zone ID of the zone that holds public_hostname. The API token never belongs in OpenTofu inputs or state."
  type        = string
  validation {
    condition     = can(regex("^[0-9a-f]{32}$", var.cloudflare_zone_id))
    error_message = "cloudflare_zone_id must be a 32-character lowercase hexadecimal zone ID."
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

variable "idle_stop" {
  description = "Optional scheduled function that drains and stops an idle host. package_path is the zip built by `task lambda:package`; null deploys none of its resources."
  type = object({
    package_path                = string
    cloudflare_token_secret_arn = string
    idle_minutes                = optional(number, 30)
    permissions_boundary_arn    = optional(string)
  })
  default = null
  validation {
    condition     = var.idle_stop == null || try(can(regex("^arn:aws:secretsmanager:[a-z0-9-]+:[0-9]{12}:secret:[A-Za-z0-9/_+=.@-]+-[A-Za-z0-9]{6}$", var.idle_stop.cloudflare_token_secret_arn)) && split(":", var.idle_stop.cloudflare_token_secret_arn)[3] == var.region && split(":", var.idle_stop.cloudflare_token_secret_arn)[4] == var.account_id, false)
    error_message = "idle_stop.cloudflare_token_secret_arn must name an administrator-owned secret in the deployment account and region."
  }
  validation {
    condition     = var.idle_stop == null || length(var.instance_ids) == 1
    error_message = "idle_stop supports exactly one game instance on Java port 25565."
  }
  validation {
    condition     = var.idle_stop == null || try(var.idle_stop.idle_minutes >= 10 && var.idle_stop.idle_minutes <= 240 && floor(var.idle_stop.idle_minutes) == var.idle_stop.idle_minutes, false)
    error_message = "idle_stop.idle_minutes must be a whole number from 10 to 240."
  }
  validation {
    condition     = var.idle_stop == null || try(var.idle_stop.permissions_boundary_arn == null || (can(regex("^arn:aws:iam::[0-9]{12}:policy/.+$", var.idle_stop.permissions_boundary_arn)) && split(":", var.idle_stop.permissions_boundary_arn)[4] == var.account_id), false)
    error_message = "idle_stop.permissions_boundary_arn must name a managed policy in the target account."
  }
}

variable "launcher" {
  description = "Optional backend for The Lobby's Minecraft Launcher: status and start through a Function URL only site_role_arn may call. package_path is the zip built by `task lambda:package:launcher`; permissions_boundary_arn is the administrator-created launcher boundary; the function is named <resource_prefix>-<site_environment>-launcher; null deploys none of its resources."
  type = object({
    package_path               = string
    site_role_arn              = string
    site_environment           = string
    server_name                = optional(string, "Family server")
    cloudflare_token_parameter = optional(string)
    permissions_boundary_arn   = string
  })
  default = null
  validation {
    condition     = var.launcher == null || try(can(regex("^arn:aws:iam::[0-9]{12}:role/.+$", var.launcher.site_role_arn)), false)
    error_message = "launcher.site_role_arn must name the site's IAM role."
  }
  validation {
    condition     = var.launcher == null || try(var.launcher.cloudflare_token_parameter == null || can(regex("^/${var.resource_prefix}/[A-Za-z0-9/_.-]+$", var.launcher.cloudflare_token_parameter)), false)
    error_message = "launcher.cloudflare_token_parameter must be an SSM parameter under /<resource_prefix>/."
  }
  validation {
    condition     = var.launcher == null || try(can(regex("^[a-z][a-z0-9]{0,11}$", var.launcher.site_environment)), false)
    error_message = "launcher.site_environment must be the calling site's environment name, such as dev or prod: up to 12 lowercase letters and digits."
  }
  validation {
    condition     = var.launcher == null || try(length(trimspace(var.launcher.server_name)) > 0 && length(var.launcher.server_name) <= 60, false)
    error_message = "launcher.server_name must be 1 to 60 characters."
  }
  validation {
    condition     = var.launcher == null || length(var.instance_ids) == 1
    error_message = "launcher supports exactly one game instance on Java port 25565."
  }
  validation {
    condition     = var.launcher == null || var.lifecycle_mode == "retained-host"
    error_message = "launcher starts the retained host and is incompatible with on-demand and S3-only lifecycles."
  }
  validation {
    condition     = var.launcher == null || try(can(regex("^arn:aws:iam::[0-9]{12}:policy/.+$", var.launcher.permissions_boundary_arn)) && split(":", var.launcher.permissions_boundary_arn)[4] == var.account_id, false)
    error_message = "launcher.permissions_boundary_arn must name the administrator-created launcher boundary, a managed policy in the target account. The operator role may create the launcher's role only inside it."
  }
  validation {
    condition     = var.launcher == null || try(!startswith(lower(reverse(split("/", var.launcher.permissions_boundary_arn))[0]), "${var.resource_prefix}-"), false)
    error_message = "launcher.permissions_boundary_arn must name a policy outside the resource prefix, so prefix-scoped operator permissions never cover it."
  }
}

variable "lifecycle_mode" {
  description = "Opt-in session host removal. Retained-host preserves existing stop/start operation. Activation requires a reviewed recurring policy and initial migration."
  type        = string
  default     = "retained-host"
  validation {
    condition     = contains(["retained-host", "on-demand-host", "s3-only"], var.lifecycle_mode)
    error_message = "lifecycle_mode must be retained-host, on-demand-host, or s3-only."
  }
  validation {
    condition     = var.lifecycle_mode == "retained-host" || var.idle_stop == null
    error_message = "On-demand host mode is incompatible with idle_stop's fixed host identity."
  }
}

variable "session_present" {
  description = "Session host and attachment presence; protected data/network/IAM/backups/state remain retained."
  type        = bool
  default     = true
  validation {
    condition     = var.session_present || var.lifecycle_mode == "on-demand-host" || (var.lifecycle_mode == "s3-only" && var.data_ownership == "session")
    error_message = "Only opt-in on-demand mode permits an absent session host."
  }
}

variable "bootstrap_sha256" {
  description = "Pinned immutable runtime archive SHA-256 under bootstrap/releases/<digest>/runtime.tar.gz in the private backup bucket. Never contains credentials."
  type        = string
  default     = null
  validation {
    condition     = var.lifecycle_mode == "retained-host" || try(can(regex("^[0-9a-f]{64}$", var.bootstrap_sha256)), false)
    error_message = "On-demand mode requires a pinned bootstrap archive SHA-256."
  }
}

variable "data_ownership" {
  description = "Retained keeps existing data protected. Session requires the separately reviewed no-change ownership/protection migration before S3-only deletion authority."
  type        = string
  default     = "retained"
  validation {
    condition     = contains(["retained", "session"], var.data_ownership) && (var.data_ownership == "retained" || var.lifecycle_mode == "s3-only")
    error_message = "Session data ownership is allowed only in s3-only mode after reviewed migration."
  }
}

variable "recovery_receipt_ids" {
  description = "Exact independently verified and durable current recovery selections. S3-only off-state requires one receipt for every configured instance; never catalog latest."
  type        = map(string)
  default     = {}
  validation {
    condition     = alltrue([for id in values(var.recovery_receipt_ids) : can(regex("^[0-9a-f]{64}$", id))])
    error_message = "Each recovery receipt ID must be a lowercase SHA-256."
  }
  validation {
    condition     = var.lifecycle_mode != "s3-only" || var.session_present || toset(keys(var.recovery_receipt_ids)) == var.instance_ids
    error_message = "S3-only absent-data handoff requires a verified receipt for every configured instance."
  }
}
