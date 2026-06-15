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

_Add more patterns below as they are discovered._

## Notes

- This step runs dynamically on public cloud substrates (`ext-*`).
