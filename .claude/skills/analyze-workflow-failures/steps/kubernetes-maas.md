# Step Knowledge: kubernetes-maas

## Step Overview

The `kubernetes-maas` layer deploys a Kubernetes cluster (Charmed Kubernetes) on
bare-metal or virtual machines provisioned by MAAS. It uses FCE (`fce build kubernetes-maas`)
which deploys a Juju bundle into the `foundations-maas:kubernetes-maas` model, then waits
for all units to be ready via `juju-wait`. The bundle includes: kubernetes-control-plane,
kubernetes-worker, containerd, etcd, easyrsa, ntp, kubeapi-load-balancer, vault,
vault-mysql-router, mysql-innodb-cluster, hacluster-vault, hacluster-kubeapi-load-balancer,
and cloud-specific subordinates (ovn-chassis, calico, etc.).

## Swift Artifacts

Objects stored under `<uuid>/generated/kubernetes-maas/` that are useful for diagnosing failures:

| Path | Description | When to check |
|---|---|---|
| `generated/kubernetes-maas/log.txt` | FCE build log for this layer | Always — first stop |
| `generated/kubernetes-maas/bundle.yaml` | The deployed bundle | Verify config/overlays |
| `generated/kubernetes-maas/juju_status_foundations-maas_kubernetes-maas.txt` | Human-readable juju status | If present: check unit/machine state at failure |
| `generated/kubernetes-maas/juju_status_foundations-maas_kubernetes-maas.json` | JSON juju status | Machine states, IP addresses |
| `generated/kubernetes-maas/juju-crashdump-*.tar.gz` | Full juju crashdump | Deep investigation: unit agent logs |
| `generated/kubernetes-maas/juju-lint.out` | juju-lint pre-validation | Check for lint errors before deploy |

**Note:** If the Juju controller itself crashed, `juju_status_*.txt/json` and
`juju-crashdump-*.tar.gz` will be **absent** — the post-failure collection steps cannot
run when the controller is unreachable.

Also check:
| Path | Description |
|---|---|
| `generated/foundation.log` | Cross-layer log; captures teardown SSH failures revealing infra state |
| `generated/monitor.log` | Runner-side monitor; last juju status timestamp shows when controller was last alive |

## Grep Patterns

```bash
# Find primary error in log.txt
grep -n "ERROR\|CalledProcessError\|health ping\|connection is shut down\|juju status.*failed" \
  generated/kubernetes-maas/log.txt | tail -20

# Find stuck units at time of failure
grep -n "juju agent status is\|workload status is" generated/kubernetes-maas/log.txt \
  | grep -v "idle\|active" | tail -30

# Find juju-wait invocation and timing
grep -n "juju-wait.*foundations-maas:kubernetes-maas" generated/kubernetes-maas/log.txt

# Check for SSH failures in teardown (infrastructure-level crash indicator)
grep "kex_exchange_identification\|Connection closed by remote host\|ssh.*255" \
  generated/foundation.log | head -20

# Find controller IP from juju_maas_controller step
grep "dns-name.*10\.\|Attempting to connect\|Bootstrap complete" \
  generated/juju_maas_controller/log.txt | head -10
```

## Known Failure Patterns

### Pattern 1: Juju controller connection lost — virtual infrastructure crash

**Symptom:**
```
ERROR:root:WARNING health ping timed out after 30s
ERROR connection is shut down
ERROR:root:juju status --format=json failed: 1

subprocess.CalledProcessError: Command ['juju-wait', '-m', 'foundations-maas:kubernetes-maas',
  '-t', '14400', '--machine-error-timeout', '1800', '--retry_errors', '5', ...]
returned non-zero exit status 1.

# Followed (optionally) by reporting timeout:
##[error]Process completed with exit code 124.   (in build_layer_report.py step)
```

**Root cause:** The Juju controller VM became unreachable mid-deployment. juju-wait
maintains a persistent websocket connection to the Juju controller API; when the controller
crashes or the network between the runner and controller is severed, juju-wait receives a
`health ping timed out` warning followed by `connection is shut down`, then fails
`juju status` with exit code 1. This is an **infrastructure failure**, not a charm failure.

On `tor3-sqa-virtual_maas`, all VMs (MAAS nodes, Juju controller, deployed machines) run
on the same physical hypervisor(s). A host-level failure (OOM kill, kernel panic, hardware
fault, power event) can take down all VMs simultaneously, causing:
1. juju-wait loses controller connection → exit code 1 (not SIGKILL — happens quickly)
2. Post-failure juju status collection skipped (no juju_status artifacts)
3. juju-crashdump skipped (controller unreachable)
4. SSH to MAAS nodes fails in teardown steps

**Key distinguishing features vs. charm failure:**
- juju-wait fails after a **short time** (minutes), not the full 4-hour timeout
- Exit code is **1** (not SIGKILL), preceded by health ping / connection shut down messages
- `juju_status_*.txt/json` **absent** from Swift artifacts (collection failed)
- `juju-crashdump-*.tar.gz` **absent** from Swift artifacts
- SSH to MAAS nodes/controller IPs fails in `foundation.log` teardown section
- `kex_exchange_identification: Connection closed by remote host` (SSH port responds but drops immediately — degraded/restarting state)

**Evidence to look for:**
- `generated/kubernetes-maas/log.txt`: `health ping timed out after 30s` then
  `connection is shut down` immediately before the CalledProcessError
- `generated/kubernetes-maas/` directory: no `juju_status_*` or `juju-crashdump-*` files
- `generated/foundation.log` (teardown): SSH commands to `10.241.144.x` all fail with
  `kex_exchange_identification: Connection closed by remote host`
- `generated/juju_maas_controller/log.txt`: Controller IP (e.g., `10.241.144.81`) —
  confirm it is in the same subnet as the unreachable MAAS nodes

**Investigation steps:**
1. Confirm it's an infra crash, not a charm issue:
   ```bash
   grep "health ping\|connection is shut down" generated/kubernetes-maas/log.txt
   ls generated/kubernetes-maas/juju_status_* 2>/dev/null || echo "No status files — controller was unreachable"
   grep "kex_exchange_identification\|Connection closed by remote" generated/foundation.log
   ```
2. Find controller IP:
   ```bash
   grep "Attempting to connect\|Bootstrap complete\|dns-name" generated/juju_maas_controller/log.txt | head -5
   ```
3. Check monitor.log for last known-good timestamp:
   ```bash
   tail -5 generated/monitor.log
   ```
4. Report the physical host failure to the infra/lab team with: substrate name
   (`tor3-sqa-virtual_maas`), cluster (`cluster_N`), time of failure, and affected IPs.

**From run:** 23162727086 (UUID 0551bd1b, tor3-sqa-virtual_maas cluster_4, branch main, 2026-03-16)

---

### Pattern 2: `juju add-model` fails with MongoDB EOF immediately after HA controller bootstrap

**Symptom:**
```
ERROR failed to create new model: initialising model logs collection:
cannot create index for logs collection logs.<model-uuid>: EOF

subprocess.CalledProcessError: Command ['juju', 'add-model', '-c', 'foundations-maas',
  'kubernetes-maas', 'maas_cloud'] returned non-zero exit status 1.
```

The failure happens at `create_model` — the very first step of the layer — before any
deployment starts. The `add-model` command runs for an unusually long time (~60–70s) before
returning the error.

**Root cause:** Juju's internal MongoDB returned EOF while trying to create the log collection
index for the new model. This occurs when the HA controller's MongoDB replica set is still
in a settling/synchronization phase after adding a HA member. When `juju_maas_controller`
completes and `kubernetes-maas` starts immediately, there may be only 40–60 seconds between
the MongoDB replica set reaching `has-vote` for the final member and the first `add-model`
call — not enough time for MongoDB replication to fully stabilize.

**Key distinguishing features vs. Pattern 1 (controller crash):**
- Failure is at `create_model`, not during `juju-wait`
- The model IS actually created server-side (Juju workers start in debug log; model visible in post-failure status)
- The Juju controller remains healthy post-failure (all units `active/idle`)
- `juju_status_*.txt/json` and `juju-crashdump-*.tar.gz` are **present** (controller was reachable)
- No SSH failures in teardown

**Evidence to look for:**
- `generated/kubernetes-maas/log.txt`: `cannot create index for logs collection logs.<uuid>: EOF`
- `generated/kubernetes-maas/juju_status_foundations-maas_controller.txt`: all controller
  units `active/idle` shortly after failure — controller healthy
- `generated/kubernetes-maas/juju_status_foundations-maas_kubernetes-maas.txt`: model exists
  but is empty (no apps/machines) — model was created server-side despite the error
- Juju debug log (in crashdump): workers for the new model UUID starting at ~the same second
  as the `add-model` CLI call, confirming server-side creation succeeded
- Timing: `juju_maas_controller` step completed with HA member still `adding-vote` < 60s
  before `kubernetes-maas` started

**Investigation steps:**
```bash
# Confirm the specific error
grep "cannot create index\|EOF\|add-model" generated/kubernetes-maas/log.txt

# Check if model was actually created (post-failure status)
head -5 generated/kubernetes-maas/juju_status_foundations-maas_kubernetes-maas.txt

# Confirm controller was healthy
head -20 generated/kubernetes-maas/juju_status_foundations-maas_controller.txt

# Check HA timing — when did machine/2 go from adding-vote to has-vote?
grep "adding-vote\|has-vote" generated/juju_maas_controller/log.txt | tail -5
```

**From run:** 28235638023 (UUID 44c1c826, tor3-sqa-dedicated_maas dh1_j2, branch main, 2026-06-26)

---

### Pattern 3: `juju deploy` fails immediately — charm not published for the bundle's base/channel

**Symptom:**
```
ERROR cannot deploy bundle: cannot resolve charm or bundle "easyrsa":
  selecting releases: charm or bundle not found for channel "1.34/stable", base "amd64/ubuntu/20.04"

  store.(*CharmAdaptor).ResolveCharm:68
  deployer.(*bundleHandler).resolveCharmsAndEndpoints:359: cannot resolve charm or bundle "easyrsa"

subprocess.CalledProcessError: Command ['juju', 'deploy', '-m',
  'foundations-maas:kubernetes-maas', '--debug', '--overlay', ..., '.../bundle.yaml']
  returned non-zero exit status 1.
```

The step fails **very fast** (~1-2 minutes) at the initial `juju deploy`, before any machine
is provisioned or model created.

**Root cause:** The generated bundle pins a charm to a `channel` + `base` combination that
Charmhub does not publish. Juju resolves every charm against Charmhub *before* creating
anything; a single unresolvable charm aborts the whole-bundle deploy with exit 1. Common
trigger: a Charmed Kubernetes track (e.g. `1.34/stable`) drops an older base like
`ubuntu@20.04` (focal), while the bundle's `default-base` / per-app `base` is still focal.

**Key distinguishing features vs. Patterns 1 & 2:**
- Failure is at `juju deploy` charm **resolution**, not `add-model` or `juju-wait`
- Fails in ~1-2 min (not the 4-hour juju-wait timeout, not a ~60s add-model hang)
- Error text: `charm or bundle not found for channel "<ch>", base "amd64/ubuntu/<ver>"`
- The model is never created (`model ... not found` in the pre-deploy export-bundle/status
  probes is expected, not the cause)
- Controller is healthy — `juju_status_foundations-maas_controller.txt` present; no SSH/infra
  failures in teardown

**Evidence to look for:**
```bash
# The resolution error and which charm/channel/base
grep -n "cannot resolve charm or bundle\|charm or bundle not found for channel" \
  generated/kubernetes-maas/log.txt

# Confirm the offending app's pinned base + channel in the bundle
grep -n -A6 "<charm_name>:" generated/kubernetes-maas/bundle.yaml
grep -n "default-base" generated/kubernetes-maas/bundle.yaml
```

**Fix direction:** Re-base the affected charm(s) to a base the channel actually ships
(e.g. `ubuntu@22.04`), or pin a channel that still publishes the required base. Consider a
`juju deploy --dry-run` / Charmhub base-availability pre-flight check in the pipeline.

**From run:** 30684110011 (UUID 306c976d, tor3-sqa-shared_maas dh1_j9_1, branch main,
2026-08-01) — `easyrsa` `1.34/stable` had no `ubuntu@20.04` release.

---

## Notes

- `vault` is always excluded from juju-wait (`-x vault`); `vault-mysql-router/0` stuck
  in "allocating" state near failure time is not necessarily the cause — it's normal for
  this subordinate to be mid-deployment.
- Exit code 124 on `build_layer_report.py` is a **cascade failure** caused by the
  infrastructure being down — the reporter times out trying to reach Weebl/infra. It is
  not an independent failure.

