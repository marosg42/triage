# Step Knowledge: sunbeam_deploy

## Step Overview

This step deploys the Sunbeam OpenStack cluster on pre-provisioned machines. It runs
`products/sunbeam/deploy_sunbeam.py` which:
1. Installs the `openstack` snap on each node
2. Bootstraps the cluster on the first node (`sunbeam cluster bootstrap`)
3. Joins remaining nodes to the cluster concurrently (`sunbeam cluster join`)

Failures here are Sunbeam-side (not MAAS-side), involving Juju, MicroK8s, LXD, or
the `sunbeam` CLI itself.

Entry point: `.github/actions/builds/run-fce-build` → `products/sunbeam/deploy_sunbeam.py`

## Swift Artifacts

| Path | Description | When to check |
|---|---|---|
| `generated/sunbeam/output.log` | Full deploy_sunbeam.py output — primary log | Always first |
| `generated/sunbeam/juju_debug_log_openstack.txt` | Juju model debug log | Charm deployment failures |
| `generated/sunbeam/juju_debug_log_openstack-machines.txt` | Juju machines model debug log | Machine bootstrap issues |
| `generated/sunbeam/juju_status_openstack.txt` | `juju status` snapshot | Which charms/units failed |
| `generated/sunbeam/juju_status_openstack-machines.txt` | Machines model status | Machine allocation state |
| `generated/sunbeam/show_units_openstack.txt` | Unit show output | Detailed unit state |
| `generated/sunbeam/kubectl_get_pod.txt` | k8s pod list | Pod failures |
| `generated/sunbeam/kubectl_get_pod_detailed.txt` | Detailed pod descriptions | Pod events, errors |
| `generated/sunbeam/sunbeam_cluster_list.txt` | `sunbeam cluster list` output | Cluster member state |
| `generated/sunbeam/manifest.yaml` | Sunbeam manifest used for deployment | Config/version verification |
| `generated/sunbeam/sosreport-<node>.tar.xz` | Full sosreport per node (5–15 MB each) | Deep node-level debugging |

`output.log` is the most important file — get it with `get_object` (189KB typical).

## Key Log Files (within sosreport archives)

Sosreports contain system logs if deeper investigation is needed:
- `var/log/juju/` — Juju agent logs
- `var/log/syslog` — System events
- `snap/openstack/` — Sunbeam snap logs

## Grep Patterns

```bash
# Find all errors in output.log
grep -i "ERROR\|Failed\|Traceback\|CalledProcessError" output.log | head -40

# Find cluster join failures
grep -i "cluster join\|cluster bootstrap\|join.*failed\|failed.*join" output.log

# Find Terraform errors
grep -i "terraform\|state lock\|Error acquiring" output.log

# Find k8s errors
grep -i "k8s nodes\|Failed to get k8s\|nodes to update" output.log

# Timeline of key events
grep -E "INFO|ERROR" output.log | grep -v "DEBUG\|Warning\|WARNING"

# Check join success/failure per node
grep -E "Node joined|Command failed.*cluster join" output.log
```

## Known Failure Patterns

### Pattern 1: Terraform state lock contention during concurrent cluster joins

**Symptom (in GitHub Actions log):**
```
subprocess.CalledProcessError: Command ['ssh', ..., 'sunbeam', 'cluster', 'join', ...,
  '--role', 'control', ...] returned non-zero exit status 1.
```

**In `generated/sunbeam/output.log`:**
```
ERROR - [localhost] Command failed: ssh ... <node>.maas -- sunbeam cluster join ...
terraform apply failed:

Error: Error acquiring the state lock
Error message: HTTP remote state already locked, failed to unmarshal body
Lock Info:
  ID:        <uuid>
  Operation: OperationTypeApply
  Who:       ubuntu@<same-node>

Error: Failed to get k8s nodes to update
```

**Root cause:** Multiple nodes run `sunbeam cluster join` concurrently. Each invocation
runs multiple internal `terraform apply` steps against shared remote state. When a
preceding step holds the lock and a subsequent step within the same join process tries
to acquire it (e.g., after k8s node registration fails mid-flight), it hits its own lock.
Once a node's join fails this way, the orphaned lock prevents all subsequent retries on
that node from succeeding.

**Key signals:**
- Lock `Who:` field is `ubuntu@<same-node>` — the node is locked by itself
- `Error: Failed to get k8s nodes to update` appears alongside the lock error
- Some other nodes eventually succeed (partial recovery) — the overall cluster isn't broken
- The failing node consistently fails on every retry until the lock is cleared

**Timeline pattern:**
- Bootstrap of first node: ~30 min (normal)
- Concurrent joins started: all nodes at same time
- First failures: ~15–20 min into joins (k8s update fails → lock orphaned)
- Partial recovery: some nodes join after concurrency drops
- Terminal failure: the locked node fails every retry until timeout

**See also:** No separate patterns file yet — add one if this recurs.

---

### Pattern 2: SSH "Broken pipe" during `sunbeam configure` (2-hour idle timeout)

**Symptom (in GitHub Actions log):**
```
subprocess.CalledProcessError: Command
  ['ssh', ..., '<node>.maas', '--', 'sunbeam', 'configure', '-m', 'manifest.yaml']
returned non-zero exit status 255.
```

**In `generated/sunbeam/output.log`:**
```
ERROR - [localhost] Command failed: ssh ... <node>.maas -- sunbeam configure -m manifest.yaml
...
STDERR: client_loop: send disconnect: Broken pipe
```

**Root cause:** `sunbeam configure` is a long-running interactive command. After answering
prompts (external network, SR-IOV NICs), it enters a silent phase deploying OpenStack
charms via Juju. If there is no SSH output for an extended period, the SSH server's
idle timeout drops the connection. Exit code **255** is the SSH protocol disconnection
code — not a failure of the remote command itself.

**Key diagnostic: check timing gap**
```bash
python3 -c "
import json
data = open('/tmp/output.log').read()
lines = data.splitlines()
configure_lines = [(i,l) for i,l in enumerate(lines) if 'sunbeam configure' in l or 'Broken pipe' in l or 'client_loop' in l]
for i,l in configure_lines: print(i, l)
"
```
If the gap between last output and `Broken pipe` is **exactly 2 hours** (7200s), this is a server-side `ClientAliveInterval` or connection idle timeout.

**Important nuance:** Check the STDOUT in the error dump — if it contains the
`# openrc for demo` / `The cloud has been configured for sample usage.` success message,
the OpenStack deployment actually completed before the hang. The failure is in a
post-configure step (e.g., SR-IOV configuration phase).

**Timeline signature:**
```
01:43:46  sunbeam configure started
01:47:19  last output (SR-IOV prompts answered)
           ... 2h silence ...
03:47:21  Broken pipe
```

**Observed in:** run 23415263364 (UUID: 6b9043d8-..., tor3-sqa-shared_maas dh1_j9_1, 7 physical nodes)

---

### Pattern 3: Terraform Juju provider "inconsistent result" on `glance-to-ceph` during bootstrap

**Symptom (in GitHub Actions log):**
```
subprocess.CalledProcessError: Command
  ['ssh', '-t', ..., 'solqa-shared-maas-server-31.maas', '--', 'sunbeam', 'cluster',
   'bootstrap', '-m', 'manifest.yaml', '--topology', 'single', '--role', 'control',
   '--role', 'compute', '--role', 'storage']
returned non-zero exit status 1.
```

**In `generated/sunbeam/output.log` (STDERR of bootstrap):**
```
terraform apply failed: ...
Error: Provider produced inconsistent result after apply

When applying changes to juju_integration.glance-to-ceph[0], provider
"provider["registry.terraform.io/juju/juju"]" produced an unexpected new
value: .application: planned set element
cty.ObjectVal(map[string]cty.Value{"endpoint":cty.UnknownVal(cty.String),
"name":cty.UnknownVal(cty.String),
"offer_url":cty.StringVal("admin/openstack-machines.microceph")}) does not
correlate with any element in actual.

This is a bug in the provider, which should be reported in the provider's own
issue tracker.
```

**Root cause:** A bug in the Terraform Juju provider when creating cross-model integrations
(SAOS — Software as a Service offers). When Terraform plans `juju_integration.glance-to-ceph`,
the `endpoint` and `name` values for the microceph offer endpoint are `Unknown` at plan time.
After the apply, the provider fails its own consistency check because the actual returned state
doesn't structurally match the planned value. This is a provider-side bug, not a deployment
script issue.

**Effect on cluster state:**
- `generated/sunbeam/sunbeam_cluster_list.txt`: Only the bootstrap node appears (no joins attempted)
- `generated/sunbeam/juju_status_openstack.txt`: Most charms deploy successfully, but:
  - `cinder-volume` SAAS: **blocked**
  - `cinder-volume-mysql-router`: **blocked** — "Missing relation: database" (terraform never completed those integrations)
  - `glance-to-ceph` integration absent

**Key signals:**
- Exit code **1** (not 255) — this is a real terraform failure, not an SSH disconnection
- STDOUT shows `Configure endpoint services? [y/n] (n):` — bootstrap reached the interactive
  phase and proceeded (with default "n"), then terraform failed in the charm-wiring stage
- The error is in `juju_integration.glance-to-ceph[0]`, involving microceph's SAOS offer

**This failure is unrelated to SSH keepalive changes** (the branch that triggered this run,
`sshimprovements`, had already added `-t -o ServerAliveInterval=60 -o ServerAliveCountMax=10`
to SSH commands and those worked correctly — the bootstrap connected and ran for 15 min).

**Snap/provider versions:**
- `openstack` snap: `2024.1/candidate` rev 945 (from manifest `software.charms`)

**Observed in:**
- Run 23454612945 (UUID: 3b3eb76e-..., tor3-sqa-shared_maas dh1_j9_1, solqa-shared-maas-server-31.maas, single topology, branch: sshimprovements)

---

### Pattern 4: SSH "Broken pipe" during `sunbeam cluster join` (idle timeout, false failure)

**Symptom (in GitHub Actions log):**
```
subprocess.CalledProcessError: Command
  ['ssh', ..., 'chespin.maas', '--', 'sunbeam', 'cluster', 'join', ...,
   '--role', 'control', '--role', 'compute', '--accept-defaults']
returned non-zero exit status 255.
```

**In `generated/sunbeam/output.log`:**
```
ERROR - [localhost] Command failed: ssh ... chespin.maas -- sunbeam cluster join ... --role control --role compute --accept-defaults
...
STDERR: client_loop: send disconnect: Broken pipe
```

**Root cause:** `sunbeam cluster join` is a long-running operation (especially with multiple
roles like `control` AND `compute`). The join itself runs completely silently — no output is
produced back to the SSH session during the ~2.5 hours it takes. The SSH server's idle timeout
eventually drops the connection. Exit code **255** is the SSH protocol disconnection code, not
a failure of the remote command.

**Crucially, the join may have already completed** by the time the SSH connection is dropped.
Always verify the actual cluster state before concluding the join failed.

**How to confirm it's a false failure:**
- `generated/sunbeam/sunbeam_cluster_list.txt`: The failing node should appear with `running`
  machine status and active roles matching what was requested.
- `generated/sunbeam/juju_status_openstack.txt`: All charms should be `active/idle` if the
  overall cluster deployment succeeded despite the SSH error.

**Key diagnostic check:**
```bash
# After extracting bundle:
grep "chespin\|<failing_node>" /tmp/<uuid>/generated/sunbeam/sunbeam_cluster_list.txt
```
If the node shows `running` with expected roles `active`, the join succeeded.

**Timeline signature:**
```
13:52:56  cluster join started for chespin (control + compute) concurrently with other nodes
           ... 2h28m of total silence (no output from chespin) ...
16:20:54  Broken pipe — exit code 255
           (other nodes joined in 6–55 min and completed long ago)
```

**Observed in:**
- Run 23437439578 (UUID: bdfaad30-164a-487f-9972-b73083ca1c7e, tor3-sqa-testflinger cluster_1,
  chespin.maas with roles control+compute, duration 2h27m58s)

**Contrast with configure pattern:** The SSH Broken Pipe during `sunbeam configure` (previous
pattern) involves a 2-hour gap and post-configure operations that may still need to run. For
`cluster join`, the join itself is the long silent operation, and if the cluster list confirms
the node joined, no further action is needed on that node.

---

### Pattern 5: Terraform apply timeout during `sunbeam configure` (Neutron API unreachable, 20-min hard timeout)

**Applies to:** `sunbeam_maas_deploy` (dedicated MAAS) and `sunbeam_deploy` steps

**Symptom (in GitHub Actions log / `run.log`):**
```
##[error]Process completed with exit code 1.
```
for the `sunbeam_maas_deploy` step.

**In `all_snaps.tgz → sunbeam-<ts>.log` (Rank 1 CLI log):**
```
subprocess.TimeoutExpired: Command '['/snap/openstack/945/bin/terraform', 'apply', '-auto-approve', '-no-color']' timed out after 1200 seconds
06:00:23,403 sunbeam.utils ERROR Error: Command '['/snap/openstack/945/bin/terraform', 'apply', '-auto-approve', '-no-color']' timed out after 1200 seconds
```
Stack trace ends at `sunbeam/commands/configure.py:293 → self.tfhelper.apply()`.

**In `all_snaps.tgz → demo-setup/terraform-apply-<ts>.log`:**
```
2026-03-29T05:50:28.799Z [ERROR] ...
  diagnostic_summary="Error getting openstack_networking_network_v2 <uuid>:
  Get "http://<vip>:80/openstack-neutron/v2.0/networks/<uuid>":
  OpenStack connection error, retries exhausted. Aborting. Last error was: context deadline exceeded"
```

**Root cause:** `sunbeam configure` runs `terraform apply` for the `demo-setup` plan, which creates
OpenStack resources (flavors, Ubuntu cloud image, external network, demo user, router, subnet,
security groups). This plan:

1. Downloads the Ubuntu noble cloud image from `cloud-images.ubuntu.com` (~600 MB) and uploads it
   to Glance/Ceph — typically 30–60 seconds
2. In parallel, creates the demo user's Neutron network (`openstack_networking_network_v2.user_network`)

After the image upload completes, terraform attempts to read back the `user_network` state from the
Neutron API. If the Neutron endpoint is temporarily unreachable at that moment (e.g., due to I/O
pressure on the shared Ceph backend from the image upload, or transient neutron process restarts),
the terraform provider retries with backoff for up to ~9 minutes before declaring `retries exhausted`.
The entire 1200-second subprocess timeout then fires before terraform can recover or exit cleanly.

**Key diagnostic:**
```bash
# The terraform apply log is the definitive source:
find /tmp/<uuid>-snaps -name "terraform-apply-*.log" -path "*/demo-setup/*"

# Look for the blocking resource and when retries started/ended:
grep -E "ERROR|context deadline|retries exhausted|user_network" terraform-apply-*.log

# Check if neutron had pebble check failures during that window:
grep "neutron.*pebble-check" generated/sunbeam/juju_debug_log_openstack.txt
```

**Key signals:**
- Exit code **1** (not 255) — this is NOT an SSH disconnection; configure was running locally on the runner
- `sunbeam configure` ran after successful `sunbeam cluster bootstrap` and `sunbeam cluster deploy`
- All Juju units `active/idle`; all pods Running — cluster was healthy at failure time
- Terraform apply log shows 9-min silence followed by `context deadline exceeded` on a Neutron resource
- `neutron/N` pebble-check-failed events in juju debug log correlating with the silence window

**Distinguishing from other configure failures:**
- vs. SSH Broken Pipe: no SSH involved; configure runs locally; exit code 1 not 255
- vs. Terraform inconsistent result: inconsistent result fails immediately (<1 min) with a different error message
- vs. Terraform state lock: lock errors appear immediately with a different error message
- This pattern: long silence in apply log + `context deadline exceeded` + `retries exhausted`

**Timeline signature:**
```
05:40:08  sunbeam configure started
05:40:23  terraform apply (demo-setup) started
05:40:27  Ubuntu cloud image download + upload to Glance begins (629 MB)
05:41:05  Image upload complete (active in Glance)
05:41:18  Last successful terraform event
           ... 9-minute silence (Neutron API unreachable, retries ongoing) ...
05:42:56  neutron/1 pebble-check-failed (recovered at 05:43:06)
05:43:39  neutron/1 pebble-check-failed again (recovered at 05:43:49)
05:50:28  terraform: "retries exhausted. Last error was: context deadline exceeded"
           ... terraform error handling continues ...
06:00:23  Python subprocess.TimeoutExpired (1200s from 05:40:23)
```

**Artifacts to check (in addition to usual):**
```
all_snaps.tgz → home/ubuntu/snap/openstack/common/etc/<cluster>/demo-setup/terraform-apply-*.log
all_snaps.tgz → home/ubuntu/snap/openstack/common/etc/<cluster>/demo-setup/config.auto.tfvars.json
all_snaps.tgz → home/ubuntu/snap/openstack/common/logs/sunbeam-<configure-ts>.log
```
The `terraform-apply-*.log` is the most valuable — it contains the terraform DEBUG output and
shows exactly which resource timed out and when.

**Snap/provider versions:**
- `openstack` snap: rev 945, channel `2024.1/stable`
- terraform-provider-openstack: v3.0.0

**Observed in:**
- Run 23699798454 (UUID: 36e0a8b4-b619-4ac4-af0e-df293c970af1, tor3-sqa-dedicated-maas dh1_j6,
  6 Pokémon-named bare-metal nodes, openstack rev 945)

---

### Pattern 6: Traefik routes not ready — 502 Bad Gateway during `sunbeam configure` (race after cluster joins)

**Applies to:** `sunbeam_deploy` step, multi-node clusters on `tor3-sqa-shared_maas`

**Symptom (in GitHub Actions log):**
```
subprocess.CalledProcessError: Command
  ['ssh', ..., 'solqa-shared-maas-server-31.maas', '--', 'sunbeam', 'configure', '-m', 'manifest.yaml']
returned non-zero exit status 1.
```

**In `generated/sunbeam/output.log`:**
```
Error configuring cloud
Traceback (most recent call last):
  File "/snap/openstack/956/lib/python3.12/site-packages/sunbeam/commands/configure.py", line 293, in run
    raise TerraformException(
sunbeam.core.terraform.TerraformException: terraform command failed: /snap/openstack/956/bin/terraform apply -auto-approve -no-color -json
```
Note: STDOUT and STDERR in the output.log are **empty** — the full error comes from the remote node's configure process.

**In `generated/sunbeam/sosreport-<bootstrap-node>.tar.xz` → `home/ubuntu/snap/openstack/common/etc/<cluster>/demo-setup/terraform-apply-<ts>.log`:**
```
2026-03-28T03:48:03.865Z [ERROR] vertex "openstack_networking_quota_v2.network_quota" error:
  Error creating openstack_networking_quota_v2: Expected HTTP response code [200] when accessing
  [PUT http://<traefik-vip>:80/openstack-neutron/v2.0/quotas/<tenant-id>], but got 502 instead: Bad Gateway
2026-03-28T03:48:15.123Z [ERROR] vertex "openstack_networking_subnet_v2.external_subnet["physnet1"]" error:
  Error getting openstack_networking_subnet_v2 <uuid>: Expected HTTP response code [200] when accessing
  [GET http://<traefik-vip>:80/openstack-neutron/v2.0/subnets/<uuid>], but got 502 instead: Bad Gateway
```

**In `generated/sunbeam/juju_debug_log_openstack.txt`** (just before configure runs):
```
unit-traefik-1: 03:47:00 WARNING unit.traefik/1.juju-log ingress:75: relation <ops.model.Relation ingress:83> not ready yet: try again in some time.
unit-traefik-1: 03:47:02 INFO unit.traefik/1.juju-log ingress:83: Provider not ready; validation error encountered:
  ({'port': '9696', 'model': '"openstack"', 'name': '"neutron"'}, ... 'required': ['model', 'name', 'host', 'port']})
```

**Root cause:** After all cluster nodes finish joining, each new join triggers a flood of Juju
`ingress-relation-joined`/`ingress-relation-changed` events across all Traefik units. Traefik
needs time to process these and configure its upstream routes. `deploy_sunbeam.py` calls
`sunbeam configure` immediately after the last `cluster list` confirms all nodes are joined —
without waiting for Traefik to finish settling. When terraform applies the `demo-setup` plan and
contacts the Neutron API through Traefik, the Neutron route hasn't been registered yet and Traefik
returns `502 Bad Gateway`. Terraform fails immediately (no retries); the entire configure step
fails in under 62 seconds.

**Key signals:**
- Exit code **1** (not 255) — terraform failed, not SSH disconnection
- `TerraformException` with **empty stderr** — actual error is inside the sosreport terraform-apply log
- Configure runs for only ~62 seconds (far less than typical ~20+ minutes)
- Terraform-apply log shows immediate `502 Bad Gateway` on first Neutron API calls (no timeout/retry cycle)
- Juju debug log shows Traefik units logging "relation not ready yet" and "Provider not ready" for Neutron just before configure runs
- All Juju units show `active/idle` in the post-failure status snapshot (Traefik eventually converged)

**How to confirm:**
```bash
# Find the terraform-apply log in the bootstrap node's sosreport:
tar -tJf generated/sunbeam/sosreport-<bootstrap-node>-*.tar.xz | grep "demo-setup/terraform-apply"

# Extract and check for 502 errors:
tar -xJf generated/sunbeam/sosreport-<bootstrap-node>-*.tar.xz -C /tmp/sos \
  --wildcards "*/demo-setup/terraform-apply-*.log"
grep "502\|Bad Gateway\|ERROR.*vertex" /tmp/sos/*/home/ubuntu/snap/openstack/common/etc/*/demo-setup/terraform-apply-*.log

# Check Traefik settling in juju debug log around configure start time:
grep "<configure-start-minute>" generated/sunbeam/juju_debug_log_openstack.txt | grep -i "traefik.*not ready\|provider not ready.*neutron"
```

**Distinguishing from other configure failures:**
- vs. SSH Broken Pipe: exit code 1 not 255; configure fails in <2 min not ~2h
- vs. Terraform timeout (Neutron retries exhausted): that pattern takes ~20+ min (retries); this fails immediately (<5s) with a 502 on the first request
- vs. Terraform inconsistent result: inconsistent result involves `juju_integration.glance-to-ceph`, not Neutron quota/subnet
- This pattern: very fast failure + 502 + Traefik "not ready" logs just before configure

**Timeline signature:**
```
02:45:45  Bootstrap completed (single topology, 1 node)
02:47:04  Concurrent joins started (servers 32, 34, 35)
03:11:30  Server-36 joins
03:16:15  Server-43 joins
03:18:30  Server-44 joins (last node dispatched)
03:47:45  cluster list confirms all nodes joined
03:47:50  sunbeam configure started (5s after cluster list)
03:47:00  [50s earlier] Traefik logging "relation not ready yet" + "Provider not ready" for Neutron
03:48:03  terraform apply: 502 Bad Gateway on first Neutron API call
03:48:15  terraform apply: 502 Bad Gateway on subnet GET
03:48:52  TerraformException propagated; configure fails (62s total)
```

**Important: Traefik convergence window can extend far beyond the last join.** The "called
immediately after cluster join" framing understates the risk. Even if `deploy_sunbeam.py`
waits for the cluster list to show all nodes joined before calling configure, Traefik may
still be settling for many minutes. Evidence from a second occurrence:
- Last cluster join: 02:55:45
- Traefik "publish_url" warnings continued until **03:09:05** — 13 minutes after last join
- `sunbeam configure` called at 03:10:50 — only 105s after the last warning
- Configure failed at 03:11:58 (55 seconds total) — same 502 signature

**Snap/provider versions:**
- `openstack` snap: rev 956, channel `2024.1/beta`

**Observed in:**
- Run 23674711306 (UUID: f11a3633-64e5-4039-b625-74e40d4deb69, tor3-sqa-shared_maas dh1_j9_1,
  7 nodes, solqa-shared-maas-server-31.maas as bootstrap, single topology, configure called
  ~5s after cluster list confirmed — Traefik "not ready" warnings visible just before)
- Run 23775401385 (UUID: 8f44bea3-df12-44df-ba5b-95f40ddf29f0, tor3-sqa-shared_maas dh1_j8_1,
  7 nodes, solqa-shared-maas-server-01.maas as bootstrap, single topology, 2026-03-31 main
  branch, snap rev 956 — Traefik "publish_url" warnings continued 13 min after last join,
  configure called 105s after last warning; same 55s rapid failure with empty stderr)

---

### Pattern 7: "No cilium pod found on node" — node name FQDN vs. short hostname mismatch

**Applies to:** `sunbeam_deploy` step, `sunbeam cluster bootstrap` phase

**Symptom (in GitHub Actions log):**
```
subprocess.CalledProcessError: Command
  ['ssh', '-t', ..., 'solqa-shared-maas-server-31.maas', '--',
   'sunbeam', 'cluster', 'bootstrap', '-m', 'manifest.yaml',
   '--topology', 'single', '--role', 'control', '--role', 'compute', '--role', 'storage']
returned non-zero exit status 1.

STDERR:
Error: No cilium pod found on node solqa-shared-maas-server-31.maas
```

**In `generated/sunbeam/output.log`:**
```
ERROR - [localhost] Command failed: ssh -t ... solqa-shared-maas-server-31.maas -- sunbeam cluster bootstrap ...
STDERR: Error: No cilium pod found on node solqa-shared-maas-server-31.maas
```
STDOUT is empty (`b''`).

**Root cause:** The `sunbeam cluster bootstrap` (via `openstack.clusterd`) checks for the Cilium
DaemonSet pod by looking it up using the node's FQDN (e.g., `solqa-shared-maas-server-31.maas`).
However, the Canonical k8s snap registers nodes using the short system hostname
(`solqa-shared-maas-server-31`). The lookup fails because `spec.nodeName` in the pod never
matches the FQDN, producing a false "No cilium pod found" error even though Cilium is healthy.

**Distinguishing evidence (from sosreport kubernetes cluster-info):**
```bash
# Node registered as short hostname:
k8s_kubectl_get_nodes:   solqa-shared-maas-server-31   Ready   ...

# But the sunbeam/hostname label is the FQDN:
k8s_kubectl_get_-o_json_nodes: 
  metadata.name:                     "solqa-shared-maas-server-31"      # short
  kubernetes.io/hostname:            "solqa-shared-maas-server-31"      # short
  sunbeam/hostname:                  "solqa-shared-maas-server-31.maas" # FQDN ← mismatch!

# Cilium pod IS running and healthy (despite the error):
k8s_kubectl_get_--all-namespaces_true_pods:
  kube-system   cilium-v5v5h   1/1   Running   0   ...
```

**Key signals:**
- Bootstrap exits after only ~6 minutes (well below the normal ~30+ min duration)
- Exit code **1**, STDOUT is empty, STDERR contains just the single line "No cilium pod found"
- The sosreport (captured ~1 min after failure) shows Cilium pod `1/1 Running` and DaemonSet
  `DESIRED=1 READY=1` — confirming the check was a false negative
- Cilium network interfaces (`cilium_host`, `cilium_net`, `cilium_vxlan`) appear in syslog
  several minutes before the bootstrap fails
- The `sunbeam/hostname` label on the k8s node contains the FQDN; `metadata.name` does not

**Confirming it's a false negative:**
```bash
# Extract sosreport for bootstrap node, then check:
tar -tJf generated/sunbeam/sosreport-solqa-shared-maas-server-31-*.tar.xz | grep "pods\|nodes"
# Extract and verify:
cat sos_commands/kubernetes/cluster-info/k8s_kubectl_get_--all-namespaces_true_pods | grep cilium
cat sos_commands/kubernetes/cluster-info/k8s_kubectl_get_-o_json_nodes | python3 -c "
import json,sys; data=json.load(sys.stdin)
for n in data['items']:
    labels=n['metadata']['labels']
    print('name:', n['metadata']['name'])
    print('sunbeam/hostname:', labels.get('sunbeam/hostname','(not set)'))
"
```

**Timeline signature:**
```
13:16:26  openstack snap + prepare-node-script installed
13:19:03  sunbeam cluster bootstrap started
13:20:24  k8s snap (v1.32.11 rev 4754, latest/stable) installed as part of bootstrap
13:20:44  k8s control-plane services started
13:20:53  Cilium DaemonSet patched
13:21:09  cilium-agent container started (pod Running)
13:21:43  cilium_host/cilium_net interfaces UP in syslog
           ... 4 min silence (no errors; openstack.clusterd heartbeats every 10s) ...
13:25:32  "No cilium pod found on node solqa-shared-maas-server-31.maas" — exit 1
```

**Snap/provider versions:**
- `openstack` snap: rev 985, channel `2024.1/beta`
- `k8s` snap: v1.32.11 rev 4754, `latest/stable` (manifest: `1.32/stable`)

**Observed in:**
- Run 24136117775 (UUID: 90f21456-419f-4a2b-9292-48a6aae5b926, tor3-sqa-shared_maas dh1_j9_1,
  7 nodes, solqa-shared-maas-server-31.maas as bootstrap node, single topology, main branch, 2026-04-08)

---

### Pattern 8: `juju wait-for` HA timeout — HA controller machines deployed from scratch by MAAS

**Applies to:** `sunbeam_maas_deploy` step, `sunbeam cluster bootstrap` phase (`Juju HA` step)

**Symptom (in GitHub Actions log):**
```
An unexpected error has occurred. Please see https://canonical-openstack.readthedocs-hosted.com/for troubleshooting information.
Error: Command '['/snap/openstack/987/juju/bin/juju', 'wait-for', 'application',
  '-m', 'controller', 'controller', '--timeout', '15m']' returned non-zero exit status 1.
##[error]Process completed with exit code 1.
```

**In `all_snaps.tgz → sunbeam-<bootstrap-ts>.log`:**
```
15:09:55,467  Finished running step 'Bootstrap Juju'. Result: ResultType.COMPLETED
15:09:55,467  Starting step 'Juju HA'
15:09:57,906  juju enable-ha -n 3 --constraints tags=juju-controller
               --to system-id=wknfb4,system-id=mg64te
               → "maintaining machines: 0 / adding machines: 1, 2"
15:09:57,906  Waiting for HA to be enabled
15:09:57,906  juju wait-for application -m controller controller --timeout 15m
15:24:58,264  CalledProcessError: ... returned non-zero exit status 1.
```

**Root cause:** `juju enable-ha` allocates the designated HA controller machines in MAAS and
triggers full curtin OS installations when they are in "Ready" (commissioned but not deployed)
state. The machines must complete the entire MAAS deployment pipeline — squashfs boot, curtin
install, reboot, cloud-init, Juju agent start — before `juju wait-for application controller`
can succeed. If any HA machine's curtin takes > ~12 minutes (leaving < 3 min for reboot +
cloud-init + agent), the hard-coded 15-minute timeout in `steps/juju.py` fires first.

**Timeline signature:**
```
15:09:57  juju enable-ha issued; both HA machines (wknfb4, mg64te) are in "Ready" state
15:10:11  MAAS DeployWorkflow starts for wknfb4 (juju-2)
15:11:11  wknfb4 boots squashfs installer (curtin begins)
15:11:12  mg64te (juju-3) boots squashfs installer (curtin begins)
15:17:10  wknfb4 curtin netboot_off (6 min curtin)
15:17:43  wknfb4 MAAS deployment complete (7.5 min total)
15:18:xx  Juju stops polling wknfb4 — agent connected; one HA member up
15:21:42  mg64te curtin netboot_off (10.5 min curtin)
15:21:50  mg64te reboots into freshly installed Ubuntu
15:24:54  mg64te still in cloud-init (MAAS metadata status POSTs from new OS)
15:24:58  juju wait-for 15-minute timeout fires — mg64te agent not yet running
```

**Key signals:**
- Bootstrap itself reports `ResultType.COMPLETED` — the initial controller bootstrapped fine
- `juju enable-ha` succeeds (`adding machines: 1, 2`)
- The wait-for fires at **exactly** 15 minutes after it was started
- MAAS syslog shows squashfs installer kernel boots for BOTH HA machines ~1 min after enable-ha
- The slower machine's `netboot_off` appears in the syslog, followed by continued metadata POSTs
  from a fresh OS (cloud-init/25.3-0ubuntu1~24.04.1) right up to the timeout
- `generated/sunbeam/output.log` has no error/failure lines — the bootstrap ran silently until
  the wait-for returned; the detailed trace is only in `all_snaps.tgz → sunbeam-<ts>.log`

**Confirming via MAAS logs:**
```bash
# Check both HA machines' boot and netboot_off times:
grep -h "juju-2\|juju-3\|wknfb4\|mg64te" /tmp/maas-logs/*/var/log/syslog \
  | grep "squashfs\|netboot_off\|deployed_os\|DeployWorkflow" | sort

# Confirm machine was still in cloud-init at timeout:
grep -h "metadata/status/mg64te" /tmp/maas-logs/*/var/log/syslog \
  | grep "15:24:5" | head -5
```

**Snap/provider versions:**
- `openstack` snap: rev 987, channel `2024.1/beta`
- Juju agent: 3.6.21

**Observed in:**
- Run 24193447734 (UUID: 13221a4e-4d35-441e-af12-2ffb1ce29201, tor3-sqa-dedicated_maas dh1_j2,
  main branch, 2026-04-09, MAAS 3.7.2)
- Run 24397128547 (UUID: 6238d910-b411-4545-8ee0-1988828c59fc, tor3-sqa-dedicated_maas dh1_j2,
  main branch, 2026-04-14, openstack rev 945 / 2024.1/stable, Juju 3.6.21):
  juju-2 deployed in 7.5 min, juju-3 deployed in 12.3 min — only 2m42s left before timeout;
  both confirmed via MAAS `Status transition DEPLOYING → DEPLOYED` and `netboot_off` in syslog

---

### Pattern 9: `cinder-volume` install hook blocks on `(amqp) integration missing` during parallel joins — 20-minute wait expires

**Applies to:** `sunbeam_deploy` step, `sunbeam cluster join` phase, multi-node parallel join on `tor3-sqa-testflinger`

**Symptom (in GitHub Actions log):**
```
subprocess.CalledProcessError: Command
  ['ssh', ..., 'behaim.maas', '--', 'sunbeam', 'cluster', 'join', '<token>',
   '--role', 'compute', '--role', 'storage', '--accept-defaults']
returned non-zero exit status 1.
```

**In STDERR of the failing join (captured in GitHub Actions log):**
```
wait timed out after 1199.999998071s
Status(
  model=ModelStatus(name='openstack-machines', ...),
  apps={
    'cinder-volume': AppStatus(
      app_status=StatusInfo(current='blocked', message='(amqp) integration missing', ...),
      units={
        'cinder-volume/1': UnitStatus(
          workload_status=StatusInfo(current='blocked', message='(amqp) integration missing', since='18:53:09Z'),
          juju_status=StatusInfo(current='executing', message='running install hook', since='18:53:08Z'),
          machine='1',
        ),
      },
    ),
    ...
  }
)
```

**Key signals:**
- Exit code **1** (not 255) — this is the remote `sunbeam cluster join` process timing out, not an SSH disconnection
- `wait timed out after 1199.999998071s` — the 1200-second (20-minute) internal `juju wait-for model openstack-machines` inside `sunbeam cluster join`
- A `cinder-volume/N` unit on **a different node** (not the joining one) is stuck in `executing: running install hook` with `blocked: (amqp) integration missing`
- The `amqp` relation IS listed as established at the app level — this is a unit-level timing issue, not a missing relation
- The stuck unit is on a node that joined earlier in the same parallel batch (e.g., `avery.maas` joined at the same time as `behaim.maas`)
- After the 20-minute wait expires, **other parallel joins continue and eventually succeed** — confirming the model was still converging, not broken
- In `generated/sunbeam/sunbeam_cluster_list.txt`: the "failing" node (e.g., `behaim`) IS present with its roles `active` — the node joined successfully despite the exit code 1
- In the final `generated/sunbeam/juju_status_openstack-machines.txt`: the `cinder-volume/N` unit that was blocked shows `active idle` — it self-healed after the timeout fired

**Root cause:** All cluster joins run in parallel via `concurrent.futures.ThreadPoolExecutor`. Each node's `sunbeam cluster join` independently waits up to 1200 seconds for the entire `openstack-machines` Juju model to converge. When many nodes join simultaneously, their Juju units (including `cinder-volume` on storage-role nodes) start their install hooks concurrently. The `cinder-volume` install hook:
1. Runs `apt-get install` and charm setup
2. Sets `status-set blocked "(amqp) integration missing"` while waiting for RabbitMQ to deliver credentials
3. Remains in this state until the install hook completes and the amqp relation-joined hook fires

Under concurrent load, this install hook can take >20 minutes. If another node's 1200-second wait starts while the install hook is still running, it will expire before the hook finishes. The failure is a **false negative**: the node itself joined correctly, and the model converges shortly after the timeout.

**Timeline signature:**
```
18:33:25  All 6 parallel joins launched simultaneously
18:53:08  cinder-volume/1 starts install hook on avery.maas (newly joined, storage role)
18:53:09  cinder-volume/1 sets blocked: (amqp) integration missing
18:53:09  behaim's internal juju wait-for starts (1200s countdown)
19:13:15  behaim's 1200s wait expires → sunbeam cluster join exits 1
19:20:06  bohr.maas join succeeds (47 min total)
19:24:06  elvey.maas join succeeds (51 min total)
19:38:40  another node join succeeds
19:41:47  another node join succeeds
19:42:49  last join succeeds — cinder-volume/1 long since recovered
19:42:49  deploy_sunbeam.py calls future.result() for behaim → CalledProcessError raised
```

**How to confirm it's a false negative:**
```bash
# Check if the node is actually in the cluster:
grep "behaim\|<failing_node>" generated/sunbeam/sunbeam_cluster_list.txt

# Check if cinder-volume eventually recovered:
grep "cinder-volume" generated/sunbeam/juju_status_openstack-machines.txt | grep -v "active"
# Expect no output if all units are active

# Check other joins completed successfully:
grep "Node joined cluster" /tmp/run_<id>_failed.log
```

**See also:** LP bug #2121929 ("parallel joins resulted in ReapplyHypervisorStep failure") — same mechanism, different unit (`openstack-hypervisor` instead of `cinder-volume`).

**Snap versions:**
- `openstack` snap: channel `2024.1/beta`, ADDON `sunbeam_2024.1_beta`
- Juju: 3.6.21

**Observed in:**
- Run 25179144394 (UUID: a1c69781-fc6e-49f5-a563-6e8c20ef6c52, tor3-sqa-testflinger cluster_2,
  7 Pokémon-named testflinger nodes, behaim.maas roles compute+storage, avery.maas roles control+storage,
  main branch, 2026-04-30)

---

### Pattern 10: `k8s` cluster-relation-changed hooks + MetalLB/CSI pods not ready block parallel join wait — 30-minute wait expires

**Applies to:** `sunbeam_deploy` step, `sunbeam cluster join` phase, multi-node parallel join on `tor3-sqa-testflinger`

**Symptom (in GitHub Actions log):**
```
subprocess.CalledProcessError: Command
  ['ssh', ..., 'crustle.maas', '--', 'sunbeam', 'cluster', 'join', '<token>',
   '--role', 'control', '--role', 'compute', '--accept-defaults']
returned non-zero exit status 1.
```

**In STDERR of the failing joins (both at ~18:33Z):**
```
wait timed out after 1799.999997804s
Status(
  model=ModelStatus(name='openstack-machines', ...),
  apps={
    'k8s': AppStatus(
      app_status=StatusInfo(current='waiting',
        message='Unready Pods: kube-system/ck-storage-rawfile-csi-node-hv4cx,
                               metallb-system/metallb-speaker-5m8cn', ...),
      units={
        'k8s/0': UnitStatus(
          workload_status=StatusInfo(current='waiting',
            message='Unready Pods: kube-system/ck-storage-rawfile-csi-node-hv4cx,
                                     metallb-system/metallb-speaker-5m8cn',
            since='18:31:52Z'),
          juju_status=StatusInfo(current='executing',
            message='running cluster-relation-changed hook for k8s/2',
            since='18:31:46Z'),
          machine='0',
        ),
        'k8s/1': UnitStatus(
          workload_status=StatusInfo(current='active', message='Ready', ...),
          juju_status=StatusInfo(current='executing',
            message='running cluster-relation-changed hook for k8s/2',
            since='18:33:05Z'),
          machine='1',
        ),
      },
    ),
    ...
  }
)
```

**Note: Two nodes failed simultaneously (7 seconds apart).** Both anonster.maas and crustle.maas emitted
`wait timed out after ~1800s` at 18:33:16Z and 18:33:23Z respectively. The `CalledProcessError` surfaces
at 18:56:46Z — when the last of the 6 parallel futures completes — because `future.result()` is called
in iteration order after all futures are done.

**Key signals:**
- Exit code **1** (not 255) — remote `sunbeam cluster join` process timing out, not SSH failure
- `wait timed out after 1799.999...s` — the 1800-second (30-minute) internal `juju wait-for model openstack-machines` (note: 1800s here vs 1200s in the cinder-volume/amqp variant — timeout constant differs between snap revisions)
- `k8s/N` workload `waiting: Unready Pods: kube-system/ck-storage-rawfile-csi-node-*` — pods are from newly-joined nodes' DaemonSets, not yet scheduled/running
- `k8s/0` and `k8s/1` juju agents `executing: running cluster-relation-changed hook for k8s/<new>` — existing k8s units processing new member admission
- **Two nodes fail nearly simultaneously** (within seconds of each other) — both had the same 1800s deadline starting from the same batch dispatch
- Failed nodes' roles ARE in the cluster: check `sunbeam_cluster_list.txt`
- All units `active idle` in final `juju_status_openstack-machines.txt`

**Root cause:** When multiple nodes join simultaneously as new Kubernetes worker nodes, each triggers
`cluster-relation-changed` hooks across all existing `k8s` units to update cluster membership. In
parallel, Kubernetes schedules new DaemonSet pods (MetalLB speaker + rawfile-CSI node) on the joining
nodes. These DaemonSet pods take time to become `Running`, keeping `k8s/N` workload in `waiting` state
throughout. If the per-join 1800s `juju wait-for model openstack-machines` deadline fires while
cluster-relation-changed hooks are still executing and DaemonSet pods not yet Ready, the join exits 1.

The nodes joined successfully — the failure is a **false negative**. Both `anonster` and `crustle` appear
in the cluster list with their roles active. The model converges after the timeout fires.

**How to confirm it's a false negative:**
```bash
# Check both failing nodes appear in cluster list with active roles:
grep -E "anonster|crustle|<failing_nodes>" generated/sunbeam/sunbeam_cluster_list.txt

# Verify k8s units are all active in final status:
grep "k8s/" generated/sunbeam/juju_status_openstack-machines.txt | grep -v "active"
# Expect no output if all k8s units are active

# Check other joins completed:
grep "Node joined cluster" /tmp/run_<id>_failed.log
```

**Distinguishing from cinder-volume/amqp variant:**
- This variant: blocking unit is `k8s` (workload `waiting`, not `blocked`); message mentions `Unready Pods`; 1800s timeout; multiple nodes fail simultaneously
- cinder-volume/amqp variant: blocking unit is `cinder-volume` (workload `blocked`); message is `(amqp) integration missing`; 1200s timeout; only one node fails

**Timeline signature:**
```
17:22:17  Bootstrap started on ancientminister.maas
17:58:50  Bootstrap completed (~36 min)
17:58:55  All 6 non-bootstrap nodes begin prepare-node-script concurrently
~18:00:33  All 6 join tokens generated; parallel joins dispatched
18:03:03  cinder-volume app status reaches active
18:31:46  k8s/0 starts executing cluster-relation-changed hook for k8s/2 (anonster joining)
18:31:52  k8s/0 workload drops to waiting: Unready Pods (MetalLB + CSI not ready on new nodes)
18:33:05  k8s/1 starts executing cluster-relation-changed hook for k8s/2
18:33:16  anonster's 1800s wait expires → sunbeam cluster join exits 1 (stored in future)
18:33:23  crustle's 1800s wait expires → sunbeam cluster join exits 1 (stored in future)
18:41:32  chespin.maas joins (control; ~41 min total)
18:55:44  another node joins (storage, compute)
18:55:47  another node joins (compute)
18:56:46  last node joins (storage); future.result() raises crustle's stored exception
```

**Snap/provider versions:**
- `openstack` snap: channel `2024.1/beta`
- `k8s` snap: `1.32/stable`
- Juju: 3.6.21

**Observed in:**
- Run 25177679456 (UUID: a12c852e-66cb-4025-85a0-6c0a4c522977, tor3-sqa-testflinger cluster_1,
  7 Pokémon-named testflinger nodes, anonster.maas roles control+storage and crustle.maas roles
  control+compute both failing, main branch, 2026-04-30)

---

### Pattern 11: `sunbeam cluster join` false failure — control-plane node never appears in deployment-labelled K8S node list

**Applies to:** `sunbeam_deploy` step, `sunbeam cluster join` phase, multi-node control-plane joins on `tor3-sqa-shared_maas`

**Symptom (in GitHub Actions log):**
```
subprocess.CalledProcessError: Command
  ['ssh', ..., 'solqa-shared-maas-server-32.maas', '--', 'sunbeam', 'cluster', 'join', '<token>',
   '--role', 'control', '--role', 'compute', '--accept-defaults']
returned non-zero exit status 1.
```
Similar errors occurred for `solqa-shared-maas-server-34.maas` (`control,storage`) and
`solqa-shared-maas-server-36.maas` (`control`).

**In `generated/sunbeam/output.log`:**
```
Warning: Permanently added 'solqa-shared-maas-server-32.maas' (ED25519) to the list of known hosts.
Error: Failed to get k8s nodes to update
```
The step fails at the runner, not with SSH exit code 255.

**In the failing nodes' sosreports**
(`home/ubuntu/snap/openstack/common/logs/sunbeam-<join-ts>.log`):
```
11:33:46 sunbeam.steps.k8s DEBUG K8S nodes filtered by deployment label: [solqa-shared-maas-server-31 only]
11:33:46 sunbeam.steps.k8s DEBUG No matching k8s node found for solqa-shared-maas-server-32.maas, cluster IPs ['10.241.32.68']
11:33:46 sunbeam.steps.k8s DEBUG Failed to get k8s nodes to update
Traceback (most recent call last):
  File "/snap/openstack/1000/lib/python3.12/site-packages/sunbeam/steps/k8s.py", line 474, in _get_k8s_node_to_update
```
Equivalent traces exist for server-34 (`10.241.32.64`) and server-36 (`10.241.32.70`).

**Post-failure state:**
- `generated/sunbeam/sunbeam_cluster_list.txt` shows all 7 machines `running`, including the expected
  control+compute, control+storage, and control-only role combinations.
- `generated/sunbeam/kubectl_get_node.txt` shows the joined control-plane nodes `32`, `34`, and `36`
  as `Ready control-plane,worker`.
- `generated/sunbeam/juju_status_openstack-machines.txt` shows `k8s/1`, `k8s/2`, and `k8s/3` active,
  while bootstrap `k8s/0` on machine 0 is `unknown/lost` by collection time.

**Root cause:** During parallel control-plane joins, the openstack snap's post-join validation step
(`sunbeam.steps.k8s._get_k8s_node_to_update`) polls the Kubernetes API for a deployment-labelled node
matching the joining machine's hostname/IP. For servers 32, 34, and 36, that lookup never found a match
before the command exited, even though the nodes did join and later appeared Ready. This makes the
failure a **false negative in Sunbeam's K8S-node discovery/validation logic** rather than a hard SSH or
Terraform failure. The evidence points to delayed or inconsistent control-plane registration/label
propagation during concurrent control-node joins.

**Supporting evidence:**
- Node 34's `snap.k8s` journal shows transient etcd peer instability for node 36 during the join window:
  `dial tcp 10.241.32.70:2380: connect: connection refused` followed by peer inactive/active flaps.
- The failing nodes' Sunbeam logs repeatedly list only the bootstrap node in the deployment-filtered K8S
  node set immediately before raising `Failed to get k8s nodes to update`.
- The final status snapshot proves the joined control-plane nodes existed after the failure, so the join
  work mostly completed and the validation path is what returned exit 1.

**How to confirm:**
```bash
# Exact false-negative signature in node-local Sunbeam logs (inside sosreports):
tar -xJOf generated/sunbeam/sosreport-<node>-*.tar.xz \
  '*/home/ubuntu/snap/openstack/common/logs/sunbeam-*.log' \
  | grep -E 'No matching k8s node found|Failed to get k8s nodes to update'

# Joined control-plane nodes exist after the failure:
cat generated/sunbeam/kubectl_get_node.txt
cat generated/sunbeam/sunbeam_cluster_list.txt

# Bootstrap/control-plane fallout visible in final Juju status:
grep -E 'k8s/0|k8s/[123]|sunbeam-machine/0' generated/sunbeam/juju_status_openstack-machines.txt
```

**Distinguishing from other join failures:**
- vs. Pattern 1 (terraform state lock): no `Error acquiring the state lock`; stderr is only `Failed to get k8s nodes to update`
- vs. Pattern 4 (SSH Broken pipe): exit code 1, not 255; no `client_loop: send disconnect: Broken pipe`
- vs. Pattern 10 (`juju wait-for` timeout): no `wait timed out after 1799...`; failure is in `sunbeam.steps.k8s` matching logic

**Observed in:**
- Run 25987960158 (UUID: 9407930c-870f-4485-9768-8211e5ff610c, tor3-sqa-shared_maas dh1_j9_1,
  branch `aipoc`, openstack snap rev 1000, k8s v1.32.11 / rev 4754, 2026-05-17)

---

### Pattern 12: `sunbeam configure` hypervisor wait times out because bootstrap `sunbeam-machine/0` is permanently `lost` after controller migration / credential rotation

**Applies to:** `sunbeam_deploy` step, `sunbeam configure` phase, `tor3-sqa-shared_maas`

**Symptom (in GitHub Actions log):**
```text
subprocess.CalledProcessError: Command
  ['ssh', ..., 'solqa-shared-maas-server-31.maas', '--', 'sunbeam', 'configure', '-m', 'manifest.yaml']
returned non-zero exit status 1.
```

**In `generated/sunbeam/output.log`:**
```text
Project external network name [physnet1] (physnet1): # openrc for demo
...
wait timed out after 1799.9999991949999s
...
'openstack-hypervisor/2': UnitStatus(
  workload_status=StatusInfo(current='waiting', message='(certificates) integration incomplete', ...),
...
'sunbeam-machine/0': UnitStatus(
  workload_status=StatusInfo(current='unknown', message="agent lost, see 'juju show-status-log sunbeam-machine/0'"),
  juju_status=StatusInfo(current='lost', message='agent is not communicating with the server', ...),
```

**In the bootstrap node sosreport**
(`var/log/juju/unit-sunbeam-machine-0.log`):
```text
2026-05-16 11:10:27 WARNING ... report migration status failed: failed to report phase progress: connection is shut down
2026-05-16 11:10:31 ERROR juju.worker.apicaller connect.go:209 Failed to connect to controller: invalid entity name or password (unauthorized access)
```

**In the bootstrap node Sunbeam CLI log**
(`home/ubuntu/snap/openstack/common/logs/sunbeam-20260516-125308.933785.log`):
```text
12:56:46,662 ... Apply complete! Resources: 0 added, 0 changed, 0 destroyed.
12:56:46,667 ... Waiting for apps ['openstack-hypervisor'] to be {'unknown', 'active'}
...
13:26:46,809 sunbeam.steps.hypervisor WARNING wait timed out after 1799.9999991949999s
```

**Key signals:**
- Exit code **1** (not 255) — this is a real remote command failure, not SSH disconnect
- Demo-setup terraform completed successfully before the failure (`Apply complete! Resources: 0 added, 0 changed, 0 destroyed.`)
- The failing wait is inside `sunbeam.steps.hypervisor`, after terraform apply, while waiting for the `openstack-machines` model to settle
- `sunbeam-machine/0` is already `lost` in the first wait snapshot and never recovers
- The bootstrap node's `unit-sunbeam-machine-0.log` shows Juju migration / reconnect churn ending in `invalid entity name or password (unauthorized access)`
- `openstack-hypervisor/2` later flips to `waiting: (certificates) integration incomplete`, but final `juju_status_openstack-machines.txt` shows all hypervisors active — that was transient, unlike the lost bootstrap `sunbeam-machine/0`

**Root cause:** `sunbeam configure` successfully finishes its terraform work, then waits for the hypervisor layer to converge. That wait can never succeed because the bootstrap node's `sunbeam-machine/0` Juju unit became permanently `lost` during controller migration / credential rotation at ~11:10Z. The unit restarted, but its final reconnect attempt failed with `invalid entity name or password`, leaving the agent unable to communicate with the controller. A later transient certificate-related flap on `openstack-hypervisor/2` added noise, but the durable blocker was the lost bootstrap `sunbeam-machine/0` unit.

**How to confirm:**
```bash
# Runner-visible timeout and blocking statuses:
grep -n 'wait timed out after 1799\|sunbeam-machine/0\|openstack-hypervisor/2' generated/sunbeam/output.log

# Node-local configure log proving terraform completed and the wait happened afterwards:
tar -xJOf generated/sunbeam/sosreport-<bootstrap-node>-*.tar.xz \
  '*/home/ubuntu/snap/openstack/common/logs/sunbeam-20260516-125308.933785.log' \
  | grep -E 'Apply complete|Waiting for apps|wait timed out'

# Bootstrap unit agent failure:
tar -xJOf generated/sunbeam/sosreport-<bootstrap-node>-*.tar.xz \
  '*/var/log/juju/unit-sunbeam-machine-0.log' \
  | grep -E 'migration phase|connection is shut down|invalid entity name or password'
```

**Observed in:**
- Run 25959668487 (UUID: ac3ebe2d-549b-4b76-824e-4230c86c2c61, tor3-sqa-shared_maas dh1_j9_1,
  branch `main`, openstack snap rev 1000 / `2024.1/beta`, 2026-05-16)

---

### Pattern 13: `sunbeam cluster join` false-negative — 1200s wait expires while hypervisor hooks are still converging

**Applies to:** `sunbeam_deploy` step, `sunbeam cluster join` phase, multi-node join on `tor3-sqa-testflinger`

**Symptom (in GitHub Actions log):**
```
subprocess.CalledProcessError: Command
  ['ssh', ..., 'barbos.maas', '--', 'sunbeam', 'cluster', 'join', '<token>',
   '--role', 'compute', '--role', 'storage', '--accept-defaults']
returned non-zero exit status 1.
```

**In STDERR of the failing join:**
```
wait timed out after 1199.999998668s
Status(
  model=ModelStatus(name='openstack-machines', ...),
  apps={
    'openstack-hypervisor': AppStatus(
      units={
        'openstack-hypervisor/0': UnitStatus(
          workload_status=StatusInfo(current='active', ...),
          juju_status=StatusInfo(current='executing', message='running config-changed hook', ...),
        ),
        'openstack-hypervisor/1': UnitStatus(... current='executing' ...),
        'openstack-hypervisor/2': UnitStatus(... current='executing' ...),
      },
    ),
  },
)
```
Only machines `0..3` exist in the timeout snapshot; later join machines are not present yet.

**Supporting evidence:**
- `generated/sunbeam/juju_debug_log_openstack-machines.txt` shows `openstack-hypervisor/1` and `/2` still finishing `config-changed` / relation hooks throughout `08:50-08:52Z`
- The same log shows later machines still being admitted **after** the timeout fired:
  - `08:52:22` → `machine 4 already started`
  - `08:52:56` → `machine 5 already started`
- Final state proves recovery:
  - `generated/sunbeam/sunbeam_cluster_list.txt` shows `barbos` `running` with `compute` and `storage` active
  - `generated/sunbeam/juju_status_openstack-machines.txt` shows all `cinder-volume`, `openstack-hypervisor`, `k8s`, and `sunbeam-machine` units `active idle`

**Root cause:** `sunbeam cluster join` uses an internal 1200-second `juju wait-for model openstack-machines`. In this run, `barbos` joined, but the model was still converging: hypervisor hooks on already-admitted nodes were still executing and additional queued joins were still being brought in. The pipeline's `products/sunbeam/deploy_sunbeam.py` submits joins via `ThreadPoolExecutor(max_workers=3)` and later raises on `future.result()`, so one join timing out aborts the whole step even when the node and overall cluster finish converging shortly afterwards. This is a **false negative** caused by the per-join wait deadline being shorter than real convergence under multi-node churn.

**How to confirm:**
```bash
# Runner-visible timeout:
grep -n 'wait timed out after 1199' generated/sunbeam/output.log

# Timeout snapshot shows transient executing hooks, not a durable error:
grep -n 'openstack-hypervisor/0\|openstack-hypervisor/1\|openstack-hypervisor/2\|running config-changed hook' generated/sunbeam/output.log

# Juju model still admitting later nodes after timeout:
grep -n '08:52:22\|08:52:56' generated/sunbeam/juju_debug_log_openstack-machines.txt

# Final state shows success despite the exit code:
cat generated/sunbeam/sunbeam_cluster_list.txt
cat generated/sunbeam/juju_status_openstack-machines.txt
```

**Distinguishing from other join false negatives:**
- vs. Pattern 9 (`cinder-volume` / `(amqp) integration missing`): same 1200s class of timeout, but here the snapshot shows **active** units with transient `executing` hooks rather than a `blocked` `cinder-volume` workload
- vs. Pattern 10 (`k8s` / Unready Pods): this run has a 1200s timeout and no `k8s` workload `waiting` message
- vs. Pattern 11 (`Failed to get k8s nodes to update`): stderr is `wait timed out ...`, not a K8S node-matching exception

**Observed in:**
- Run 25955678538 (UUID: a82859ea-8b32-4e7a-bc07-e751dcec68f0, tor3-sqa-testflinger cluster_3, branch `main`, `barbos.maas` roles `compute,storage`, openstack snap `2024.1/beta`, 2026-05-16)
- Run 25948797819 (UUID: f846183c-519b-4dc5-affd-611837fb05b1, tor3-sqa-testflinger cluster_3, branch `main`, `fizeau.maas` roles `control,storage`, openstack snap `2024.1/beta`, 2026-05-16); stderr showed `wait timed out after 1199.9999989389999s`, final `sunbeam_cluster_list.txt` showed `fizeau` `running` with `control`+`storage`, `kubectl_get_node.txt` showed `fizeau Ready control-plane,worker`, and `juju_debug_log_openstack-machines.txt` still admitted machines 4 and 5 after the timeout.
- Run 25940588751 (UUID: 72cea7f3-5a7d-4baa-ac17-ebb88051fc40, tor3-sqa-testflinger cluster_3, branch `main`, `barbos.maas` roles `control,compute`, openstack snap `2024.1/beta`, 2026-05-15); stderr showed `wait timed out after 1799.999998577s`, the timeout snapshot already had `barbos` machine `3` with `k8s/2` and `openstack-hypervisor/2` active, `openstack-hypervisor/3` on later machine `5` still `executing` `ceph-access-relation-changed`, and final `sunbeam_cluster_list.txt` / `kubectl_get_node.txt` showed `barbos` fully joined and Ready.
- Run 25928796613 (UUID: 02c1a007-6d9c-4a94-b412-12f62b3bceb8, tor3-sqa-testflinger cluster_3, branch `main`, `napple.maas` roles `control,storage` timed out first after `1199.9999981559995s`, then `fizeau.maas` roles `control,compute` later raised the terminal `CalledProcessError` after `1799.9999977789994s`; both nodes were present in final `sunbeam_cluster_list.txt`, final `kubectl_get_node.txt` showed `fizeau` and `napple` Ready, and final `juju_status_openstack-machines.txt` showed `cinder-volume/2`, `openstack-hypervisor/2`, and all `k8s/*` units `active idle`.
- Run 25932491932 (UUID: a14f61b1-6b73-400c-9e54-5761c4e197e9, tor3-sqa-testflinger cluster_1, branch `main`, `ancientminister.maas` roles `control,compute`, openstack snap `2024.1/beta`, 2026-05-15); stderr showed `wait timed out after 1799.9999987379997s`, the timeout snapshot had `cinder-volume/3` still `waiting` on backends with `cinder-volume-ceph/3` `executing` `ceph-access-relation-changed` and `openstack-hypervisor/3` still `executing` `ovsdb-cms-relation-changed`, but final `sunbeam_cluster_list.txt`, `kubectl_get_node.txt`, and `juju_status_openstack-machines.txt` showed `ancientminister` fully joined and the model converged.

---

### Pattern 14: `sunbeam configure` runs before Neutron finishes converging on a newly joined control-plane node

**Applies to:** `sunbeam_deploy` step, `sunbeam configure` phase, `tor3-sqa-testflinger`

**Symptom (in GitHub Actions log):**
```
subprocess.CalledProcessError: Command ['ssh', ..., 'heatmor.maas', '--',
 'sunbeam', 'configure', '-m', 'manifest.yaml'] returned non-zero exit status 1.
```

**In `generated/sunbeam/output.log`:**
```
16:35:59  sunbeam cluster list
          ancientminister running control active
16:35:59  sunbeam configure -m manifest.yaml
16:37:35  Error configuring cloud
          sunbeam.core.terraform.TerraformException: terraform command failed: ... terraform apply ...
```
The terraform wrapper reports an empty stderr, so the useful evidence comes from the post-failure snapshots.

**Supporting evidence:**
- `products/sunbeam/deploy_sunbeam.py` calls `configure_sunbeam(nodes[0])` immediately after `resize_cluster(nodes[0])`; there is no wait for the `openstack` model to become healthy.
- `generated/sunbeam/kubectl_get_pod_detailed.txt`: `neutron-1` was created at `16:27:23Z` on `crustle`, but its `Ready` condition did not become `True` until `17:03:00Z`.
- `generated/sunbeam/juju_status_openstack.txt` (17:04:26Z): `neutron/1` is still `blocked` with `(container:neutron-server) healthcheck failed: online`.
- The final machine-side state had already converged (`generated/sunbeam/sunbeam_cluster_list.txt` shows all requested nodes running), so the failure is in service convergence after the joins, not in the joins themselves.

**Root cause:** `deploy_sunbeam.py` treats a successful `sunbeam cluster list` / `cluster resize` as sufficient to start `sunbeam configure`, but in this run the OpenStack control plane was still converging. A newly added Neutron unit on the joined control-plane node was not yet healthy, so the Neutron API layer was incomplete when `sunbeam configure` launched its demo-setup terraform apply. Terraform then failed quickly inside the snap with only the generic `terraform command failed` wrapper surfaced to the runner. This is effectively a premature-configure race: cluster membership had converged enough to list nodes, but the `openstack` model had not.

**How to confirm:**
```bash
# Configure starts immediately after the final cluster list:
grep -n 'sunbeam cluster list\|sunbeam configure -m manifest.yaml' generated/sunbeam/output.log

# Newly added neutron pod is not Ready until well after the failure:
python3 - <<'PY'
import yaml
with open('generated/sunbeam/kubectl_get_pod_detailed.txt') as f:
    data=yaml.safe_load(f)
for item in data['items']:
    if item.get('metadata', {}).get('name') == 'neutron-1':
        print(item['metadata']['creationTimestamp'])
        for cond in item['status']['conditions']:
            if cond['type'] == 'Ready':
                print(cond['lastTransitionTime'])
PY

# Post-failure status still shows neutron unhealthy:
grep -n 'neutron.*healthcheck failed' generated/sunbeam/juju_status_openstack.txt
```

**Observed in:**
- Run 25921180840 (UUID: 72595a1b-fa63-4f49-9917-5bcae8737c4c, tor3-sqa-testflinger cluster_1, branch `main`, openstack snap rev 1000 / `2024.1/beta`, 2026-05-15).

---

_Add more patterns below as they are discovered._

## Notes

- `generated/sunbeam/output.log` is the single most valuable file for this step — always check it first
- The bootstrap node (typically the first listed in `nodes.yaml`) is not involved in the join failures; check `juju_status_openstack.txt` to see if bootstrap itself had issues
- `sunbeam cluster join` is retry-safe for most errors, **but not** when the Terraform state is locked — a stale lock must be force-unlocked before retrying
- On `tor3-sqa-testflinger` substrate, machines have MAAS hostnames like `ancientminister.maas`, `anonster.maas`, etc. (Pokémon-themed)
- On `tor3-sqa-shared_maas` substrate, machines are named `solqa-shared-maas-server-<N>.maas` (numbered physical servers)
- `sosreport-<node>.tar.xz` files are large (5–16 MB each) — download only the specific failing node's report if needed
- Exit code **255** from an SSH command always means SSH connection failure, not a remote process failure

## Version History

- **v1.0** (2026-03-23): Initial version from analysis of run 23409860645 (UUID: 0b0a8518-..., tor3-sqa-testflinger-cluster_1)
- **v1.1** (2026-03-23): Added SSH Broken Pipe pattern from analysis of run 23415263364 (UUID: 6b9043d8-..., tor3-sqa-shared_maas dh1_j9_1)
- **v1.2** (2026-03-23): Added SSH Broken Pipe during `cluster join` (false failure) pattern from analysis of run 23437439578 (UUID: bdfaad30-..., tor3-sqa-testflinger cluster_1, chespin.maas)
- **v1.4** (2026-03-27): Added `terraform apply` timeout during `sunbeam configure` — Neutron API unreachable after Glance image upload, demo-setup `user_network` GET retries exhausted over 9 min pushing apply past 1200s limit; from run 23699798454 (UUID: 36e0a8b4, tor3-sqa-dedicated-maas dh1_j6, rev 945)
- **v1.5** (2026-03-30): Added Traefik routes not ready (502 Bad Gateway) pattern — `sunbeam configure` called immediately after last cluster join completes; Traefik still processing ingress-relation-joined events returns 502 on first Neutron API calls; from run 23674711306 (UUID: f11a3633, tor3-sqa-shared_maas dh1_j9_1, rev 956)
- **v1.7** (2026-04-08): Added "No cilium pod found" pattern — node name FQDN vs. short hostname mismatch; sunbeam-clusterd checks for Cilium pod by FQDN (`sunbeam/hostname` label) but k8s node is registered by short hostname; false negative; Cilium was 1/1 Running throughout; bootstrap exits in ~6 min; from run 24136117775 (UUID: 90f21456, tor3-sqa-shared_maas dh1_j9_1, openstack rev 985, k8s v1.32.11 rev 4754, 2026-04-08).
- **v1.8** (2026-04-09): Added `juju wait-for` HA timeout pattern — `juju enable-ha` triggers full MAAS curtin deployment of HA controller machines from "Ready" state; 15-minute hard-coded timeout in `steps/juju.py` insufficient when slower machine needs ~10.5 min curtin + 3+ min post-reboot; from run 24193447734 (UUID: 13221a4e, tor3-sqa-dedicated_maas dh1_j2, openstack rev 987, MAAS 3.7.2, 2026-04-09).
- **v1.9** (2026-04-14): Second occurrence of `juju wait-for` HA timeout — same cluster (dh1_j2), openstack rev 945 / 2024.1/stable; juju-3 took 12.3 min to deploy, leaving only 2m42s before timeout; from run 24397128547 (UUID: 6238d910, 2026-04-14).
- **v2.0** (2026-05-04): Added `cinder-volume` install hook blocks on `(amqp) integration missing` during parallel joins — same mechanism as LP bug #2121929 (parallel joins cause Juju model churn; a unit on a concurrently-joining peer node runs its install hook for >20 min; behaim's 1200s internal wait expires; node is actually in the cluster and unit self-heals); from run 25179144394 (UUID: a1c69781, tor3-sqa-testflinger cluster_2, 2024.1/beta, 2026-04-30).
- **v2.1** (2026-05-04): Added `k8s` cluster-relation-changed + MetalLB/CSI pods not ready variant of parallel join false failure — two nodes (anonster, crustle) fail simultaneously after 1800s wait; blocking condition is k8s cluster membership hooks running while DaemonSet pods for new nodes are not yet Ready; same false-failure outcome as v2.0 but different blocker and 1800s (not 1200s) timeout; from run 25177679456 (UUID: a12c852e, tor3-sqa-testflinger cluster_1, 2024.1/beta, 2026-04-30).
- **v2.2** (2026-05-19): Added control-plane join false-negative pattern — `sunbeam.steps.k8s._get_k8s_node_to_update` cannot find the joining node in the deployment-labelled K8S node list, raising `Failed to get k8s nodes to update` even though the control-plane nodes appear later as Ready; from run 25987960158 (UUID: 9407930c-870f-4485-9768-8211e5ff610c, tor3-sqa-shared_maas dh1_j9_1, branch `aipoc`, openstack rev 1000).
- **v2.3** (2026-05-19): Added configure-time hypervisor wait timeout pattern — demo-setup terraform completes, but `sunbeam configure` times out after 1800s because bootstrap `sunbeam-machine/0` is permanently `lost` after controller migration / credential rotation and later reconnects fail with `invalid entity name or password`; from run 25959668487 (UUID: ac3ebe2d-549b-4b76-824e-4230c86c2c61, tor3-sqa-shared_maas dh1_j9_1, branch `main`, openstack rev 1000 / `2024.1/beta`).
- **v2.4** (2026-05-19): Added 1200s parallel-join false-negative variant — `barbos.maas` (`compute,storage`) timed out in `sunbeam cluster join` while `openstack-hypervisor` hooks were still executing and later nodes were still being admitted; final cluster and Juju status snapshots proved full convergence after the timeout; from run 25955678538 (UUID: a82859ea-8b32-4e7a-bc07-e751dcec68f0, tor3-sqa-testflinger cluster_3, branch `main`).
- **v2.5** (2026-05-19): Added second confirmation of the 1200s parallel-join false-negative pattern on `tor3-sqa-testflinger` cluster_3 — `fizeau.maas` (`control,storage`) timed out in `sunbeam cluster join`, but final cluster/K8S/Juju snapshots showed the node fully joined and later machines were still being admitted after the timeout; from run 25948797819 (UUID: f846183c-519b-4dc5-affd-611837fb05b1, branch `main`).
- **v2.6** (2026-05-19): Added third confirmation of the same parallel-join false-negative family on `tor3-sqa-testflinger` cluster_3 — `barbos.maas` (`control,compute`) timed out after 1800s in `sunbeam cluster join`, but the timeout snapshot already showed `barbos` active in Juju while a later node's `openstack-hypervisor` hook was still executing, and final cluster/K8S snapshots showed `barbos` fully joined; from run 25940588751 (UUID: 72cea7f3-5a7d-4baa-ac17-ebb88051fc40, branch `main`).
- **v2.7** (2026-05-19): Added fourth confirmation of the same `tor3-sqa-testflinger` cluster_3 parallel-join false-negative family — `napple.maas` timed out first after 1200s and `fizeau.maas` later surfaced the fatal `CalledProcessError` after 1800s, but final cluster/K8S/Juju snapshots showed both nodes fully joined and all relevant units recovered to `active idle`; from run 25928796613 (UUID: 02c1a007-6d9c-4a94-b412-12f62b3bceb8, branch `main`).
- **v2.8** (2026-05-19): Added fifth confirmation of the same `tor3-sqa-testflinger` parallel-join false-negative family — `ancientminister.maas` (`control,compute`) timed out after 1800s on cluster_1 while `cinder-volume/3`, `cinder-volume-ceph/3`, and `openstack-hypervisor/3` were still converging, but final cluster/K8S/Juju snapshots showed `ancientminister` fully joined and the model recovered; from run 25932491932 (UUID: a14f61b1-6b73-400c-9e54-5761c4e197e9, branch `main`).
- **v2.9** (2026-05-19): Added premature `sunbeam configure` / Neutron convergence race pattern — `deploy_sunbeam.py` starts `sunbeam configure` immediately after `cluster resize` with no wait for the `openstack` model; in run 25921180840 (UUID: 72595a1b-fa63-4f49-9917-5bcae8737c4c, tor3-sqa-testflinger cluster_1) `neutron-1` was created at 16:27:23Z but only became Ready at 17:03:00Z, while `sunbeam configure` failed at 16:37:35Z and post-failure status showed `neutron/1` blocked with `(container:neutron-server) healthcheck failed: online`.
