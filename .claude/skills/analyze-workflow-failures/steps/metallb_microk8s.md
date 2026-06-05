# Step Knowledge: metallb_microk8s

## Step Overview

Deploys MetalLB on the existing MicroK8s cluster via Juju. The first action is
`juju add-model` which creates a new Juju model (`juju-system-microk8s`) backed
by the `microk8s_cloud` Kubernetes cloud. This requires the Kubernetes API server
on the MicroK8s cluster to be reachable and able to accept namespace creation
requests. Once the model exists, FCE deploys the `metallb` charm into it.

This step runs **after** `existing_juju_maas_controller_microk8s` (which registers
the MicroK8s cloud) and is a prerequisite for any workloads deployed onto the
MicroK8s cluster.

## Swift Artifacts

Objects stored under `<uuid>/generated/metallb_microk8s/` that are useful for
diagnosing failures:

| Path | Description | When to check |
|---|---|---|
| `generated/metallb_microk8s/log.txt` | FCE build log — includes the full error and stack trace | Always — first stop |
| `generated/metallb_microk8s/juju_status_foundations-maas_microk8s.txt` | Juju status of the MicroK8s model at failure time | Check unit health and messages |
| `generated/metallb_microk8s/juju_status_foundations-maas_controller.txt` | Juju status of the controller model | Check controller health |
| `generated/metallb_microk8s/bundle.yaml` | The Juju bundle FCE attempted to deploy | Verify charm/config being deployed |

## Key Log Files (inside tgz archive)

Rarely needed for this step. If investigating node-level API server issues, the
MAAS logs tgz (`generated/maas/logs-<timestamp>.tgz`) from the same run may
contain syslog entries from the MicroK8s nodes.

## Grep Patterns

```bash
# Find the primary error
grep -E "unexpected EOF|add-model|create_model|juju-system" \
  <work_dir>/<uuid>/generated/metallb_microk8s/log.txt

# Check MicroK8s unit health at failure time
grep -E "error|blocked|waiting|maintenance" \
  <work_dir>/<uuid>/generated/metallb_microk8s/juju_status_foundations-maas_microk8s.txt

# Check which MicroK8s node IP is the API endpoint being hit
grep "16443" <work_dir>/<uuid>/generated/metallb_microk8s/log.txt
```

## Known Failure Patterns

### Pattern 1: MicroK8s API server "unexpected EOF" during juju add-model

**Symptom:**
```
ERROR creating namespace "juju-system-microk8s":
Post "https://<microk8s-node-ip>:16443/api/v1/namespaces": unexpected EOF

subprocess.CalledProcessError: Command '['juju', 'add-model', '-c',
'foundations-maas', 'juju-system-microk8s', 'microk8s_cloud']'
returned non-zero exit status 1.
```

**Root cause:** `juju add-model` on a MicroK8s cloud must create a Kubernetes
namespace via a POST to the MicroK8s API server (port 16443). An `unexpected EOF`
means the HTTP/TLS connection was dropped by the server mid-request — the TCP
session closed before a complete HTTP response was received. This is a transient
connectivity failure to the MicroK8s API server, most likely caused by:
- A brief MicroK8s API server restart or crash loop
- MicroK8s dqlite leader re-election (which stalls writes temporarily)
- A network-level blip on the MicroK8s node

The MicroK8s cluster itself typically appears fully healthy (`active/idle`) at
the time the error is captured in `juju status`, since the service recovers
quickly.

Note: this is a related but distinct symptom to the `database is locked` error
seen in `existing_juju_maas_controller_microk8s` — both point to transient
MicroK8s API instability, possibly originating from dqlite.

**Evidence to look for:**
- `log.txt`: `unexpected EOF` at step `create_model`
- `log.txt`: The failing URL will be `https://<node-ip>:16443/api/v1/namespaces`
  — note which node IP to correlate with `juju status`
- `juju_status_foundations-maas_microk8s.txt`: All MicroK8s units showing
  `active / idle` — confirms the issue was transient

**Substrate observed:** `tor3-sqa-virtual_maas`, cluster_6 (2026-03-24)

**Recommended action:** Retry the run. This is a transient MicroK8s API server
connectivity error and typically resolves on the next attempt.

**See also:** `steps/existing_juju_maas_controller_microk8s.md` for the related
`database is locked` pattern, which affects an earlier step on the same cluster
type.

---

_Add more patterns below as they are discovered._

## Notes

- The `create_model` step is the very first thing `metallb_microk8s` does — if it
  fails, no charm deployment has been attempted at all.
- On `tor3-sqa-virtual_maas`, both cluster_4 and cluster_6 showed MicroK8s API
  instability on 2026-03-24 (runs 23491280339 and 23491285216), suggesting the
  issue may be substrate-wide rather than cluster-specific.
- A non-fatal `ERROR model foundations-maas:admin/kubernetes-maas not found`
  appears before the main error — this is expected behaviour (FCE probes for the
  model before attempting to create it).

## Version History

- **v1.0** (2026-03-25): Initial version — unexpected EOF pattern from run
  23491285216 (UUID 8647be66, tor3-sqa-virtual_maas cluster_6)
