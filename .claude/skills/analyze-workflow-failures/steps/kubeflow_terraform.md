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

**Variant (observed on `ext-sqa-aks`, mysql-k8s rev 423 / 8.0/stable):** the same
`database-relation-changed` hook failure can hit **multiple** `mysql-k8s` deployments in the
same run — e.g. both `katib-db/N*` and `mlflow-mysql/N*` leader units in `error` simultaneously.
Only the **leader** replica fails; the other `mysql-k8s` units in the app stay `active`. The
surrounding step failure is a `juju wait-for model kubeflow ... --timeout 60m` that times out:
```
ERROR timed out waiting for "kubeflow" to reach goal state
##[error]Process completed with exit code 1.
```
Downstream symptoms cascade from the failed DBs:
- `katib-db-manager`, `kfp-api`, `mlflow-server`: `waiting` — "Waiting for relational-db data"
- `kfp-persistence`, `kfp-ui`: `blocked` — "[relation:kfp-api] Expected data from exactly 1 related applications - got 0."

To find the failed DBs quickly:
```bash
grep -iE "hook failed.*database-relation-changed" <uuid>/generated/lastlines.txt
```

---

### Pattern 2: mysql-k8s `database-relation-changed` fails — kube-proxy HAIRPIN on self-targeted primary ClusterIP (ext-sqa-aks)

**This is the DEFINITIVE root cause of the recurring `kubeflow_terraform` failures on
`ext-sqa-aks` (confirmed by live capture on run 30108398444). It is a mysql-k8s charm bug
exposed by an AKS/CNI hairpin limitation — NOT a Juju version / 3.6-candidate defect.**

**Symptom:** `juju wait-for model kubeflow --timeout 60m` times out; one or more `mysql-k8s`
leader units (katib-db, kfp-db, mlflow-mysql — any combination) stuck in
`hook failed: "database-relation-changed"`; consumers (kfp-api, mlflow-server,
katib-db-manager) `waiting`, kfp-persistence/kfp-ui `blocked`.

**Charm traceback (only visible via live `juju debug-log`, absent from Swift bundles):**
```
unit.<app>/N.juju-log database:NN: Timed out waiting for k8s service to be ready
  File ".../charm/src/relations/mysql_provider.py", line 141, in _on_database_requested
    self.charm.k8s_helpers.wait_service_ready((primary_endpoint, 3306))
  File ".../charm/src/k8s_helpers.py", line 205, in wait_service_ready
    raise TimeoutError
TimeoutError
```
`wait_service_ready` just does `socket.connect_ex((primary_endpoint, 3306))`, retrying
60×1s (60s total).

**Mechanism:** the `database-relation-changed` hook runs on the **leader** unit and connects
to the `<app>-primary` ClusterIP Service, whose sole endpoint is the current MySQL **Primary**
pod. When the leader unit and the Primary are the **same pod** (common — unit `/0` is often
both), the pod connects to a ClusterIP that DNATs back to itself → **hairpin**, which this AKS
kube-proxy/CNI does not support. SYN is black-holed (connect errno 11 / timeout, no RST), so
the 60s wait always expires → hook fails. Juju auto-retries every ~2 min but keeps re-hitting
the hairpin, so the model never settles and the 60m wait-for expires.

**How to confirm (live env only — needs SSH to runner + juju access):**
```bash
# 1) failing unit == leader (*) AND "Primary"?  ->  juju status -m kubeflow <app>
# 2) mysqld healthy: from the primary pod, its own pod IP:3306 is OPEN
# 3) HAIRPIN differential test against the <app>-primary ClusterIP:3306
#    from the primary pod itself  -> closed(11)/timeout
#    from ANY other pod           -> OPEN
# 4) kube-proxy is fine globally: kubernetes.default:443 OPEN and the Juju-created
#    <app> service ClusterIP:3306 OPEN
```
If the ClusterIP is reachable from every pod except its own endpoint pod, it is hairpin.

**Why it is NOT the Juju candidate:** Juju does not manage the charm-created
`<app>-primary`/`<app>-replicas` services and does not program kube-proxy; Juju's own `<app>`
Service routes fine on the same cluster/agent. The determining factor is leader==primary
(charm topology), not the Juju build.

**Fixes:**
1. Charm (mysql-k8s): don't TCP-connect to the primary ClusterIP from the primary pod; check
   endpoint readiness via the k8s API, or connect to the pod FQDN. Report upstream (rev 423).
2. Env (AKS/CNI): enable hairpin/loopback for ClusterIP services (kube-proxy hairpinMode /
   masquerade).

See `outputs/30108398444-analysis.md` for the full live evidence.

---

_Add more patterns below as they are discovered._

## Notes

- Deployments on public cloud substrates (like `ext-sqa-aks`) typically lack MAAS logs.
- If crashdumps are missing, diagnosis heavily relies on `juju status` output captured in `run.log` or `lastlines.txt`.

