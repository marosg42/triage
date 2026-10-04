# Step Knowledge: canonicalk8s_maas

## Step Overview

Deploys **Canonical K8s on bare MAAS machines** with the **Terraform Juju provider** against
the `foundations-maas` Juju controller (built earlier by `juju_maas_controller`).

Flow: `terraform apply -var-file=config/canonicalk8s-maas.tfvars` → create Juju model
`canonicalk8s-maas` on `maas_cloud/default` → deploy `k8s` (3 control-plane units) and
`k8s-worker` (3 units) from `config/manifest_canonicalk8s_maas.yaml` → **then** create the
`k8s:cluster ↔ k8s-worker:cluster`, containerd and COS-token integrations.

The whole apply is wrapped in `timeout 4h`, so **any application that stays `blocked` produces
a bare `exit code 124`** four hours later, with no terraform error message. The real cause is
always in the Juju crashdump, never in the runner log.

> **Ordering matters:** the integrations are only applied after both `juju_application`
> resources complete, so whenever an apply hangs, `k8s-worker` reports
> `Missing cluster integration` — that is a *symptom*, not the cause. Always diagnose the
> `k8s` application first.
>
> ⚠️ **Do not assume the provider waits for the app to be active.** In
> terraform-provider-juju **v0.23.3**, `juju_application` `Create` = `CreateApplication` +
> `ReadApplicationWithRetryOnNotFound`, with **no** wait on application-active or unit-idle.
> An earlier version of this file claimed the opposite; that led to a wrong root cause in
> UUID `d15e0b23-4e2d-4957-b95a-13eac4a21f7b` (see Pattern 4). If `k8s` is active and its
> resource is *still* "creating", the provider itself is wedged.

## Swift Artifacts

| Path | Description | When to check |
|---|---|---|
| `generated/canonicalk8s-maas/juju-crashdump-foundations-maas:canonicalk8s-maas.tar.gz` | Juju crashdump: `juju_status.txt`, `juju_status.yaml`, `debug_log.txt`, `model_config.yaml` | **Always — the only real evidence** |
| `generated/github-runner/run.log` | Runner log; contains `timeout 4h ${COMMAND}` and the terraform `Still creating…` lines | Confirm the 4h timeout |
| `generated/github-runner/jobs.json` | Step conclusions; `canonicalk8s_maas` is step 95 | Triage |
| `config/manifest_canonicalk8s_maas.yaml` | Charm channels/revisions and `k8s` config (`load-balancer-*`, `containerd-custom-registries`) | Always |
| `config/canonicalk8s-maas.tfvars` | Model name/cloud | Context |
| `config/networks.yaml` | MAAS subnet reservations — check the metallb range vs `load-balancer-cidrs` | LB/IP questions |
| `generated/sshtest.txt` | SSH tunnel health (virtual_maas) | Rule out tunnel |
| `generated/maas/logs-*.tgz` | Infra node logs (10.241.144.2–4 only) | MAAS-side deploy issues |

> ⚠️ The crashdump contains **no pod-level data** — no `kubectl get pods`, no pod logs, no
> events, despite the per-IP directories in the tarball being empty. Anything inside the
> cluster must be inferred from `debug_log.txt`.

## Key Log Files (inside the crashdump tgz)

| File | What it contains | When to use |
|---|---|---|
| `juju_status.txt` | App/unit/machine status + **Integration table** | First stop; check which integrations exist |
| `debug_log.txt` | Full model debug-log (all units, DEBUG) | Root cause — feature reconcile errors live here |
| `juju_status.yaml` | Machine → MAAS instance-id / AZ / base mapping | Node identification |
| `model_config.yaml` | Model-level config | Rare |

## Grep Patterns

```bash
D=/tmp/ck8sdump/*/debug_log.txt

# 1) Which k8s features failed to deploy (census):
grep -a -o "Feature '[a-z-]*' enabled=[A-Za-z]*,deployed=[A-Za-z]*" $D | sort | uniq -c | sort -rn

# 2) Distinct failure messages for the failing feature (collapses 1000s of retries):
grep -a -o "Failed to deploy MetalLB, the error was: .*" $D | sed 's/^.*error was: //' \
  | sort | uniq -c | sort -rn

# 3) First/last occurrence of the dominant error (how long was it stuck?):
grep -a -n "no route to host" $D | head -2; grep -a -n "no route to host" $D | tail -1

# 4) Non-feature errors (finds secondary charm bugs, e.g. worker cert validation):
grep -a "ERROR" $D | grep -av "load-balancer\|Failed features" \
  | sed 's/^[a-z0-9-]*: [0-9-]* [0-9:]* ERROR //' | cut -c1-160 | sort | uniq -c | sort -rn | head -20

# 5) Confirm the 4h wrapper timeout in the runner log:
grep -an "timeout 4h\|exit code 124" files/<uuid>/generated/github-runner/run.log
```

> `debug_log.txt` is frequently detected as binary — **always use `grep -a`**.

## Known Failure Patterns

### Pattern 1: MetalLB webhook ClusterIP unreachable → `load-balancer` feature never ready → 4h timeout

**Symptom (GitHub Actions):**
```
##[error]Process completed with exit code 124.
module.k8s.juju_application.k8s: Still creating... [236m…s elapsed]
```

**Symptom (`juju_status.txt`):**
```
k8s         blocked  3  1.36/beta  1962  Feature 'load-balancer' is not ready
k8s-worker  blocked  3  1.36/beta  1956  Missing cluster integration
```

**Symptom (`debug_log.txt`, thousands of times):**
```
Failed to deploy MetalLB … failed to upgrade metallb-loadbalancer: failed to create resource:
Internal error occurred: failed calling webhook "ipaddresspoolvalidationwebhook.metallb.io":
Post "https://metallb-webhook-service.metallb-system.svc:443/validate-metallb-io-v1beta1-ipaddresspool?timeout=10s":
dial tcp 10.152.183.81:443: connect: no route to host
```

**Root cause:** The k8sd MetalLB feature registers the `ValidatingWebhookConfiguration` as part
of the helm release, then immediately creates the `IPAddressPool` CR. If the metallb-controller
pod that backs `metallb-webhook-service` is not yet Ready, the **first** apply fails with
`context deadline exceeded`, leaving the helm release wedged (`another operation
(install/upgrade/rollback) is in progress`). Every subsequent reconcile runs helm *upgrade* and
fails on the webhook admission call; the release never self-heals, so the feature stays
`deployed=False` for the whole run and `k8s` never leaves `blocked`.

**Evidence to look for:**
- Error census shows the sequence: `context deadline exceeded` (helm **install**) →
  `another operation … is in progress` → thousands of `no route to host` (helm **upgrade**).
- `EHOSTUNREACH` / `no route to host` on a ClusterIP under Cilium's kube-proxy replacement means
  the Service has **zero ready backends** — i.e. the metallb-controller pod never came up.
  It does *not* by itself prove cluster-wide ClusterIP breakage.

**Do NOT jump to "ClusterIP/Cilium networking is broken cluster-wide":** check the feature
census first. If `dns` reports `deployed=True … enabled at 10.152.183.x` and `network` is
`deployed=True`, ClusterIP service routing works and the fault is MetalLB-specific.

**Careful with the `load-balancer-cidrs` red herring:** `config/networks.yaml` normally
*reserves* the metallb range (e.g. `10.241.144.130–139`) outside the dynamic DHCP pool
(`.150–.250`). Verify before claiming a DHCP/ARP overlap.

---

### Pattern 2: `k8s-worker` rejects its own default `bootstrap-certificates`

**Symptom (`debug_log.txt`, every reconcile on every worker unit):**
```
ERROR unit.k8s-worker/N.juju-log Invalid bootstrap-certificates: . Valid Options are: snap-managed
ERROR unit.k8s-worker/N.juju-log Caught ReconcilerError
```

**Root cause:** The `k8s-worker` charm (seen on 1.36/beta rev 1956) validates
`bootstrap-certificates` against `["snap-managed"]` but is deployed with the value empty/unset,
so every reconcile raises `ReconcilerError`. Independent of Pattern 1, but it will block the
workers from joining even if the control plane goes active.

**Evidence to look for:** grep pattern 4 above; check the error spans the whole run
(first ≈ agent start, last ≈ crashdump time).

---

### Pattern 3: k8s-dqlite never elects a leader → k8sd 500 → charm re-bootstrap loop (400)

**Symptom (GitHub Actions):** exit code **1**, *not* 124 — the failure comes from the pipeline's own
`wait_for` goal-state check (`duration_ms≈3601484`, i.e. 1 h), long before the `timeout 4h` wrapper:
```
ERROR timed out waiting for "canonicalk8s-maas" to reach goal state
##[error]Process completed with exit code 1.
```

**Symptom (`juju_status.txt`):**
```
k8s         blocked  3  1.34/beta  1958  Failed to apply containerd_custom_registries, check logs for details
k8s-worker  waiting  3  1.34/beta  1927  Node node3 unresponsive

k8s/0*  maintenance  executing  Ensuring snap readiness
k8s/1   blocked      idle       Failed to apply containerd_custom_registries, …
k8s/2   waiting      idle       Waiting for Cluster token
```

**Symptom (`debug_log.txt`) — the real evidence is in the indented `body=` lines, not the
`ERROR` lines:**
```
GET /1.0/k8sd/cluster → 500
  "failed to get cluster members: failed to get leader client: no available dqlite leader server found"
  "failed to check if cluster has ready nodes: failed to list nodes: storage is (re)initializing:
   failed to list *core.Node: etcdserver: request timed out"
  "failed to check if cluster has ready nodes: failed to list nodes:
   Get \"https://127.0.0.1:6443/api/v1/nodes\": dial tcp 127.0.0.1:6443: connect: connection refused"
POST /1.0/k8sd/cluster → 400  "cluster is already bootstrapped"
```

**Root cause:** The k8s snap's embedded **k8s-dqlite** datastore on the bootstrap node comes up
"bootstrapped" but never elects a leader, so `kube-apiserver` never listens on `127.0.0.1:6443` and
all k8sd cluster-state reads return 500. The charm treats *any* non-2xx from the cluster endpoint as
"cluster not bootstrapped", regresses to `WaitingStatus('Waiting to bootstrap k8s snap')` and calls
`bootstrap_k8s_snap()`, which k8sd rejects with `400 cluster is already bootstrapped`. Each probe
costs the charm's full ~5-minute request timeout, so the loop repeats every ~5–10 min forever. Not
recoverable in place — only a redeploy that wipes `/var/snap/k8s/common/var/lib/k8sd` clears it.

**`Failed to apply containerd_custom_registries` is a red herring.**
`/var/snap/k8s/common/args/containerd` is written by k8sd only on a *healthy* join.
`containerd.py:containerd_path()` calls `CONTAINERD_ARGS_PATH.read_text()` with no existence check,
so a unit that reached the post-join path in a broken cluster raises `FileNotFoundError`, which the
`@on_error(BlockedStatus("Failed to apply containerd_custom_registries, …"), …, FileNotFoundError, …)`
decorator mislabels. Only the unit that actually joined shows this block; the leader and any unjoined
peer show `Ensuring snap readiness` / `Waiting for Cluster token` instead. **Do not investigate the
registry mirrors** — check them once in the manifest and move on.

**Evidence to look for:**
```bash
D=/tmp/ck8sdump/*/debug_log.txt
grep -ac "no available dqlite leader server found" $D    # dqlite has no leader
grep -ac "storage is (re)initializing" $D
grep -ac "dial tcp 127.0.0.1:6443: connect: connection refused" $D   # apiserver never listened
grep -ac "cluster is already bootstrapped" $D            # the 400 the charm keeps hitting
grep -a "Skipping non-existent arguments file: /var/snap/k8s/common/args/k8s-dqlite" $D | head
grep -a "already part of the cluster" $D                 # which peer actually joined
```
- `Skipping non-existent arguments file: …/args/k8s-dqlite` on **every** reconcile from the first
  hook onward = the datastore never initialised.
- `Presence.UNRESPONSIVE` in the hundreds; workers logging
  `Command timeout: [… kubectl … get nodes nodeX …]` → `Node nodeX unresponsive`.
- Cross-check the substrate is innocent: all machines `started`/`Deployed` in `juju status`, no
  DHCPNAK activity in `logs-*.tgz`, clean `sshtest.txt`.

**Verdict:** upstream k8s-snap / k8sd / k8s-dqlite defect (seen on `1.34/beta`, snap rev 5499),
compounded by two charm robustness bugs (500 → re-bootstrap → 400 loop; wrong status message).
The bundle contains **no** node-level data, so the reason dqlite failed to elect a leader cannot be
established from the archive — recommend adding `journalctl -u snap.k8s.k8sd`, `k8s status` and
`k8s-dqlite` log collection to this step's teardown.

**Reference run:** UUID `473c3c1d-c4af-458e-9395-c22532e1c7f5` (run 33167249307, cluster_6,
`tor3-sqa-virtual_maas`, node4 = bootstrap node `10.241.144.87`, node6 = `k8s/1` the unit that
joined). Timeline: bootstrap 13:37 → apiserver refusing connections 13:40 → no dqlite leader
13:42:20 → last healthy k8sd call 13:46:21 → workers time out 13:48 → 500/400 loop 13:51:21 onward →
step fails 14:26:33.

---

### Pattern 4: terraform-provider-juju v0.23.3 application `Create` never returns → 4h timeout with a *healthy* model

**Symptom (GitHub Actions):** exit code **124**, identical to Pattern 1:
```
module.k8s.juju_application.k8s: Still creating... [239m52s elapsed]
module.k8s_worker["k8s-worker"].juju_application.k8s_worker: Still creating... [239m52s elapsed]
Error: Plugin did not respond ... plugin6.(*GRPCProvider).ApplyResourceChange
##[error]Process completed with exit code 124.
```

**What distinguishes it from Pattern 1/3:** the model is *fine*. All `k8s/N` units close every
reconcile with `Status context closed with: []` (= active) within ~15 min of the apply starting,
every feature reports `deployed=True`, and there are no hook failures — yet both
`juju_application` resources stay pending for the full 4 h.

**Root cause:** the provider hangs in the post-create `ReadApplicationWithRetryOnNotFound`
retry loop (`Attempts: 30`, `Delay: 1s`, `BackoffFunc: retry.DoubleDelay`, `Stop: ctx.Done()`;
retryable = ApplicationNotFound / StorageNotFound / RetryReadError / "connection refused").
With doubling backoff the loop is effectively unbounded and surfaces no error. `k8s-worker`
blocked on `Missing cluster integration` is purely downstream: the `juju_integration`
resources are gated behind the applications.

**Evidence to look for (all available from the bundle):**
```bash
C=files/<uuid>/generated/juju_maas_controller/juju-crashdump-controller-*.tar.gz  # extract first

# 1) Provider identity + retry cadence (one connection pair per pending resource):
grep -ah "<model-uuid>" */var/log/juju/audit.log | grep -ao '"when":"[^"]*"' | sort
#    doubling gaps (4, 4, 9, 17, 34, 68 min) then silence = provider backoff, not model activity

# 2) Per-unit status census — the decisive discriminator:
grep -aoh "Status context closed with: .*" */var/log/juju/logsink.log | sort | uniq -c | sort -rn
#    "[]" for every k8s unit = control plane active -> provider is at fault
```
- The failing layer has **no crashdump of its own** in these bundles; the model's full
  `debug-log` is nevertheless recoverable from the *controller* crashdump under
  `controller_N/var/log/juju/logsink.log` and
  `controller_N/var/log/juju/models/admin-<model>-<uuid6>.log`. Only one controller holds
  the large copy — check file sizes, the other two are ~137 bytes.
- `terraform.tfstate` (not in the bundle) would show only `juju_model.this`.

**Verdict:** terraform-provider-juju defect (v0.23.3). Recommend upgrading the provider and
capping the deploy timeout near the healthy convergence time (~15 min).

**Reference run:** UUID `d15e0b23-4e2d-4957-b95a-13eac4a21f7b` (run 34934214175, cluster_2,
`tor3-sqa-virtual_maas`, k8s 1.36/beta rev 1981, k8s-worker rev 1978). Timeline: apply 08:35:03
→ machines Deployed 08:43:59 → MetalLB transient 08:46–08:48 (Pattern 1 signature, *recovered*)
→ k8s active ~08:50 → last provider retry 10:51:43 → no activity for 1h44m → killed 12:34:59.

---

_Add more patterns below as they are discovered._

## Notes

- `timeout 4h` on `terraform apply` means the runner log carries **no diagnostic value** beyond
  the exit code — go to the crashdump immediately.
- **Two distinct failure shapes exist for this step.** `exit code 124` = the `timeout 4h` wrapper
  fired (Pattern 1). `exit code 1` with
  `ERROR timed out waiting for "canonicalk8s-maas" to reach goal state` = the pipeline's own
  one-hour `wait_for` goal-state check fired first (Pattern 3). Check the exit code before assuming
  the 4 h path.
- In `debug_log.txt` the k8sd HTTP error **bodies** are on indented continuation lines
  (`\tbody={"type":"error",…}`) *below* the `ERROR … Invalid response …` line. A grep restricted to
  `ERROR` lines will miss them and you will end up with a much vaguer root cause — always
  `grep -a 'body={"type":"error"'` as well.
- Terraform state (`terraform.tfstate.d/canonicalk8s-maas/terraform.tfstate`) is *not* in the
  Swift bundle; use the Integration table in `juju_status.txt` to see which integrations exist.
- The crashdump's per-node IP directories (e.g. `10.241.144.86/`) are **empty** — do not expect
  node-local logs.
- On `virtual_maas`, always check `generated/sshtest.txt` first; a clean file means the whole
  archive is trustworthy.
- Improvement worth suggesting on every failure of this shape: add a `juju wait-for` /
  blocked-status watchdog so a permanently blocked app fails the step in minutes instead of 4 h.
