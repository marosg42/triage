# Step Knowledge: k8s_cloud

## Step Overview

This step deploys a Kubernetes cluster (e.g., AWS EKS, Azure AKS) using Terraform to support subsequent pipeline layers like Kubeflow, MLflow, or other database/charm deployments.

## Swift Artifacts

Objects stored under `<uuid>/generated/` that are useful for diagnosing failures:

| Path | Description | When to check |
|---|---|---|
| `generated/github-runner/run.log` | Full workflow runner logs | Always — check for Terraform outputs and shell errors |
| `generated/version_collector_k8s_cloud.log` | Versions collected from the deployed K8s cloud | Check if cluster deployed but versions failed |

## Key Log Files (inside tgz archive)

For public cloud substrates (like Azure AKS or AWS EKS), no MAAS logs or node logs are collected. Standard GHA action logs and Terraform logs inside `run.log` are the primary investigation source.

## Grep Patterns

Useful search strings for this step's failure modes:

```bash
# Find Terraform Errors
grep -rn "Error:" <work_dir>/github-runner/run.log

# Check Terraform Apply Status
grep -rn "Apply complete!" <work_dir>/github-runner/run.log
```

## Known Failure Patterns

### Pattern 1: Missing Root Level Outputs (Azure Storage Secret Key)

**Symptom:**
```
Error: Output "azure_storage_secret_key" not found

The output variable requested could not be found in the state file. If you
recently added this to your configuration, be sure to run `terraform
apply`, since the state won't be updated with new output variables until
that command is run.
Process completed with exit code 1.
```

**Root cause:**
The GHA action `.github/actions/builds/k8s-clouds/action.yaml` unconditionally tries to fetch the output `azure_storage_secret_key` from the root level Terraform module. However, that output is either omitted completely or only defined in the nested `aks` module (`module.aks_cluster[0].azure_storage_secret_key`) but never propagated to the root level `outputs.tf` in `terraform/k8s-clouds/outputs.tf`.

**Evidence to look for:**
- `generated/github-runner/run.log`: Search for `Error: Output "azure_storage_secret_key" not found` right after `Apply complete!`.

---

### Pattern 2: Azure AKS GPU VMSS Overconstrained Allocation

**Symptom:**
```
Error: creating Kubernetes Cluster (...): polling after CreateOrUpdate:
Status: "OverconstrainedAllocationRequest"
Code: "OverconstrainedAllocationRequest"
Message: "Create or update VMSS ... failed. Allocation failed. VM(s) with the
following constraints cannot be allocated, because the condition is too restrictive.
Constraints applied are:
  - Networking Constraints (such as Accelerated Networking or IPv6)
  - VM Size"

with module.aks_cluster[0].azurerm_kubernetes_cluster.k8s,
on modules/aks/main.tf line 19, in resource "azurerm_kubernetes_cluster" "k8s"
Process completed with exit code 1.
```

**Root cause:**
Azure could not allocate the requested AKS node-pool VM size in the selected region.
This is most likely with GPU SKUs such as `vm_size = "small_tesla"` mapping to
`Standard_NC6s_v3` in `eastus`. The pipeline successfully reaches Terraform apply,
but the Azure capacity/allocation API rejects the VMSS for the AKS node pool.

**Evidence to look for:**
- `generated/github-runner/run.log`: Terraform plan shows `vm_size = "small_tesla"`
  and node profile `vm_size = "Standard_NC6s_v3"`, then `azurerm_kubernetes_cluster.k8s`
  stays in "Still creating..." before failing with `OverconstrainedAllocationRequest`.
- The same log should show the resource group was created, then subsequent
  "Get AKS kubeconfig" / storage output steps were skipped.
- `config/cloud.tfvars`: confirms `cloud_provider = "azure"`, `vm_type = "gpu"`,
  and the selected `vm_size`.

**Remediation:**
Retry when regional capacity changes, or select a different Azure region/SKU
with available GPU capacity.

---

_Add more patterns below as they are discovered._

## Notes

- This step runs dynamically on public cloud substrates (`ext-*`).
