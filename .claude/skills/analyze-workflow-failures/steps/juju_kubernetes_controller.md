# Step Knowledge: juju_kubernetes_controller

## Step Overview

This step bootstraps a Juju controller onto a Kubernetes cluster (CAAS). FCE runs
`fce build --layer juju_kubernetes_controller`, which:
1. Installs/refreshes the `juju` snap to the configured channel
2. Runs `juju add-k8s kubernetes_cloud --client --storage <storage_class>` to register
   the k8s cluster as a Juju cloud
3. Runs `juju bootstrap kubernetes_cloud foundations-kubernetes` with
   `--config caas-image-repo=ghcr.io/juju` and `bootstrap-timeout=1800`

This layer depends on a prior `kubernetes-openstack` (or similar) layer having deployed
a working k8s cluster and exported its kubeconfig.

Entry point: `fce build --layer juju_kubernetes_controller` (via `.github/actions/builds/run-fce-build`)

## Swift Artifacts

| Path | Description | When to check |
|---|---|---|
| `generated/juju_kubernetes_controller/log.txt` | FCE log — bootstrap steps and timing | Always first |
| `generated/juju_kubernetes_controller/model_defaults.yaml` | Juju model defaults | Config verification |
| `generated/kubernetes-openstack/juju-status-verbose.txt` | Juju status of the k8s cluster at end of prior layer | Verify cluster health |
| `generated/kubernetes-openstack/juju-crashdump-kubernetes-openstack-<ts>.tar.gz` | Crashdump of the k8s cluster collected after failure | Deep investigation — contains nginx API LB logs |
| `generated/kubernetes-openstack/kube.conf` | Kubeconfig used to access the cluster | Connect and kubectl if runner is still alive |
| `generated/version_collector_juju_kubernetes_controller.log` | Snap list, juju controllers at time of failure | Juju version context |

### Crashdump contents (most useful for diagnosing this step)

The crashdump is for the `kubernetes-openstack` Juju model — it contains logs from the
**kubeapi-load-balancer** machine (machine 4), including:

| File in crashdump | What it contains |
|---|---|
| `4/baremetal/var/log/nginx/apilb.access.log` | **All k8s API requests** — every kubelet and Juju client call goes through this LB. This is the primary source of truth for pod lifecycle. |
| `4/baremetal/var/log/nginx/apilb.error.log` | nginx upstream errors (upstream prematurely closed, etc.) |
| `4/baremetal/var/log/syslog` | System events on the LB machine (nginx restarts, etc.) |
| `4/baremetal/var/log/juju/machine-4.log` | Juju agent log for kubeapi-lb machine |

```bash
# Extract crashdump
mkdir -p /tmp/k8s-crashdump
tar -xzf generated/kubernetes-openstack/juju-crashdump-kubernetes-openstack-*.tar.gz -C /tmp/k8s-crashdump
# Then use: /tmp/k8s-crashdump/<uuid>/4/baremetal/var/log/nginx/apilb.access.log
```

## Grep Patterns

```bash
# Find the bootstrap error
grep -E "ERROR|failed|unreachable|contact api" generated/juju_kubernetes_controller/log.txt

# Find timing of key bootstrap phases
grep -E "bootstrap|Downloading|Starting controller|Contacting|show-controller" generated/juju_kubernetes_controller/log.txt

# In GH runner log: find the exact error and timing
grep -n "unable to contact api\|connection refused\|not found\|bootstrap.*exit" /tmp/run_<run_id>_failed.log | head -20

# Verify k8s cluster was healthy before this step
grep -E "active|idle|Ready|error" generated/kubernetes-openstack/juju-status-verbose.txt | grep -v "^#" | head -20

# === FROM CRASHDUMP (kubeapi-lb nginx access log) ===
# Check all activity in the controller-foundations-kubernetes namespace (timeline of pod lifecycle)
grep "controller-foundations-kubernetes" /tmp/k8s-crashdump/*/4/baremetal/var/log/nginx/apilb.access.log | \
  grep -v "kube-node-lease" | head -100

# Look for the kubelet activity gap (CrashLoopBackOff)
grep "controller-0.*status" /tmp/k8s-crashdump/*/4/baremetal/var/log/nginx/apilb.access.log | \
  awk '{print $4, $6, $7, $8, $9}' | tr -d '[]"'

# Check if PVC was bound (storage problem?)
grep "storage-controller-0\|volumeattachment" /tmp/k8s-crashdump/*/4/baremetal/var/log/nginx/apilb.access.log | head -20

# Check for event 403 errors (RBAC cleaned up while pod still running)
grep "controller-foundations-kubernetes.*403\|controller-foundations-kubernetes.*404" \
  /tmp/k8s-crashdump/*/4/baremetal/var/log/nginx/apilb.access.log | tail -20

# Check for nginx restarts (may indicate kubeapi-lb was reconfigured)
grep "nginx.*stop\|nginx.*start\|Failed with result" /tmp/k8s-crashdump/*/4/baremetal/var/log/syslog
```

## Known Failure Patterns

### Pattern 1: Controller pod starts but API never becomes reachable (60-attempt exhaustion)

**Symptom (in `generated/juju_kubernetes_controller/log.txt` and GH Actions log):**
```
2026-03-21-09:20:22 root DEBUG Starting controller pod
2026-03-21-09:20:22 root DEBUG Bootstrap agent now started
2026-03-21-09:20:22 root DEBUG Contacting Juju controller at 10.152.183.206 to verify accessibility...
2026-03-21-09:32:12 root DEBUG ERROR unable to contact api server after 60 attempts: unable to connect to API: dial tcp 127.0.0.1:45915: connect: connection refused
...
2026-03-21-09:32:58 root ERROR juju controller is bootstrapped but unreachable.
ERROR controller foundations-kubernetes not found
```

**Root cause:** The Juju controller pod is scheduled and starts (the bootstrap agent
launches — evidenced by "Bootstrap agent now started"), but the Juju API server inside the
pod never becomes ready. Juju reaches the controller via a local port-forward
(`127.0.0.1:<ephemeral_port> → ClusterIP:17070`); when the pod/service is not ready, the
port-forward itself fails with `connection refused`. After exactly 60 retry attempts (~12
seconds apart, ~12 minutes total) Juju gives up. This is **not** a `bootstrap-timeout=1800`
(30-minute) timeout — the 60-attempt API contact retry limit is a separate, shorter limit
that fires first.

**Sub-cause: controller pod went into CrashLoopBackOff**

Confirmed by the nginx API LB access log in the juju-crashdump. The kubelet (172.16.0.11)
was actively patching `controller-0/status` from 09:19:55 to 09:20:53, then went completely
silent for 11 minutes (09:20:53 → 09:32:17). This 684-second gap matches the exponential
backoff sequence (10+20+40+80+160+300s ≈ 610s + container runtime). The pod was restarting
repeatedly but crashing quickly each time.

**Key distinguishing signals:**

| Signal | Value in this run |
|---|---|
| Duration from "Contacting controller" to error | ~12 min (60 × ~12s) — retry exhaustion, NOT 30-min bootstrap-timeout |
| `juju show-controller foundations-kubernetes` | `{}` / "not found" — never registered |
| Kubelet activity gap in API LB log | 11m24s gap (09:20:53 → 09:32:17) — CrashLoopBackOff |
| PVC `storage-controller-0` | 200 OK responses → **bound** ✅ — storage was NOT the issue |
| Cinder volumeattachment | 200 OK → **attached** ✅ — OpenStack Cinder worked |
| Kubelet events → 403 at end | Juju RBAC cleanup ran before pod stopped restarting |
| `storageclass controller-foundations-kubernetes-csi-cinder-default` | 404 (Juju's own SC) — created during bootstrap; 404 at start is expected |

**Without container logs, exact crash cause is unknown.** Possible reasons for the container
crashing at startup:
- Juju agent startup failure (missing secret, misconfigured credential, internal error)
- OOMKilled (not enough memory for the container)
- Startup probe/liveness probe failing before the API server could bind

**Detailed timing pattern (run 23372951162 / UUID 990c6ff4) — from nginx API LB log:**
```
09:19:47  juju add-k8s kubernetes_cloud --client --storage csi-cinder-default  ✅ (1.4s)
09:19:49  bootstrap starts; Juju client creates k8s namespace/resources
09:19:54  POST statefulset → 201 (controller-0 pod submitted to k8s scheduler)
09:19:55  Kubelet starts patching controller-0/status (pod running on node 172.16.0.11)
09:20:01  PVC storage-controller-0 → 200 BOUND ✅ (Cinder volume provisioned)
09:20:02  Cinder volumeattachment → 200 ✅ (volume attached to node)
09:20:22  FCE logs: "Bootstrap agent now started / Contacting Juju controller at 10.152.183.206"
          Juju switches from k8s event polling to direct port-forward API contact
09:20:53  LAST kubelet PATCH to controller-0/status
          ← 11-minute gap — controller-0 in CrashLoopBackOff ←
          (10s + 20s + 40s + 80s + 160s + 300s backoff = ~11 min total)
09:32:12  FCE logs: "ERROR unable to contact api server after 60 attempts"
09:32:17  Kubelet resumes patching controller-0/status (pod restarted again)
09:32:18  Kubelet: POST events → 403 FORBIDDEN (Juju cleanup already removed RBAC)
09:32:48  Kubelet: DELETE controller-0 → 200 (Juju cleanup deleted statefulset)
09:32:57  Juju client: GET namespace → 404 (namespace deleted by Juju cleanup)
09:32:58  FCE logs: "juju show-controller foundations-kubernetes → not found"
          Process completed with exit code 1
```

**Additional note — nginx restart at 09:09 (unrelated):**
The syslog shows nginx was killed and restarted at 09:09:31 (~10 minutes before bootstrap):
`nginx.service: Stopping timed out. Terminating` — charm-triggered nginx reconfiguration.
Nginx was fully recovered before the bootstrap began and had no effect on the failure.

**What to collect in future runs:**
```bash
# Immediately after juju bootstrap failure, before cleanup:
kubectl --kubeconfig <kube.conf> -n controller-foundations-kubernetes get pods -o wide
kubectl --kubeconfig <kube.conf> -n controller-foundations-kubernetes get pvc
kubectl --kubeconfig <kube.conf> -n controller-foundations-kubernetes logs controller-0
kubectl --kubeconfig <kube.conf> -n controller-foundations-kubernetes describe pod controller-0
# (Add --previous flag if pod has already restarted)
```

**Substrate:** Observed on `tor3-sqa-shared_maas dh1_j8_2` (shared MAAS — no MAAS logs,
k8s cluster is on OpenStack VMs).

**Observed in:**
- Run 23372951162 (UUID: 990c6ff4-482b-4635-966b-94dfc01cdf1c, tor3-sqa-shared_maas dh1_j8_2)
- Run 23395916011 (UUID: 8ebb62bd-03a1-43a8-9c28-b52b191cdd8d, tor3-sqa-shared_maas dh1_j8_2, 2026-03-22)
  - Same symptom: "Contacting Juju controller at 10.152.183.131" → 60-attempt exhaustion after 11m27s
  - Cluster was fully healthy (all units active/idle) in prior kubernetes-openstack layer
  - All versions identical to working run 1fcd7f2a: Juju 3.6.19, FCE 2.21.2+git.143.gefccf3aa, CK 1.35
  - Working run (1fcd7f2a, same cluster, 2026-03-21) bootstrapped in 14s after "Contacting"
  - Crashdump available: `generated/kubernetes-openstack/juju-crashdump-kubernetes-openstack-2026-03-22-08.55.12.tar.gz`
- Run 23408678722 (UUID: 1a004baa-f2a9-4d0c-b518-5d850966f2ac, tor3-sqa-shared_maas, 2026-03-22)
  - Same symptom: "Contacting Juju controller at 10.152.183.45" → 60-attempt exhaustion after 11m45s
  - Cluster fully healthy (all active/idle, juju status at 20:56:51Z) entering this step
  - All versions identical to working run 1fcd7f2a: Juju 3.6.19, FCE 2.21.2+git.143.gefccf3aa, CK 1.35, MAAS 3.7.1
  - Third confirmed occurrence of this intermittent CrashLoopBackOff pattern on tor3-sqa-shared_maas
  - Crashdump available: `generated/kubernetes-openstack/juju-crashdump-kubernetes-openstack-2026-03-22-21.30.37.tar.gz`

---

_Add more patterns below as they are discovered._

## Notes

- `bootstrap-timeout=1800` (30 minutes) is the configured timeout, but the **60-attempt
  API contact retry limit** (~12 minutes) is a tighter constraint — it fires first if the
  controller pod starts but its API is never reachable.
- Storage class `csi-cinder-default` (from cinder-csi charm) requires OpenStack Cinder to
  provision a PVC for the controller's database. If Cinder is slow or overloaded, this can
  delay or block pod startup.
- The `tor3-sqa-shared_maas` substrate has no MAAS logs available. Investigation is limited
  to GitHub Actions logs and Swift artifacts — no kubectl access to the cluster post-run.
- The controller image repo `ghcr.io/juju` is used for all CAAS bootstraps. Rate limiting
  or transient pull failures from ghcr.io can cause this pattern.

## Version History

- **v1.0** (2026-03-24): Initial version from analysis of run 23372951162 (UUID 990c6ff4).
  Controller pod started but API unreachable; 60-attempt retry exhaustion pattern documented.
- **v1.1** (2026-03-24): Deep analysis using kubernetes-openstack crashdump nginx API LB log.
  CrashLoopBackOff confirmed (11m24s kubelet silence gap). PVC/Cinder storage ruled out.
  Added precise per-second timeline, crashdump artifact guide, and grep patterns for the LB log.
- **v1.2** (2026-03-24): Added third confirmed occurrence — run 23408678722 (UUID 1a004baa,
  tor3-sqa-shared_maas, 2026-03-22). Identical symptom: 11m45s 60-attempt exhaustion on
  tor3-sqa-shared_maas. Pattern now confirmed recurring/intermittent on this substrate.
- **v1.2** (2026-03-24): Second occurrence of same pattern recorded — run 23395916011 (UUID
  8ebb62bd, tor3-sqa-shared_maas dh1_j8_2, 2026-03-22). Same cluster, same versions, healthy
  k8s cluster, working reference run 1fcd7f2a succeeded in 14s on same cluster previous day.
