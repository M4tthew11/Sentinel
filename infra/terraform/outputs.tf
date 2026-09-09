output "resource_group" {
  description = "Resource group holding every Sentinel resource."
  value       = azurerm_resource_group.this.name
}

output "api_url" {
  description = "Public URL of the incident console."
  value       = "https://${azurerm_container_app.api.ingress[0].fqdn}"
}

output "storage_account" {
  description = "Storage account backing the landing zone and the warehouse."
  value       = azurerm_storage_account.data.name
}

output "identity_client_id" {
  description = "Client id of the managed identity the application runs as."
  value       = azurerm_user_assigned_identity.app.client_id
}

output "key_vault_uri" {
  description = "Key Vault holding the Anthropic API key."
  value       = azurerm_key_vault.this.vault_uri
}

output "pipeline_job" {
  description = "Scheduled job that runs the pipeline for one business date."
  value       = azurerm_container_app_job.pipeline.name
}
