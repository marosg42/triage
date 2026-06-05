# Step Knowledge: existing_juju_maas_controller_microk8s

## Step Overview

Registers an existing MicroK8s cluster as a Kubernetes cloud in an existing Juju
MAAS controller. The key action is `juju add-k8s`, which creates a cluster role
binding inside MicroK8s to establish Juju credentials, then registers the cluster
as a named K8s cloud (`microk8s_cloud`) in the `foundations-maas` controller.

## Swift Artifacts

Objects stored under `<uuid>/generated/existing_juju_maas_controller_microk8s/`
that are useful for diagnosing failures:

| Path | Description | When to check |
|---|---|---|
| `generated/existing_juju_maas_controller_microk8s/log.txt` | FCE build log — includes the full error and stack trace | Always — first stop |
| `generated/existing_juju_maas_controller_microk8s/juju_status_foundations-maas_microk8s.txt` | Juju status of the MicroK8s model at failure time | Check unit health and messages |
| `generated/existing_juju_maas_controller_microk8s/juju_status_foundations-maas_controller.txt` | Juju status of the controller model | Check controller health |
| `generated/existing_juju_maas_controller_microk8s/juju_status_foundations-maas_microk8s.json` | JSON version of microk8s model status | Programmatic inspection |

The MAAS logs tgz (`generated/maas/logs-<timestamp>.tgz`) is available for
`virtual_maas` and `dedicated_maas` substrates but is rarely needed for failures
at this step (which is a Juju/K8s-level issue, not a MAAS-level issue).

## Key Log Files (inside tgz archive)

Rarely needed for this step. If investigating node-level issues:

| File | What it contains | When to use |
|---|---|---|
| `var/log/syslog` | System and service events | Snap refresh or host-level issue |

## Grep Patterns

```bash
# Find the primary juju add-k8s error
grep "add-k8s\|database is locked\|cluster role binding" \
  <work_dir>/<uuid>/generated/existing_juju_maas_controller_microk8s/log.txt

# Check MicroK8s unit health in juju status
grep -E "error|blocked|waiting|maintenance" \
  <work_dir>/<uuid>/generated/existing_juju_maas_controller_microk8s/juju_status_foundations-maas_microk8s.txt
```

## Known Failure Patterns

### Pattern 1: MicroK8s dqlite "database is locked" during juju add-k8s

**Symptom:**
```
ERROR making juju admin credentials in cluster: ensuring cluster role binding
"juju-credential-<id>": rpc error: code = Unknown desc = exec (try: 500):
database is locked

subprocess.CalledProcessError: Command '['juju', 'add-k8s', 'microk8s_cloud',
'--controller', 'foundations-maas', '--storage', 'ceph-rbd',
'--context-name', 'microk8s']' returned non-zero exit status 1.
```

**Root cause:** `juju add-k8s` connects to the MicroK8s cluster's Kubernetes API
and tries to create a `ClusterRoleBinding` to establish credentials. MicroK8s
uses **dqlite** (a distributed SQLite-based store) as its backing database for
Kubernetes objects. When dqlite is temporarily contended — due to a leader
election, an in-flight write, or a transient lock — the K8s API server rejects
the write with `database is locked`. This is an intermittent, transient error
rather than a hard infrastructure failure: the MicroK8s cluster itself typically
appears fully healthy (all units `active/idle`) at the time the error is captured.

**Evidence to look for:**
- `log.txt`: `rpc error: code = Unknown desc = exec (try: 500): database is locked`
- `juju_status_foundations-maas_microk8s.txt`: All MicroK8s units showing
  `active / idle` — the cluster is healthy, confirming the lock was transient
- `log.txt`: The error occurs at step `add_cloud` (very early in the layer),
  roughly 24 seconds after FCE started (14:41:38 → 14:42:02)

**Substrate observed:** `tor3-sqa-virtual_maas`, cluster_4 (2026-03-24)

**Recommended action:** Retry the run. This is a transient dqlite contention
error and typically resolves on the next attempt.

---

_Add more patterns below as they are discovered._

## Notes

- A non-fatal `ERROR model foundations-maas:admin/kubernetes-maas not found`
  often appears just before the main error — this is expected when the
  `kubernetes-maas` model doesn't yet exist and is handled gracefully by FCE.
- The MicroK8s cluster uses ceph-rbd for storage; the `--storage ceph-rbd`
  argument to `juju add-k8s` requires a healthy Ceph cluster on the MicroK8s
  nodes. If Ceph is degraded, a different error will surface.
- MicroK8s 1.28/stable has been observed on this cluster.

