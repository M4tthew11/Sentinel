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
      version = "~> 4.50" # rbac_authorization_enabled on the Key Vault needs a recent 4.x
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

# The DuckDB file the pipeline job writes and the console reads has to live on
# a volume both containers mount; without one, each container gets its own
# ephemeral /data and the console never sees a pipeline run.
#
# Container Apps can only mount Azure Files with an account key, so the share
# lives in its own small account. The data lake above keeps shared keys disabled;
# this key is held by the Container Apps environment and never reaches the
# application, its environment variables or its logs.
resource "azurerm_storage_account" "state" {
  name                            = replace("st${local.name}fs${random_string.suffix.result}", "-", "")
  resource_group_name             = azurerm_resource_group.this.name
  location                        = azurerm_resource_group.this.location
  account_tier                    = "Standard"
  account_replication_type        = var.storage_replication
  account_kind                    = "StorageV2"
  min_tls_version                 = "TLS1_2"
  https_traffic_only_enabled      = true
  allow_nested_items_to_be_public = false
  shared_access_key_enabled       = true # required by the Container Apps SMB mount

  share_properties {
    retention_policy {
      days = var.retention_days
    }
  }

  tags = local.tags
}

resource "azurerm_storage_share" "warehouse" {
  name               = "warehouse"
  storage_account_id = azurerm_storage_account.state.id
  quota              = var.warehouse_share_quota_gb
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

resource "azurerm_container_app_environment_storage" "warehouse" {
  name                         = "warehouse"
  container_app_environment_id = azurerm_container_app_environment.this.id
  account_name                 = azurerm_storage_account.state.name
  share_name                   = azurerm_storage_share.warehouse.name
  access_key                   = azurerm_storage_account.state.primary_access_key
  access_mode                  = "ReadWrite"
}

locals {
  # Key Vault references resolved by the platform with the managed identity, so
  # neither secret appears in Terraform state or in the container definition.
  app_secrets = {
    for name, secret_name in {
      "anthropic-api-key" = var.anthropic_secret_name
      "approver-tokens"   = var.approver_tokens_secret_name
    } : name => "${azurerm_key_vault.this.vault_uri}secrets/${secret_name}"
    if secret_name != ""
  }
  secret_env = {
    "anthropic-api-key" = "ANTHROPIC_API_KEY"
    "approver-tokens"   = "SENTINEL_APPROVER_TOKENS"
  }
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

  dynamic "secret" {
    for_each = local.app_secrets
    content {
      name                = secret.key
      key_vault_secret_id = secret.value
      identity            = azurerm_user_assigned_identity.app.id
    }
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
    # One replica: DuckDB allows a single writing process per file, and the
    # approve/reject endpoints write. Scale out once the operational tables
    # move to a server database.
    min_replicas = 1
    max_replicas = 1

    volume {
      name         = "warehouse"
      storage_type = "AzureFile"
      storage_name = azurerm_container_app_environment_storage.warehouse.name
    }

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

      dynamic "env" {
        for_each = local.app_secrets
        content {
          name        = local.secret_env[env.key]
          secret_name = env.key
        }
      }

      volume_mounts {
        name = "warehouse"
        path = "/data"
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

  dynamic "secret" {
    for_each = local.app_secrets
    content {
      name                = secret.key
      key_vault_secret_id = secret.value
      identity            = azurerm_user_assigned_identity.app.id
    }
  }

  schedule_trigger_config {
    cron_expression          = var.pipeline_cron
    parallelism              = 1
    replica_completion_count = 1
  }

  template {
    volume {
      name         = "warehouse"
      storage_type = "AzureFile"
      storage_name = azurerm_container_app_environment_storage.warehouse.name
    }

    container {
      name   = "sentinel"
      image  = var.image
      cpu    = 1.0
      memory = "2Gi"

      # No date here on purpose: Container Apps cannot compute one in an
      # argument list, and an unresolved $(VAR) reaches the process verbatim.
      # `sentinel monitor` without a date processes yesterday in the business
      # timezone. To backfill, start the job with an explicit date:
      #   az containerapp job start ... --args monitor 2026-11-10
      args = ["monitor"]

      env {
        name  = "SENTINEL_DATA_DIR"
        value = "/data"
      }
      env {
        name  = "SENTINEL_BUSINESS_TIMEZONE"
        value = var.business_timezone
      }
      env {
        name  = "SENTINEL_LOG_FORMAT"
        value = "json"
      }
      env {
        name  = "AZURE_CLIENT_ID"
        value = azurerm_user_assigned_identity.app.client_id
      }

      dynamic "env" {
        for_each = local.app_secrets
        content {
          name        = local.secret_env[env.key]
          secret_name = env.key
        }
      }

      volume_mounts {
        name = "warehouse"
        path = "/data"
      }
    }
  }
}
