# Infrastructure

Terraform for running Sentinel on Azure Container Apps: the console as a service,
the pipeline as a scheduled job, ADLS Gen2 for the medallion layers, Key Vault for
the API key, and a user-assigned managed identity so nothing carries a connection
string.

**Nothing here is needed to run the project.** `docker compose up` and
`sentinel demo` work entirely offline; this is what the same deployment looks
like when it leaves a laptop.

## Layout

| File | Contents |
|---|---|
| `main.tf` | Resource group, storage, identity, Key Vault, Log Analytics, Container Apps |
| `variables.tf` | Inputs, with validation on the ones that are easy to get wrong |
| `outputs.tf` | Console URL, storage account, identity client id |
| `terraform.tfvars.example` | A starting point; copy to `terraform.tfvars` |

## Usage

```bash
cp terraform.tfvars.example terraform.tfvars   # then edit `image`
terraform init
terraform plan
terraform apply
```

## Decisions worth noting

- **Shared access keys are disabled** on the storage account. The container
  authenticates with its managed identity, so there is no credential to rotate or
  leak.
- **The pipeline is a job, not a service.** It runs once per business date and
  exits; a long-lived container would keep a warehouse connection open for
  nothing.
- **`remediation_mode` defaults to `manual`.** Autonomy is opt-in per
  environment: nothing self-applies in a new deployment until someone has watched
  it propose the right thing a few times.
- **Purge protection is on in prod only**, so a `dev` environment can be
  destroyed and recreated without waiting out the soft-delete window.

## Not covered

Networking (private endpoints, VNet integration), the container registry itself,
and the state backend - all of which belong to whatever landing zone the platform
is deployed into rather than to this repository. The `backend "azurerm"` block in
`main.tf` is commented out for the same reason.
