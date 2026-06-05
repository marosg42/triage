# Step Knowledge: kubeflow_terraform

## Step Overview

Deploys Charmed Kubeflow components via Terraform to a Kubernetes cluster (e.g., AKS or MicroK8s). This step handles deploying and integrating multiple Kubeflow applications like `kfp-api`, `kubeflow-dashboard`, `mlflow-server`, and `mlflow-mysql`.

## Swift Artifacts

Objects stored under `<uuid>/generated/` that are useful for diagnosing failures:

| Path | Description | When to check |
|---|---|---|
| `generated/project_comp.log` | FCE component configuration log | To identify executed layers |
| `generated/version_collector_kubeflow_terraform.log` | Debug output collected after step fails | Always — captures `juju status` and Juju configuration commands output |

> Note: For substrates like `ext-sqa-aks`, MAAS logs and `juju-crashdump` might be missing. The `github-runner/run.log` usually contains the raw Juju status tables output right before the pipeline exit.

## Key Log Files (inside bundle)

| File | What it contains | When to use |
|---|---|---|
| `generated/github-runner/run.log` | Raw GitHub Actions runner execution output | Primary investigation for Juju wait/timeout/status errors |
| `generated/lastlines.txt` | Tail ends of all captured log streams | Quick triage of unit states at failure time |

## Grep Patterns

Useful search strings for this step's failure modes:

```bash
# Search for Juju hook failures in the GitHub Actions runner log
grep -i "hook failed" <work_dir>/<uuid>/generated/github-runner/run.log

# Extract the state of mlflow components from the last known state
grep -i "mlflow" <work_dir>/<uuid>/generated/lastlines.txt

# Extract error statuses from Juju components
grep -i "error" <work_dir>/<uuid>/generated/lastlines.txt
```

## Known Failure Patterns

### Pattern 1: mlflow-mysql hook failed: "database-relation-changed"

**Symptom:**
```
mlflow-mysql/2*             error     idle       10.244.2.24         hook failed: "database-relation-changed"
##[error]Process completed with exit code 1.
```

**Root cause:** The leader unit of `mlflow-mysql` (using the `mysql-k8s` charm) encounters an exception during the `database-relation-changed` hook. The `mlflow-server/0*` unit remains in `waiting` state with `Waiting for relational-db relation data`. Without `juju-crashdump` logs (often missing on AKS substrates), the precise stack trace is unavailable, but it is a deterministic hook failure in the MySQL charm processing the relation data.

**Evidence to look for:**
- `generated/github-runner/run.log` or `lastlines.txt`: The `mlflow-mysql` leader unit in `error` with the `database-relation-changed` hook failure.
- `generated/github-runner/run.log` or `lastlines.txt`: The `mlflow-server` unit in `waiting` status.
- Missing `juju-crashdump` files from the bundle preventing further tracing.

---

_Add more patterns below as they are discovered._

## Notes

- Deployments on public cloud substrates (like `ext-sqa-aks`) typically lack MAAS logs.
- If crashdumps are missing, diagnosis heavily relies on `juju status` output captured in `run.log` or `lastlines.txt`.

