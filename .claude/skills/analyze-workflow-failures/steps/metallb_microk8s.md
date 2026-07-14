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

### Pattern 2: Metallb charm install hook failure (pod started but hook errored)

**Symptom:**
```
DEBUG:root:metallb/0 workload status is running since ...
DEBUG:root:metallb/0 workload status is maintenance since ... (installing charm software)
DEBUG:root:metallb/0 juju agent status is executing since ... (running install hook)
DEBUG:root:metallb/0 workload status is error since ...
ERROR:root:metallb/0 failed: workload status is error

subprocess.CalledProcessError: Command '['juju-wait', '-m', 'foundations-maas:juju-system-microk8s', ...]'
returned non-zero exit status 1.
```

**Root cause:** The metallb charm install hook failed after the Kubernetes pod was
successfully created and the container reached "running". The bundle deployment
(`juju deploy`) completed cleanly, the charm was located in CharmHub, and the pod
transitioned through allocating → waiting → running → maintenance before hitting
error in ~4 seconds.
- Could be a race condition during charm software installation
- Could be an IP range configuration issue (e.g., range invalid or overlapping with
  existing allocations on the cluster)
- Could be a transient Kubernetes API error during the hook
- The MicroK8s cluster itself appears healthy (all units `active/idle`) at
  failure time

**Note:** On `shared_maas` substrates, charm logs and kubectl events are
unavailable because the `Collect logs` step may also fail with an FCE code bug
(`'ForkLayer' object has no attribute 'kubeconf_path'`). This makes root-cause
determination from GitHub logs alone difficult — the error is visible only as
the unit transitioning to error, with no charm-level debug output.

**Evidence to look for:**
- `log.txt`: `ERROR:root:metallb/0 failed: workload status is error`
- `log.txt`: The sequence going `running` → `maintenance` → `error` within a few
  seconds, confirming the install hook is where it failed
- `log.txt`: `Deploy of bundle completed.` preceding the error — confirms juju
  deploy itself succeeded
- `juju_status_foundations-maas_juju-system-microk8s.txt`: Unit showing
  `maintenance / installing charm software` or `error`
- `juju_status_foundations-maas_microk8s.txt`: MicroK8s units all `active/idle`
- Look for a secondary failure in `Collect logs` with
  `'ForkLayer' object has no attribute 'kubeconf_path'` — this explains missing
  charm logs

**Substrate observed:** `tor3-sqa-shared_maas`, dh1_j8_1 (2026-06-29, run 28365885178)

**Recommended action:** Retry the run. If the failure recurs on the same cluster,
check whether the configured IP range `10.241.36.130-10.241.36.134` is valid and
available on that cluster. Also consider fixing the FCE `collect_logs` bug for
`ForkLayer` to enable charm log collection for future diagnoses.

---

### Pattern 3: config-changed hook fails — MetalLB validating webhook unreachable (HTTP 500 on IPAddressPool)

**Symptom (GitHub log):** identical outer failure to Pattern 2 — unit goes to
`error`, `juju-wait` returns exit 1, FCE traceback ends in
`jujuworkloadlayer.py wait_until_ready`.

**Symptom (juju crashdump `debug_log.txt`):**
```
INFO unit.metallb/0.juju-log MetalLB native manifest has been installed
INFO ... HTTP Request: PATCH https://<apiserver>/apis/metallb.io/v1beta1/namespaces/metallb-system/ipaddresspools/<model>-metallb?force=true... "HTTP/1.1 500 Internal Server Error"   (repeated for several minutes)
ERROR unit.metallb/0.juju-log Uncaught exception while in charm code:
  httpx.HTTPStatusError: Server error '500 Internal Server Error' for url '.../ipaddresspools/<model>-metallb...'
  lightkube.core.exceptions.ApiError: Internal error occurred: failed calling webhook
  "ipaddresspoolvalidationwebhook.metallb.io": failed to call webhook:
  Post "https://webhook-service.metallb-system.svc:443/validate-metallb-io-v1beta1-ipaddresspool?timeout=10s":
  dial tcp <clusterip>:443: connect: connection refused
ERROR juju.worker.uniter.operation hook "config-changed" ... failed: exit status 1
```

**Root cause:** After installing the MetalLB native manifests, the charm's
`config-changed` hook applies the `IPAddressPool` custom resource. This triggers
MetalLB's own **validating admission webhook**, which the API server serves by
calling `webhook-service.metallb-system.svc:443`. The webhook backend (the MetalLB
controller pod) was not yet ready, so every API-server call to it was refused and
surfaced to the charm as HTTP 500. The charm retries for several minutes; if the
webhook never becomes reachable in that window, the hook raises an uncaught
`ApiError` and the unit goes to `error`. This is a **startup-ordering race**, not
an IP-range or config problem — the MicroK8s cluster itself is fully healthy
(all `microk8s/*` units `active/idle`).

**Evidence to look for:**
- Crashdump `debug_log.txt`: repeated `PATCH .../ipaddresspools/... 500 Internal
  Server Error`, then `failed calling webhook "ipaddresspoolvalidationwebhook.metallb.io"
  ... connection refused`
- Crashdump `pods.txt`: charm pod `metallb-0` shows `0/1 Running` (hook in error)
- `juju_status_..._microk8s.txt`: all MicroK8s units `active/idle`, "node is ready"
- Timeline: a multi-minute PATCH retry loop preceding the error confirms the race

**Substrate observed:** `tor3-sqa-shared_maas`, dh1_j8_1 (2026-07-09, run 29007511122)

**Recommended action:** Retry the run — the webhook race typically clears on the
next attempt. Longer-term, the metallb charm should wait for `webhook-service`
endpoints to be ready (or treat this specific 500 as a retryable `WaitingStatus`)
before applying the `IPAddressPool`. On shared_maas, slow image pulls can delay the
controller/webhook pod past the charm's retry budget.

---

_Add more patterns below as they are discovered._

## Notes

- The `create_model` step is the very first thing `metallb_microk8s` does — if it
  fails, no charm deployment has been attempted at all.
- On `tor3-sqa-virtual_maas`, both cluster_4 and cluster_6 showed MicroK8s API
  instability on 2026-03-24 (runs 23491280339 and 23491285216), suggesting the
  issue may be substrate-wide rather than cluster-specific.
- On `tor3-sqa-shared_maas`, charm logs are often unavailable due to a secondary
  FCE bug in `collect_logs`: `'ForkLayer' object has no attribute 'kubeconf_path'`
  (observed in run 28365885178). When this secondary failure is present, accept
  that the exact cause of the metallb hook failure cannot be determined from
  GitHub logs alone.

