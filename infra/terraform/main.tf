#
# Azure deployment for Sentinel.
#
# Scope: the container that runs the platform, the storage it reads and writes,
# and the secret it needs. Deliberately small - a portfolio repository should
# show the IaC that matches the application it ships, not a landing zone.
#
# The image is expected in an existing registry (see variables.tf). Nothing here
# is required to run the project locally; `docker compose up` is the offline
# path, and this is what the same thing looks like on Azure.
#

terraform {
  required_version = ">= 1.6.0"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # Remote state, created out of band so this configuration can be applied from
  # CI without a bootstrap chicken-and-egg problem.
  # backend "azurerm" {
  #   resource_group_name  = "rg-tfstate"
  #   storage_account_name = "sttfstate"
  #   container_name       = "tfstate"
  #   key                  = "sentinel.tfstate"
  # }
}

provider "azurerm" {
  features {}
}

data "azurerm_client_config" "current" {}

resource "random_string" "suffix" {
  length  = 6
  special = false
  upper   = false
}

locals {
  name = "${var.project}-${var.environment}"
  tags = merge(
    {
      project     = var.project
      environment = var.environment
      managed_by  = "terraform"
    },
    var.tags,
  )
}

resource "azurerm_resource_group" "this" {
  name     = "rg-${local.name}"
  location = var.location
  tags     = local.tags
}

# ---------------------------------------------------------------------------
# Storage: the landing zone and the warehouse file
# ---------------------------------------------------------------------------

resource "azurerm_storage_account" "data" {
  name                            = replace("st${local.name}${random_string.suffix.result}", "-", "")
  resource_group_name             = azurerm_resource_group.this.name
  location                        = azurerm_resource_group.this.location
  account_tier                    = "Standard"
  account_replication_type        = var.storage_replication
  account_kind                    = "StorageV2"
  is_hns_enabled                  = true # Data Lake Gen2 semantics for the medallion layout
  min_tls_version                 = "TLS1_2"
  https_traffic_only_enabled      = true
  allow_nested_items_to_be_public = false
  shared_access_key_enabled       = false # managed identity only

  blob_properties {
    versioning_enabled  = true
    change_feed_enabled = true

    delete_retention_policy {
      days = var.retention_days
    }
  }

  tags = local.tags
}

resource "azurerm_storage_container" "landing" {
  name                  = "landing"
  storage_account_id    = azurerm_storage_account.data.id
  container_access_type = "private"
}

resource "azurerm_storage_container" "warehouse" {
  name                  = "warehouse"
  storage_account_id    = azurerm_storage_account.data.id
  container_access_type = "private"
}

# ---------------------------------------------------------------------------
# Identity and secrets
# ---------------------------------------------------------------------------

resource "azurerm_user_assigned_identity" "app" {
  name                = "id-${local.name}"
  resource_group_name = azurerm_resource_group.this.name
  location            = azurerm_resource_group.this.location
  tags                = local.tags
}

resource "azurerm_role_assignment" "storage" {
  scope                = azurerm_storage_account.data.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
}

resource "azurerm_key_vault" "this" {
  name                       = "kv-${substr(local.name, 0, 14)}-${random_string.suffix.result}"
  resource_group_name        = azurerm_resource_group.this.name
  location                   = azurerm_resource_group.this.location
  tenant_id                  = data.azurerm_client_config.current.tenant_id
  sku_name                   = "standard"
  purge_protection_enabled   = var.environment == "prod"
  soft_delete_retention_days = var.retention_days
  rbac_authorization_enabled = true
  tags                       = local.tags
}

resource "azurerm_role_assignment" "vault_read" {
  scope                = azurerm_key_vault.this.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
}

# The Anthropic key is written to the vault out of band (CI, or by hand); this
# only declares that the application is allowed to read it.
data "azurerm_key_vault_secret" "anthropic" {
  count        = var.anthropic_secret_name == "" ? 0 : 1
  name         = var.anthropic_secret_name
  key_vault_id = azurerm_key_vault.this.id
}

# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------

resource "azurerm_log_analytics_workspace" "this" {
  name                = "log-${local.name}"
  resource_group_name = azurerm_resource_group.this.name
  location            = azurerm_resource_group.this.location
  sku                 = "PerGB2018"
  retention_in_days   = var.retention_days
  tags                = local.tags
}

# ---------------------------------------------------------------------------
# Compute: the API, and the nightly pipeline as a scheduled job
# ---------------------------------------------------------------------------

resource "azurerm_container_app_environment" "this" {
  name                       = "cae-${local.name}"
  resource_group_name        = azurerm_resource_group.this.name
  location                   = azurerm_resource_group.this.location
  log_analytics_workspace_id = azurerm_log_analytics_workspace.this.id
  tags                       = local.tags
}

resource "azurerm_container_app" "api" {
  name                         = "ca-${local.name}-api"
  resource_group_name          = azurerm_resource_group.this.name
  container_app_environment_id = azurerm_container_app_environment.this.id
  revision_mode                = "Single"
  tags                         = local.tags

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.app.id]
  }

  ingress {
    external_enabled = true
    target_port      = 8000

    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }

  template {
    min_replicas = 1
    max_replicas = var.api_max_replicas

    container {
      name   = "sentinel"
      image  = var.image
      cpu    = 0.5
      memory = "1Gi"

      args = ["serve", "--host", "0.0.0.0", "--port", "8000"]

      env {
        name  = "SENTINEL_DATA_DIR"
        value = "/data"
      }
      env {
        name  = "SENTINEL_LOG_FORMAT"
        value = "json"
      }
      env {
        name  = "SENTINEL_REMEDIATION__MODE"
        value = var.remediation_mode
      }
      env {
        name  = "AZURE_CLIENT_ID"
        value = azurerm_user_assigned_identity.app.client_id
      }

      liveness_probe {
        transport = "HTTP"
        port      = 8000
        path      = "/health"
      }
    }
  }
}

# The pipeline is a batch workload, not a service: one run per business date.
resource "azurerm_container_app_job" "pipeline" {
  name                         = "caj-${local.name}-pipeline"
  resource_group_name          = azurerm_resource_group.this.name
  location                     = azurerm_resource_group.this.location
  container_app_environment_id = azurerm_container_app_environment.this.id
  replica_timeout_in_seconds   = 1800
  replica_retry_limit          = 1
  tags                         = local.tags

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.app.id]
  }

  schedule_trigger_config {
    cron_expression          = var.pipeline_cron
    parallelism              = 1
    replica_completion_count = 1
  }

  template {
    container {
      name   = "sentinel"
      image  = var.image
      cpu    = 1.0
      memory = "2Gi"

      # The date is resolved by the entrypoint at run time in a real
      # deployment; kept explicit here so the contract is visible.
      args = ["monitor", "$(BUSINESS_DATE)"]

      env {
        name  = "SENTINEL_DATA_DIR"
        value = "/data"
      }
      env {
        name  = "SENTINEL_LOG_FORMAT"
        value = "json"
      }
      env {
        name  = "AZURE_CLIENT_ID"
        value = azurerm_user_assigned_identity.app.client_id
      }
    }
  }
}
