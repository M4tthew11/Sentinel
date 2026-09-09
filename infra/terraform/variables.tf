variable "project" {
  description = "Short project name used in every resource name."
  type        = string
  default     = "sentinel"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,14}$", var.project))
    error_message = "project must be 3-15 lowercase alphanumeric characters or hyphens."
  }
}

variable "environment" {
  description = "Deployment environment."
  type        = string
  default     = "dev"

  validation {
    condition     = contains(["dev", "test", "prod"], var.environment)
    error_message = "environment must be one of: dev, test, prod."
  }
}

variable "location" {
  description = "Azure region."
  type        = string
  default     = "westeurope"
}

variable "image" {
  description = "Fully qualified container image, e.g. myregistry.azurecr.io/sentinel:1.0.0."
  type        = string
}

variable "storage_replication" {
  description = "Storage account replication type."
  type        = string
  default     = "LRS"
}

variable "retention_days" {
  description = "Soft-delete and log retention window, in days."
  type        = number
  default     = 30

  validation {
    condition     = var.retention_days >= 7 && var.retention_days <= 365
    error_message = "retention_days must be between 7 and 365."
  }
}

variable "api_max_replicas" {
  description = "Upper bound on API replicas."
  type        = number
  default     = 3
}

variable "remediation_mode" {
  description = "auto lets low-risk actions self-apply; manual requires approval for everything."
  type        = string
  default     = "manual"

  validation {
    condition     = contains(["auto", "manual"], var.remediation_mode)
    error_message = "remediation_mode must be auto or manual."
  }
}

variable "pipeline_cron" {
  description = "Cron expression for the nightly pipeline job (UTC)."
  type        = string
  default     = "0 3 * * *"
}

variable "anthropic_secret_name" {
  description = "Name of the Key Vault secret holding the Anthropic API key. Empty disables it."
  type        = string
  default     = ""
}

variable "tags" {
  description = "Additional tags applied to every resource."
  type        = map(string)
  default     = {}
}
