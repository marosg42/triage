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
data = open('<work_dir>/output.log').read()
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
grep "chespin\|<failing_node>" <work_dir>/<uuid>/generated/sunbeam/sunbeam_cluster_list.txt
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
find <work_dir>/<uuid>-snaps -name "terraform-apply-*.log" -path "*/demo-setup/*"

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
tar -xJf generated/sunbeam/sosreport-<bootstrap-node>-*.tar.xz -C <work_dir>/sos \
  --wildcards "*/demo-setup/terraform-apply-*.log"
grep "502\|Bad Gateway\|ERROR.*vertex" <work_dir>/sos/*/home/ubuntu/snap/openstack/common/etc/*/demo-setup/terraform-apply-*.log

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
grep -h "juju-2\|juju-3\|wknfb4\|mg64te" <work_dir>/maas-logs/*/var/log/syslog \
  | grep "squashfs\|netboot_off\|deployed_os\|DeployWorkflow" | sort

# Confirm machine was still in cloud-init at timeout:
grep -h "metadata/status/mg64te" <work_dir>/maas-logs/*/var/log/syslog \
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
grep "Node joined cluster" <work_dir>/run_<id>_failed.log
```

**See also:** LP bug #2121929 ("parallel joins resulted in ReapplyHypervisorStep failure") — same mechanism, different unit (`openstack-hypervisor` instead of `cinder-volume`).

**Snap versions:**
- `openstack` snap: channel `2024.1/beta`, ADDON `sunbeam_2024.1_beta`
- Juju: 3.6.21

**Later 1800-second variant:** On newer runs in the same false-negative family, the same storage-role convergence delay can surface with an **1800s** wait instead of 1200s. In those cases the timeout snapshot may show `cinder-volume/3` in `waiting: (cinder-volume) integration incomplete`, `cinder-volume-ceph/3` in `waiting: (ceph) integration incomplete`, and a later `openstack-hypervisor/3` unit still executing relation hooks. The diagnosis is unchanged: the join timed out while a concurrently joining storage node was still converging, and the model recovered afterwards.

**Observed in:**
- Run 25179144394 (UUID: a1c69781-fc6e-49f5-a563-6e8c20ef6c52, tor3-sqa-testflinger cluster_2,
  7 Pokémon-named testflinger nodes, behaim.maas roles compute+storage, avery.maas roles control+storage,
  main branch, 2026-04-30)
- Run 26664595605 (UUID: 1e9216b1-618d-4c01-89d5-0b358b6c6fdb, tor3-sqa-testflinger cluster_2,
  `ditto.maas` roles `control,compute`; timed out after `1799.9999980110006s` while `cinder-volume/3`,
  `cinder-volume-ceph/3`, and `openstack-hypervisor/3` were still converging, but final cluster/Juju snapshots showed full recovery; branch `main`, 2026-05-29)

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
grep "Node joined cluster" <work_dir>/run_<id>_failed.log
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
<<<<<<< Updated upstream
=======
- Run 26338138903 (UUID: 71805006-220e-4a49-a12b-89e235c314ba, tor3-sqa-shared_maas dh1_j9_1,
  branch `main`, control-plane joins for servers 32/34/36 failed with `Failed to get k8s nodes to update`,
  but post-failure snapshots showed those nodes `Ready` in Kubernetes and `running` in the cluster list; k8s v1.32.13, 2026-05-23)
>>>>>>> Stashed changes

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
<<<<<<< Updated upstream
=======
- Run 26252890519 (UUID: 6ec1d2ac-4bba-4447-b337-a0dfa1acec2b, tor3-sqa-testflinger cluster_1, branch `main`, `doble.maas` roles `control,storage`, openstack snap `2024.1/beta`, 2026-05-21); stderr showed `wait timed out after 1799.9999971770003s`, the timeout snapshot had `openstack-hypervisor/1` still `executing` `nova-service-relation-changed` and `openstack-hypervisor/2` still `executing` `ceph-access-relation-changed` / `waiting` on certificates, `juju_debug_log_openstack-machines.txt` admitted machines 4, 5, and 6 after the timeout, and final `sunbeam_cluster_list.txt`, `kubectl_get_node.txt`, and `juju_status_openstack-machines.txt` showed `doble` fully joined and the model converged.
- Run 26338143617 (UUID: f20d40df-9e36-4c83-aaec-fd4953ea7236, tor3-sqa-testflinger cluster_1, branch `main`, `ledian.maas` roles `control,compute`, openstack snap `2024.1/beta`, 2026-05-23); stderr showed `wait timed out after 1799.999997808s`, the timeout snapshot already had `ledian` machine `2` `started`, `k8s/1` `active` / `Ready` since `18:30:43Z`, and `openstack-hypervisor/2` still `executing` `config-changed hook`; final `sunbeam_cluster_list.txt`, `kubectl_get_node.txt`, `juju_status_openstack-machines.txt`, and `juju_status_openstack.txt` showed `ledian` fully joined and the cluster converged.
- Run 26657349467 (UUID: 87f87610-4c3f-4b6d-ad7e-7c17b5d2483a, tor3-sqa-testflinger cluster_3, branch `main`, `cajal.maas` roles `control,compute`, `euler.maas` roles `control,storage`, openstack snap `2024.1/beta`, 2026-05-29); `euler` timed out first after `1799.999997515s`, then `cajal` timed out 34s later after `1799.999997676s`, but the timeout snapshot already had machines `0..3` started with `k8s/2` on `cajal` `waiting: Waiting for Cluster token` and `openstack-hypervisor/2` still `executing` / `waiting` on certificates, while later joins for `jasperoid`, `fava`, `gravetusk`, and `barbos` all completed; final `sunbeam_cluster_list.txt`, `kubectl_get_node.txt`, `juju_status_openstack-machines.txt`, and `juju_status_openstack.txt` showed full cluster convergence.
>>>>>>> Stashed changes

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

<<<<<<< Updated upstream
=======
### Pattern 10: Charmhub docker-registry token API 503 causes bootstrap-time ImagePullBackOff across multiple charms

**Applies to:** `sunbeam_deploy` step, `sunbeam cluster bootstrap` phase

**Symptom (in GitHub Actions log):**
```
subprocess.CalledProcessError: Command
  ['ssh', '-t', ..., 'solqa-shared-maas-server-31.maas', '--',
   'sunbeam', 'cluster', 'bootstrap', '-m', 'manifest.yaml',
   '--topology', 'single', '--role', 'control', '--role', 'compute', '--role', 'storage']
returned non-zero exit status 1.
```

**In `generated/sunbeam/output.log`:**
```
Error configuring cloud
Traceback (most recent call last):
  File "/snap/openstack/1004/lib/python3.12/site-packages/sunbeam/steps/openstack.py", line 828, in run
    self.tfhelper.update_tfvars_and_apply_tf(
...
sunbeam.core.terraform.TerraformException: terraform command failed: /snap/openstack/1004/bin/terraform apply -input=false -auto-approve -no-color -json
stderr:
```
The terraform wrapper surfaces only a generic failure with empty stderr.

**In `generated/sunbeam/juju_status_openstack.txt`:**
```
mysql            error  unknown container reason "ImagePullBackOff" ...
rabbitmq         error  unknown container reason "ImagePullBackOff" ...
traefik          error  unknown container reason "ImagePullBackOff" ...
traefik-public   error  unknown container reason "ImagePullBackOff" ...
```

**In `generated/sunbeam/kubectl_get_pod_detailed.txt`:**
```
failed to authorize: failed to fetch oauth token: unexpected status from
POST request to https://api.charmhub.io/v1/tokens/docker-registry: 503 Service Unavailable
```
This same 503 appears for multiple unrelated charm images (`mysql-image`, `rabbitmq-image`, `traefik-image`).

**Root cause:** `sunbeam cluster bootstrap` progressed far enough to create the initial OpenStack model, but several k8s charm workloads could not pull their OCI images because Charmhub's docker-registry token service returned HTTP 503 for image-auth requests. Since the missing images belonged to core services (MySQL, RabbitMQ, Traefik), the bootstrap could not finish wiring the control plane. The runner only saw the outer `TerraformException`; the decisive evidence is in the post-failure Juju/Kubernetes snapshots.

**Key signals:**
- Exit code **1** from `sunbeam cluster bootstrap` on the bootstrap node
- `generated/sunbeam/sunbeam_cluster_list.txt` shows only the bootstrap node (`control` + `storage`) — no joins were attempted yet
- Multiple unrelated pods in the `openstack` namespace are stuck in `ImagePullBackOff`, not just one charm
- `kubectl_get_pod_detailed.txt` shows the same upstream failure for each pod: Charmhub token endpoint `https://api.charmhub.io/v1/tokens/docker-registry` returned **503 Service Unavailable**
- The failure is during image authorization, before the containers can even start; this is not a Pebble, charm hook, or SSH issue

**How to confirm:**
```bash
# Post-failure control-plane state:
grep -n 'ImagePullBackOff' generated/sunbeam/juju_status_openstack.txt generated/sunbeam/kubectl_get_pod.txt

# Definitive root cause from pod details:
grep -n 'tokens/docker-registry\|503 Service Unavailable\|failed to authorize' generated/sunbeam/kubectl_get_pod_detailed.txt

# Confirm bootstrap never advanced to node joins:
cat generated/sunbeam/sunbeam_cluster_list.txt
```

**Distinguishing from other bootstrap/configure failures:**
- vs. Traefik 502 / Neutron race: this fails during bootstrap, before configure and before any joins
- vs. Cilium hostname mismatch: that fails with explicit `No cilium pod found` stderr, not generic terraform failure
- vs. Terraform provider bug: no `glance-to-ceph` or provider consistency message appears; instead the pods cannot fetch images at all
- This pattern: multiple charm pods all fail registry auth with the same Charmhub token API **503**

**Observed in:**
- Run 26226826932 (UUID: 3140b6d1-aa24-436f-aef4-b2dd22477cdd, tor3-sqa-shared_maas dh1_j9_1, branch `aipoc`, openstack snap rev 1004 / `2024.1/beta`, 2026-05-21).

---

### Pattern 11: Snap Store assertion outage during storage-node join prevents `microceph` install and stalls model convergence

**Applies to:** `sunbeam_deploy` step, `sunbeam cluster join` phase on storage-role nodes

**Symptom (in GitHub Actions log):**
```text
subprocess.CalledProcessError: Command
  ['ssh', ..., 'claydol.maas', '--', 'sunbeam', 'cluster', 'join', ..., '--role', 'storage', '--accept-defaults']
returned non-zero exit status 1.
```

**In `generated/sunbeam/output.log`:**
```text
Error: wait timed out after 1799.9999979040003s
...
'microceph/3': UnitStatus(
  workload_status=StatusInfo(current='error', message='hook failed: "install"', since='21 May 2026 13:57:24Z'),
)
'epa-orchestrator/6': UnitStatus(
  workload_status=StatusInfo(current='error', message='hook failed: "install"', since='21 May 2026 13:57:27Z'),
)
```
The outer failure is only the 1800-second `juju wait-for` timeout inside `sunbeam cluster join`.

**In `generated/sunbeam/sosreport-<failing-node>.tar.xz`:**
- `var/log/juju/unit-microceph-3.log`
  ```text
  Failed executing cmd: ['snap', 'install', 'microceph', '--channel', 'squid/candidate'], error: error: unable to contact snap store
  subprocess.CalledProcessError: Command '['snap', 'install', 'microceph', '--channel', 'squid/candidate']' returned non-zero exit status 1.
  ```
- `var/log/syslog`
  ```text
  snapd[...] Change 15 task (Fetch and check assertions for snap "microceph" ...) failed:
  cannot fetch assertion: got unexpected HTTP status code 503 via GET to
  https://api.snapcraft.io/v2/assertions/snap-revision/...
  ```
- `var/log/juju/unit-epa-orchestrator-6.log`
  ```text
  urllib.error.HTTPError: HTTP Error 500: Internal Server Error
  ...
  charms.operator_libs_linux.v2.snap.SnapNotFoundError: Snap 'epa-orchestrator' not found!
  ```

**Root cause:** The join itself reached the stage where Juju started installing the storage-node charms on `claydol.maas`, but the node could not fetch snap assertions from the Snap Store. `microceph/3` repeatedly failed its install hook because `snap install microceph --channel squid/candidate` could not contact the store, and the subordinate `epa-orchestrator/6` failed for the same underlying reason while querying snap metadata through snapd. Because those installs never completed, the `openstack-machines` model never converged, so the join's internal `juju wait-for` hit its 1800-second deadline and returned exit code 1.

**Key signals:**
- The failing node is **not** a false-positive join timeout: `generated/sunbeam/sunbeam_cluster_list.txt` shows `claydol` with `Storage = error`, not `active`
- `microceph/3` install hook fails on the node that was joining with the `storage` role
- Node-local `snapd` syslog shows a definitive upstream failure: Snap Store assertions API returned **503**
- `epa-orchestrator/6` errors (`HTTP Error 500`, `SnapNotFoundError`) are secondary symptoms of the same snapd/store outage on that node

**How to confirm:**
```bash
# Outer symptom from the join wrapper:
grep -n 'wait timed out after 1799\|microceph/3\|epa-orchestrator/6' generated/sunbeam/output.log

# Definitive node-local cause:
tar -xOf generated/sunbeam/sosreport-<failing-node>.tar.xz \
  sosreport-<failing-node>*/var/log/juju/unit-microceph-3.log \
  | grep -n 'unable to contact snap store\|snap install'

tar -xOf generated/sunbeam/sosreport-<failing-node>.tar.xz \
  sosreport-<failing-node>*/var/log/syslog \
  | grep -n 'Fetch and check assertions for snap "microceph"\|api.snapcraft.io'
```

**Distinguishing from other join timeouts:**
- vs. the parallel-join false-negative patterns: those runs show the timed-out node eventually `active` in `sunbeam_cluster_list.txt`; here `claydol` stays `storage error`
- vs. Terraform state-lock / k8s-node-update failures: no terraform lock message or `Failed to get k8s nodes to update` appears
- This pattern is a genuine external dependency outage on the joining node: snap installation cannot complete because Snap Store assertion fetches return **503**

**Observed in:**
- Run 26220369033 (UUID: 0f7a43e5-db7a-4968-b7a4-80c97024b3f8, tor3-sqa-testflinger cluster_1, branch `main`, `claydol.maas` storage join, openstack snap rev 1004 / `2024.1/beta`, 2026-05-21).

---

### Pattern 12: Silent bootstrap over SSH hits transport timeout on testflinger and surfaces as false failure

**Applies to:** `sunbeam_deploy` step, `sunbeam cluster bootstrap` phase on `tor3-sqa-testflinger`

**Symptom (in GitHub Actions log):**
```text
subprocess.CalledProcessError: Command
  ['ssh', '-t', ..., 'bronzor.maas', '--', 'sunbeam', 'cluster', 'bootstrap', '-m', 'manifest.yaml',
   '--topology', 'single', '--role', 'control', '--role', 'compute', '--role', 'storage']
returned non-zero exit status 255.
```

**In `generated/sunbeam/output.log`:**
```text
2026-05-22 12:07:56,082 - DEBUG - [localhost]: ssh -t ... bronzor.maas -- sunbeam cluster bootstrap ...
...
2026-05-22 12:40:58,868 - ERROR - [localhost] Command failed: ssh -t ... bronzor.maas -- sunbeam cluster bootstrap ...
2026-05-22 12:40:58,868 - ERROR - 1[localhost] STDOUT follows:
b''
2026-05-22 12:40:58,868 - ERROR - 2[localhost] STDERR follows:
Pseudo-terminal will not be allocated because stdin is not a terminal.
Warning: Permanently added 'bronzor.maas' (ED25519) to the list of known hosts.
Timeout, server bronzor.maas not responding.
```
The SSH session produced no remote output for **33m02s** before it was dropped.

**Post-failure state proves bootstrap succeeded:**
- `generated/sunbeam/sunbeam_cluster_list.txt`
  ```text
  openstack-machines
  bronzor  running  compute=active  control=active  storage=active
  ```
- `generated/sunbeam/juju_status_openstack.txt`: all core OpenStack apps (`mysql`, `rabbitmq`, `keystone`, `nova`, `neutron`, `traefik*`, etc.) are `active/idle`
- `generated/sunbeam/kubectl_get_pod.txt`: all pods are `Running`
- `generated/sunbeam/juju_debug_log_openstack-machines.txt`
  ```text
  12:40:01 unit-openstack-hypervisor-0 ... Completed guarded section fully: 'Bootstrapping'
  ```
  This last bootstrap-critical hook completed **57 seconds before** the SSH timeout surfaced on the runner.

**Root cause:** `deploy_sunbeam.py` runs `sunbeam cluster bootstrap` over an interactive SSH session (`ssh -t`) and waits for the command to return. On this `tor3-sqa-testflinger` run, the bootstrap phase was largely silent from the SSH client's perspective, so the transport timed out after ~33 minutes with `Timeout, server <node> not responding.` even though the remote bootstrap had already converged successfully. The pipeline treated the SSH disconnect (`exit status 255`) as a hard failure and never advanced to the join phase.

**Key signals:**
- Exit code **255** with `Timeout, server <node> not responding.` — transport failure, not an application stack trace
- `STDOUT follows: b''` — the runner saw no bootstrap progress output before the disconnect
- Post-failure artifacts show a healthy single-node Sunbeam deployment: cluster list `running`, Juju apps `active`, Kubernetes pods `Running`
- `juju_debug_log_openstack-machines.txt` shows the last bootstrap-critical hook finishing just before the disconnect, with no corresponding Juju/charm failure

**How to confirm:**
```bash
# Transport-level symptom and exact timing gap:
grep -n 'cluster bootstrap\|Timeout, server .* not responding' generated/sunbeam/output.log

# Prove the single-node bootstrap actually converged:
cat generated/sunbeam/sunbeam_cluster_list.txt
cat generated/sunbeam/juju_status_openstack.txt
cat generated/sunbeam/kubectl_get_pod.txt

# Correlate final bootstrap hook completion to just before disconnect:
grep -n "Completed guarded section fully: 'Bootstrapping'" generated/sunbeam/juju_debug_log_openstack-machines.txt
```

**Distinguishing from other bootstrap failures:**
- vs. Charmhub token-service outage: there are no `ImagePullBackOff` pods or Charmhub `503 Service Unavailable` errors
- vs. Cilium hostname mismatch / terraform bootstrap bugs: there is no `No cilium pod found`, Terraform provider inconsistency, or application-level traceback from the remote bootstrap itself
- vs. SSH `Broken pipe` patterns: this substrate surfaced `Timeout, server <node> not responding.` instead of `client_loop: send disconnect: Broken pipe`, but both are transport-level disconnects rather than remote bootstrap failures

**Observed in:**
- Run 26285062285 (UUID: 4cd6f7f0-434b-442e-8ddf-9ce278b1b73e, tor3-sqa-testflinger cluster_1, branch `main`, `bronzor.maas` bootstrap node, openstack snap `2024.1/beta`, 2026-05-22).

---

### Pattern 14: Runner-side DNS / SSH reachability loss during `sunbeam cluster bootstrap`

**Applies to:** `sunbeam_deploy` step, `sunbeam cluster bootstrap` phase on `tor3-sqa-shared_maas`

**Symptom (in GitHub Actions log):**
```text
subprocess.CalledProcessError: Command
  ['ssh', '-t', '-o', 'ServerAliveInterval=60', '-o', 'ServerAliveCountMax=10', ...,
   'solqa-shared-maas-server-10.maas', '--', 'sunbeam', 'cluster', 'bootstrap', ...]
returned non-zero exit status 255.

STDERR:
Pseudo-terminal will not be allocated because stdin is not a terminal.
Warning: Permanently added 'solqa-shared-maas-server-10.maas' (ED25519) to the list of known hosts.
Timeout, server solqa-shared-maas-server-10.maas not responding.
```

**Follow-on evidence in the same run:**
```text
ssh: Could not resolve hostname solqa-shared-maas-server-10.maas: Temporary failure in name resolution
ssh: Could not resolve hostname solqa-shared-maas-server-11.maas: Temporary failure in name resolution
...
ssh: Could not resolve hostname solqa-shared-maas-server-19.maas: Temporary failure in name resolution
```
These resolver failures begin immediately during post-failure log collection and affect **all** `.maas` hosts.

**Root cause:** The failure is transport-side, not a Sunbeam application traceback. `deploy_sunbeam.py` reaches the `bootstrap_sunbeam()` SSH wrapper and then sits silent for ~38 minutes until the SSH client returns exit code 255 with `Timeout, server ... not responding.`. Within ~45 seconds, every follow-up SSH/SCP to all shared-MAAS hosts fails with `Temporary failure in name resolution`, showing that the runner lost DNS resolution / reachability to the `.maas` hosts. Because the same outage also blocks sosreport and status collection, there is no direct post-failure cluster snapshot proving whether bootstrap itself later converged; the diagnosable root cause is the runner-side connectivity/resolver failure.

**Key signals:**
- Exit code **255** from the SSH wrapper in `products/sqa_common/helpers.py` — transport failure, not a remote Python/Sunbeam exception
- `output.log` shows the bootstrap command started at 13:09:29 and produced no remote output before timing out at 13:47:35
- No `sunbeam_cluster_list.txt`, `juju_status_openstack*.txt`, or `kubectl_get_pod*.txt` artifacts were collected afterward
- Post-failure collection immediately fails on every host with `Temporary failure in name resolution`, proving the runner could no longer resolve shared-MAAS hostnames

**How to confirm:**
```bash
# Transport timeout during bootstrap:
grep -n 'cluster bootstrap\|Timeout, server .* not responding' generated/sunbeam/output.log

# Immediate runner-side resolver failure across all hosts:
grep -n 'Temporary failure in name resolution' generated/github-runner/run.log

# Show that post-failure status artifacts were never collected:
find generated/sunbeam -maxdepth 1 \( -name 'sunbeam_cluster_list.txt' -o -name 'juju_status_openstack*.txt' -o -name 'kubectl_get_pod*.txt' \)
```

**Distinguishing from other bootstrap failures:**
- vs. bootstrap false-failure with healthy snapshots: here the decisive post-failure cluster artifacts are **missing**, because log collection itself was blocked by the resolver outage
- vs. Charmhub / Terraform / Cilium bootstrap bugs: there is no application-level stderr from `sunbeam cluster bootstrap` (no `TerraformException`, no `ImagePullBackOff`, no `No cilium pod found`)
- vs. long-idle SSH disconnects (`Broken pipe`): this signature is `Timeout, server ... not responding.` followed by multi-host DNS resolution failures on the runner

**Observed in:**
- Run 26288126836 (UUID: d5fa73a7-9822-4d9b-847e-373f59ea34da, tor3-sqa-shared_maas dh1_j8_2, branch `main`, bootstrap node `solqa-shared-maas-server-10.maas`, openstack snap `2024.1/beta`, 2026-05-22).

---

### Pattern 15: Bootstrap hits 3600s wait timeout after transient Keystone/Placement 502 leaves `nova` stuck in Bootstrapping

**Applies to:** `sunbeam_deploy` step, `sunbeam cluster bootstrap` phase on `tor3-sqa-shared_maas`

**Symptom (in GitHub Actions log):**
```text
subprocess.CalledProcessError: Command
  ['ssh', '-t', ..., 'solqa-shared-maas-server-10.maas', '--', 'sunbeam', 'cluster',
   'bootstrap', '-m', 'manifest.yaml', '--topology', 'single', '--role', 'control',
   '--role', 'compute', '--role', 'storage']
returned non-zero exit status 1.
```

**In `generated/sunbeam/output.log`:**
```text
wait timed out after 3599.9999992559997s
...
'cinder-volume-mysql-router': AppStatus(
  app_status=StatusInfo(current='blocked', message='Missing relation: database', since='21 May 2026 01:31:16Z'),
)
...
'nova/0': UnitStatus(
  workload_status=StatusInfo(current='waiting', message='(workload) Container service not ready', since='21 May 2026 01:40:24Z'),
)
```
The outer failure is the 3600-second wait inside `sunbeam cluster bootstrap`.

**In `generated/sunbeam/pods_openstack_logs.tgz`:**
- `logs-openstack-nova-0.txt`
  ```text
  01:40:23 nova-conductor ERROR Failed to initialize placement client (is keystone available?):
            keystoneauth1.exceptions.http.BadGateway: Bad Gateway (HTTP 502)
  01:40:24 Service "nova-conductor" stopped unexpectedly with code 1
  01:40:24 Service "nova-conductor" ... restart
  ```
  After that restart, the pod later serves healthy `wsgi-nova-api` `GET /` checks with HTTP 200, but the charm never logs a later `Completed guarded section fully: 'Bootstrapping'`; only `update-status` keeps firing.
- `logs-openstack-placement-0.txt`
  ```text
  01:39:42 Completed guarded section fully: 'Bootstrapping'
  ```
  So Placement itself finished bootstrapping before Nova hit the 502.
- `logs-openstack-cinder-volume-mysql-router-0.txt`
  ```text
  01:31:35 ... database created ...
  01:31:39 ... Enabled MySQL Router service
  02:52:26 ran "update-status" hook
  ```
  The unit had its backend-database relation and kept running update-status without fresh errors, despite Juju still reporting the app blocked with `Missing relation: database`.

**Root cause:** `sunbeam cluster bootstrap` created the single-node OpenStack model, but the model never reached all-active state before the hard 3600-second deadline. The decisive blocker was `nova-k8s`: during bootstrap, `nova-conductor` made an early Placement/Keystone API call and got HTTP 502, causing Pebble to restart the service. Even though Nova services later came up, the charm stayed stuck in the Bootstrapping section (`Container service not ready`) and never emitted a completion transition. At the same time, `cinder-volume-mysql-router` showed a stale blocked app status even though its unit log proves the backend database relation was present and the router service was running. Together those non-converged statuses caused the bootstrap wait to expire.

**Key signals:**
- Exit code **1** with `wait timed out after 3599.999...s` — this is a real bootstrap timeout, not an SSH disconnect
- `generated/sunbeam/sunbeam_cluster_list.txt` shows only the bootstrap node — joins never started
- `logs-openstack-placement-0.txt` completes bootstrap before the failure, ruling out Placement being permanently down
- `logs-openstack-nova-0.txt` shows the one concrete application failure: `nova-conductor` dies on a transient HTTP 502 and never drives the charm to a completed bootstrap state afterward
- `logs-openstack-cinder-volume-mysql-router-0.txt` shows relation/service activity inconsistent with the final Juju app status, indicating a stale status contribution rather than a fresh runtime crash

**How to confirm:**
```bash
# Timeout symptom and non-converged apps:
grep -n 'wait timed out after 3599\|cinder-volume-mysql-router\|Container service not ready' generated/sunbeam/output.log

# Decisive Nova failure:
tar -xOf generated/sunbeam/pods_openstack_logs.tgz generated/sunbeam/logs-openstack-nova-0.txt \
  | grep -n 'Failed to initialize placement client\|Bad Gateway\|stopped unexpectedly\|Starting conductor node'

# Show Placement had already completed:
tar -xOf generated/sunbeam/pods_openstack_logs.tgz generated/sunbeam/logs-openstack-placement-0.txt \
  | grep -n "Completed guarded section fully: 'Bootstrapping'"
```

**Distinguishing from other bootstrap failures:**
- vs. Charmhub token-service outage: there are no `ImagePullBackOff` pod failures or Charmhub `503` token errors
- vs. SSH transport timeout: exit code is **1** with a model-status dump, not exit 255 / transport stderr
- vs. Terraform provider inconsistency: there is no `glance-to-ceph` provider error; the remote bootstrap simply waits for model convergence and times out

**Observed in:**
- Run 26197342522 (UUID: 29af6577-4ec1-45f6-b2d2-f976dd214a67, tor3-sqa-shared_maas dh1_j8_2, branch `main`, bootstrap node `solqa-shared-maas-server-10.maas`, openstack snap `2024.1/beta`, 2026-05-21).

---

### Pattern 16: Bootstrap-node `snap install openstack` fails immediately with Snap Store HTTP 408

**Applies to:** `sunbeam_deploy` step, very start of `install_snap_and_prepare_nodes()` on the bootstrap node

**Symptom (in GitHub Actions log):**
```text
subprocess.CalledProcessError: Command ['ssh', '-o', 'ServerAliveInterval=60',
 '-o', 'ServerAliveCountMax=10', ... 'solqa-shared-maas-server-31.maas', '--',
 'set', '-ex', ';', 'sudo', 'snap', 'install', 'openstack', '--channel',
 '2024.1/beta', ';', 'sunbeam', 'prepare-node-script', '--bootstrap', '|',
 'bash', '-x']' returned non-zero exit status 1.
```

**In `generated/sunbeam/output.log`:**
```text
2026-05-26 17:34:36,028 - DEBUG - [localhost]: ssh ... solqa-shared-maas-server-31.maas -- set -ex ; sudo snap install openstack --channel 2024.1/beta ; sunbeam prepare-node-script --bootstrap | bash -x
2026-05-26 17:34:41,870 - ERROR - [localhost] Command failed: ssh ...
...
+ sudo snap install openstack --channel 2024.1/beta
error: cannot install "openstack": cannot query the store for
       updates: got unexpected HTTP status code 408 via POST to
       "https://api.snapcraft.io/v2/snaps/refresh"
```

**Root cause:** `deploy_sunbeam.py` always starts by SSHing to the bootstrap node and running `sudo snap install openstack --channel <channel>` before any cluster bootstrap or join work begins. In this failure, snapd could reach the install path locally but the store query backing the install returned HTTP **408** from `api.snapcraft.io/v2/snaps/refresh`. `products/sqa_common/helpers.py` treats any non-zero remote exit as fatal and immediately raises `CalledProcessError`, so the deployment aborts before `sunbeam prepare-node-script` or `sunbeam cluster bootstrap` can run.

**Key signals:**
- Failure happens **~11 seconds after the `sunbeam_deploy` step starts** — much earlier than bootstrap/join/configure failures
- The failing command is the very first bootstrap-node remote command in `deploy_sunbeam.py:136 → install_snap_and_prepare_nodes()`
- STDERR contains a direct Snap Store transport/protocol error (`unexpected HTTP status code 408`) rather than a Sunbeam/Juju/Terraform error
- `generated/sunbeam/` contains `output.log`, manifest, netplan files, and sosreports, but **no** `sunbeam_cluster_list.txt`, `juju_status_openstack*.txt`, or `juju_debug_log_*.txt` artifacts — confirming the deployment never progressed far enough to create a cluster
- Any later `juju models --format yaml` failure during log collection is a **cascade**: no Juju controller existed because bootstrap never started

**How to confirm:**
```bash
# Primary failure in the Sunbeam layer log:
grep -n 'snap install openstack\|unexpected HTTP status code 408\|api.snapcraft.io/v2/snaps/refresh' generated/sunbeam/output.log

# Matching GitHub Actions traceback / call chain:
grep -n 'deploy_sunbeam.py\|install_snap_and_prepare_nodes\|CalledProcessError' generated/github-runner/run.log

# Confirm no cluster-status artifacts were ever produced:
find generated/sunbeam -maxdepth 1 -type f | grep -E 'sunbeam_cluster_list|juju_status|juju_debug' || true
```

**Distinguishing from other Snap Store failures:**
- vs. join-time `microceph` assertion outage: this happens on the **bootstrap node before bootstrap starts**, not during a later join on a peer node
- vs. generic SSH failure: SSH itself works; the remote shell runs and snapd returns a concrete store-side error, so exit code is **1** not **255**
- vs. Sunbeam/Terraform/bootstrap bugs: none of those components have started yet; the failure is entirely at snap installation time

**Observed in:**
- Run 26462983826 (UUID: ce4bafb6-62cf-42d1-a3ea-56ecf9b59752, tor3-sqa-shared_maas dh1_j9_1, branch `main`, bootstrap node `solqa-shared-maas-server-31.maas`, openstack channel `2024.1/beta`, 2026-05-26).

---

### Pattern 17: Juju controller charm download times out during bootstrap-node prepare phase

**Applies to:** `sunbeam_deploy` step, first `install_snap_and_prepare_nodes()` call on the bootstrap node while `sunbeam prepare-node-script --bootstrap` bootstraps Juju onto LXD

**Symptom (in GitHub Actions log):**
```text
subprocess.CalledProcessError: Command ['ssh', '-o', 'ServerAliveInterval=60',
 '-o', 'ServerAliveCountMax=10', ... 'solqa-shared-maas-server-31.maas', '--',
 'set', '-ex', ';', 'sudo', 'snap', 'install', 'openstack', '--channel',
 '2024.1/beta', ';', 'sunbeam', 'prepare-node-script', '--bootstrap', '|',
 'bash', '-x']' returned non-zero exit status 1.
```

**In `generated/sunbeam/output.log`:**
```text
openstack (2024.1/beta) 2024.1 from Canonical** installed
juju (3.6/stable) 3.6.23 from Canonical** installed
Bootstrapping LXD
Bootstrapping Juju onto LXD
...
ERROR cannot deploy controller application: deploying charmhub controller charm:
  downloading charm "juju-controller" ... cannot get archive:
  Get "https://canonical-bos01.cdn.snapcraftcontent.com/...":
  net/http: TLS handshake timeout
ERROR failed to bootstrap model: subprocess encountered error code 1
```

**Root cause:** The bootstrap node completed the initial `openstack` snap install and entered `sunbeam prepare-node-script --bootstrap`, which installs Juju, bootstraps a local LXD controller, and then downloads the `juju-controller` charm from Charmhub. In this run, the bootstrap host got past an earlier direct `curl -s -m 10 -x '' api.charmhub.io` connectivity check, but the later artifact fetch from Charmhub's CDN (`canonical-bos01.cdn.snapcraftcontent.com`) stalled during the TLS handshake. Juju therefore could not deploy its controller application, and `deploy_sunbeam.py` aborted before `sunbeam cluster bootstrap` ever ran.

**Key signals:**
- Failure happens on the **bootstrap node before any `sunbeam cluster bootstrap` / `cluster join` work**
- `openstack` and `juju` snaps install successfully; the fault is later, during Juju controller charm retrieval
- `generated/sunbeam/output.log` contains the concrete Juju bootstrap error (`cannot deploy controller application` + `TLS handshake timeout`)
- `deploy_sunbeam.py:136 -> install_snap_and_prepare_nodes()` is the failing call site, as shown in the GitHub traceback
- `generated/sunbeam/` has no `sunbeam_cluster_list.txt` or `juju_status_openstack*.txt` snapshots — the cluster never existed
- Later `juju models --format yaml` / `No controllers registered` messages are **post-failure collection cascades**

**How to confirm:**
```bash
# Primary failure and call chain:
grep -n 'cannot deploy controller application\|juju-controller\|TLS handshake timeout\|failed to bootstrap model' generated/sunbeam/output.log

grep -n 'deploy_sunbeam.py\|install_snap_and_prepare_nodes\|CalledProcessError' generated/github-runner/run.log

# Show bootstrap never reached cluster creation:
find generated/sunbeam -maxdepth 1 -type f | grep -E 'sunbeam_cluster_list|juju_status_openstack|juju_debug_log_openstack' || true
```

**Distinguishing from related patterns:**
- vs. Pattern 16 (Snap Store HTTP 408): the `openstack` snap install succeeds here; the failure is later while Juju downloads the `juju-controller` charm
- vs. bootstrap-time Charmhub token-service outage: there are no `ImagePullBackOff` pod failures because Kubernetes/OpenStack never started
- vs. generic SSH failure: SSH stays up and returns a concrete remote error; exit code is **1**, not **255**

**Observed in:**
- Run 26457590888 (UUID: d489a2f8-1786-4909-888f-3ad1ddcc16e3, tor3-sqa-shared_maas dh1_j9_1, branch `main`, bootstrap node `solqa-shared-maas-server-31.maas`, openstack channel `2024.1/beta`, 2026-05-26).

---

### Pattern 17: Parallel `sunbeam cluster join` false negative on `shared_maas` — 1800s wait expires while later joins and storage/certificate hooks are still converging

**Applies to:** `sunbeam_deploy` step, `sunbeam cluster join` phase, multi-node join on `tor3-sqa-shared_maas`

**Symptom (in GitHub Actions log):**
```text
subprocess.CalledProcessError: Command
  ['ssh', ..., 'solqa-shared-maas-server-32.maas', '--', 'sunbeam', 'cluster', 'join', '<token>',
   '--role', 'control', '--role', 'compute', '--accept-defaults']
returned non-zero exit status 1.
```

**In `generated/sunbeam/output.log`:**
```text
2026-05-26 08:05:07,331 - ERROR - [localhost] Command failed: ssh ... solqa-shared-maas-server-32.maas -- sunbeam cluster join ... --role control --role compute --accept-defaults
wait timed out after 1799.9999993759993s
...
2026-05-26 08:05:41,834 - ERROR - [localhost] Command failed: ssh ... solqa-shared-maas-server-35.maas -- sunbeam cluster join ... --role compute --role storage --accept-defaults
wait timed out after 1799.9999994890004s
...
2026-05-26 08:17:37,180 - ERROR - [localhost] Command failed: ssh ... solqa-shared-maas-server-43.maas -- sunbeam cluster join ... --role compute --accept-defaults
wait timed out after 1799.9999992880003s
```

The timeout snapshots already showed the cluster still progressing:
- the first timed-out snapshot already contained `k8s/3` on machine `4` and `openstack-hypervisor/3` on machine `5`
- the last timed-out snapshot showed transient convergence, not a durable failure:
  - `cinder-volume/3` workload `waiting: (workload) Waiting for backends`
  - subordinate `cinder-volume-ceph/3` still `executing` `ceph-relation-joined`
  - `openstack-hypervisor/0` still `executing` `certificates-relation-changed`
- after the first timeout had already fired, another queued join still completed:
  - `2026-05-26 08:19:56,233 - INFO - [localhost]: Node joined cluster with roles: storage`

**Final state proves the step failure was a false negative:**
- `generated/sunbeam/juju_status_openstack-machines.txt` at `13:21:19Z` shows all machines `0..6` `started`
- all `cinder-volume/*`, `openstack-hypervisor/*`, `k8s/*`, and `sunbeam-machine/*` units are `active idle`
- `generated/sunbeam/sunbeam_cluster_list.txt` shows all seven `solqa-shared-maas-server-*` nodes `running` with their requested roles active

**Root cause:** `deploy_sunbeam.py` dispatches joins concurrently with `ThreadPoolExecutor(max_workers=3)` and then calls `future.result()` in node order. Each remote `sunbeam cluster join` carries its own internal `juju wait-for model openstack-machines` deadline of ~1800 seconds. On this run, several joins overlapped while the `openstack-machines` model was still converging storage and certificate relations from earlier admissions. The per-join wait hit its 1800-second deadline on servers 32, 35, and 43 even though the model kept progressing and a later queued node still joined successfully. Once the first stored timeout exception was re-raised through `future.result()`, `deploy_sunbeam.py` aborted the whole layer even though the cluster eventually converged.

**How to confirm:**
```bash
# Timeouts on multiple nodes:
grep -n 'solqa-shared-maas-server-32\|solqa-shared-maas-server-35\|solqa-shared-maas-server-43\|wait timed out after 1799' generated/sunbeam/output.log

# A queued join still finished after those timeouts:
grep -n 'Node joined cluster with roles: storage' generated/sunbeam/output.log

# Final state is healthy despite the exit code:
cat generated/sunbeam/sunbeam_cluster_list.txt
cat generated/sunbeam/juju_status_openstack-machines.txt
```

**Distinguishing from related patterns:**
- vs. the `Failed to get k8s nodes to update` false-negative pattern: this run never raises the K8S node-matching exception; stderr is only `wait timed out after 1799...`
- vs. the `k8s` / Unready Pods variant: the timeout snapshots here are dominated by storage/backend and certificate convergence, not `k8s` workload `waiting: Unready Pods`
- vs. the Snap Store assertion outage on storage join: final cluster state is fully healthy here; no unit remains in `error`

**Observed in:**
- Run 26435144943 (UUID: f14615e7-bfef-40ce-97df-7cb515cdb7ad, tor3-sqa-shared_maas dh1_j9_1, branch `main`, servers `32`/`35`/`43` timed out, 2026-05-26).

---

### Pattern 18: Bootstrap hits 3600s wait timeout — `ovn-central/0` permanently blocked after `check_pebble_handlers_ready()` Pebble socket timeout

**Applies to:** `sunbeam_deploy` step, `sunbeam cluster bootstrap` phase on `tor3-sqa-testflinger`

**Symptom (in GitHub Actions log):**
```text
subprocess.CalledProcessError: Command
  ['ssh', '-t', ..., 'gardener.maas', '--', 'sunbeam', 'cluster', 'bootstrap',
   '-m', 'manifest.yaml', '--topology', 'single',
   '--role', 'control', '--role', 'compute', '--role', 'storage']
returned non-zero exit status 1.

STDERR:
wait timed out after 3599.999999006s
```

**In `generated/sunbeam/juju_debug_log_openstack.txt`:**
```
unit-ovn-central-0: 10:29:23 ERROR unit.ovn-central/0.juju-log ovsdb-cms:130:
  Exception raised in section 'Bootstrapping': timed out
  ...
  File "ops_sunbeam/charm.py", line 726, in check_pebble_handlers_ready
    if not ph.service_ready:
  File "ops_sunbeam/container_handlers.py", line 269, in pebble_ready
    return self.charm.unit.get_container(self.container_name).can_connect()
  File "ops/model.py", line 2580, in can_connect
    self._pebble.get_system_info()
  File "ops/pebble.py", line 2221, in _request_raw
    response = self.opener.open(request, timeout=self.timeout)
    ← timed out
```

**Timeline:**
- Pebble-ready hooks for all 3 OVN containers (`ovn-nb-db-server`, `ovn-northd`, `ovn-sb-db-server`) fired successfully ~4 minutes earlier
- Multiple successful `Completed guarded section fully: 'Bootstrapping'` entries for `ovn-central` after pebble-ready
- At `ovsdb-cms-relation-changed`, `configure_app_leader` re-checks Pebble via `check_pebble_handlers_ready()` — the HTTP GET to `/v1/system-info` times out after 17 seconds
- After the error, `ovn-central/0` only runs `update-status` hooks and never re-enters `Bootstrapping`
- `cinder-volume` in `openstack-machines` enters "(amqp) integration missing" late (~1h07 in) as a cascade

**Root cause:** `configure_app_leader` in `ovn-central-k8s` calls `check_pebble_handlers_ready()` on every `ovsdb-cms-relation-changed` event. This calls `container.can_connect()` on each sidecar container, which issues an HTTP GET to the Pebble Unix socket. On a resource-constrained single testflinger node running the full Sunbeam stack, the Pebble daemon can be temporarily unresponsive during OVN OVSDB service initialization. When the socket times out, `ops_sunbeam`'s guard section catches the exception and sets the unit permanently blocked — there is no retry.

**Key signals:**
- Exit code **1** with `wait timed out after 3599.999...s` — real bootstrap timeout, not SSH disconnect
- `juju_debug_log_openstack.txt`: multiple earlier `Completed guarded section fully: 'Bootstrapping'` for `ovn-central` (Pebble was responsive), then a single error entry with the socket timeout, then only `update-status` hooks
- `kubectl_get_pod.txt`: `ovn-central-0: 4/4 Running, 0 restarts` — pod is healthy; no container crash
- `sunbeam_cluster_list.txt`: only the bootstrap node present — joins never started

**How to confirm:**
```bash
WORK=<work_dir>/<uuid>/generated/sunbeam

# Confirm bootstrap timeout
grep "wait timed out after 3599" $WORK/output.log

# Find the Pebble socket timeout in the Juju debug log
grep -A40 "ERROR unit.ovn-central.*timed out" $WORK/juju_debug_log_openstack.txt | head -45

# Confirm pod health (should show 0 restarts)
grep "ovn-central" $WORK/kubectl_get_pod.txt

# Confirm prior bootstrapping successes (proving it's not a persistent issue)
grep "ovn-central.*Completed guarded section\|ovn-central.*ERROR" $WORK/juju_debug_log_openstack.txt
```

**Distinguishing from other bootstrap failures:**
- vs. Pattern 15 (Nova HTTP 502): here the blocker is `ovn-central`, not `nova`; the error is a Pebble socket timeout, not a Nova service crash
- vs. SSH transport timeout (Pattern 12/14): exit code is **1** with a model-status dump, not exit 255
- vs. Snap Store outage: there are no `snap install` failures or HTTP 408/503 from `snapcraft.io`

**Observed in:**
- Run 26680401911 (UUID: 23f5cdb9-da4a-41c1-9fe4-b5ce5e049733, tor3-sqa-testflinger cluster_1, branch `main`, bootstrap node `gardener.maas`, openstack snap `2024.1/beta`, 2026-05-30).

---

### Pattern 19: `sunbeam configure` hits Neutron EOF via Traefik while ingress providers are still not ready

**Applies to:** `sunbeam_deploy` step, `sunbeam configure` phase, `tor3-sqa-testflinger`

**Symptom (in GitHub Actions log):**
```text
subprocess.CalledProcessError: Command
  ['ssh', ..., 'fleetroc.maas', '--', 'sunbeam', 'configure', '-m', 'manifest.yaml']
returned non-zero exit status 1.
```

**In `generated/sunbeam/output.log`:**
```text
02:34:28  sunbeam cluster list
02:34:37  sunbeam configure -m manifest.yaml
02:36:15  Error configuring cloud
          sunbeam.core.terraform.TerraformException: terraform command failed: ... terraform apply ...
```
The wrapper stderr is empty in the runner log, so the decisive evidence comes from the bootstrap node's sosreport terraform log and the post-failure Juju snapshot.

**Supporting evidence:**
- `generated/sunbeam/sosreport-fleetroc-*.tar.xz` → `home/ubuntu/snap/openstack/common/etc/modern-slug/demo-setup/terraform-apply-*.log`:
  ```text
  02:35:40  Starting apply for openstack_networking_router_interface_v2.user_router_interface
  02:35:43  Error creating openstack_networking_router_interface_v2:
             Put "http://10.241.4.104:80/openstack-neutron/v2.0/routers/<uuid>/add_router_interface":
             OpenStack connection error, retries exhausted. Aborting. Last error was: EOF
  ```
- `generated/sunbeam/juju_debug_log_openstack.txt`: Traefik units continue logging
  `Provider not ready` for **Neutron (port 9696)** during the configure window, including at
  `02:34:51`, `02:34:53`, `02:35:10`, `02:35:17`, and `02:35:29` — after `sunbeam configure`
  has already started.
- `generated/sunbeam/sunbeam_cluster_list.txt` and both Juju status snapshots show all seven
  nodes and all collected applications `active/idle` after the failure, so the cluster itself
  had converged enough to recover once ingress settled.

**Root cause:** `deploy_sunbeam.py` starts `sunbeam configure` immediately after the final
`sunbeam cluster list` succeeds, but on this run Traefik's public Neutron route was still being
reconciled. Terraform reached the `openstack_networking_router_interface_v2.user_router_interface`
step and talked to Neutron through `traefik-public` (`10.241.4.104`). That request did not get a
stable upstream response and eventually failed with `EOF`, surfacing only as a generic
`TerraformException` in the runner. This is the same premature-configure / ingress-settling race
family as the earlier 502 pattern, but here the externally visible symptom is connection abort
(`EOF`) rather than HTTP 502.

**How to confirm:**
```bash
WORK=<work_dir>/<uuid>/generated/sunbeam
SOS=$WORK/sosreport-fleetroc-*.tar.xz

# Configure starts right after the final cluster list
grep -n 'sunbeam cluster list\|sunbeam configure -m manifest.yaml' $WORK/output.log

# Extract the terraform apply log from the bootstrap node sosreport
TLOG=$(tar -tJf $SOS | grep 'demo-setup/terraform-apply' | head -1)
tar -xJOf $SOS $TLOG | grep -n 'router_interface\|EOF\|retries exhausted'

# Show Neutron ingress still not ready while configure is running
grep 'Provider not ready' $WORK/juju_debug_log_openstack.txt | grep '9696\|neutron' | tail -20
```

**Observed in:**
- Run 26665920907 (UUID: 3d5b2587-f94d-40e4-b90a-f640e3e43739, tor3-sqa-testflinger cluster_3, branch `main`, bootstrap node `fleetroc.maas`, openstack snap rev 1005 / `2024.1/beta`, 2026-05-30).

---

### Pattern 20: `sunbeam cluster resize` hits 3600s wait timeout because `nova/1` never leaves Bootstrapping after MySQL-router refusals and Placement API errors

**Applies to:** `sunbeam_deploy` step, `sunbeam cluster resize` phase on `tor3-sqa-testflinger`

**Symptom (in GitHub Actions log):**
```text
subprocess.CalledProcessError: Command
  ['ssh', '-o', 'ServerAliveInterval=60', '-o', 'ServerAliveCountMax=10',
   '-o', 'StrictHostKeyChecking=no', '-o', 'UserKnownHostsFile=/dev/null',
   'belome.maas', '--', 'sunbeam', 'cluster', 'resize']
returned non-zero exit status 1.
```

**In `generated/sunbeam/output.log`:**
```text
00:00:49  ssh ... belome.maas -- sunbeam cluster resize
01:03:03  wait timed out after 3599.999998026s
...
'nova/1': UnitStatus(
  workload_status=StatusInfo(current='waiting',
    message='(workload) Container service not ready', since='30 May 2026 00:13:20Z'),
)
```
The runner-side failure is the 3600-second wait inside `sunbeam cluster resize`.

**Supporting evidence:**
- `generated/sunbeam/sunbeam_cluster_list.txt`: all seven nodes (`belome`, `lyot`, `heatmor`, `claydol`, `kearns`, `sarabhai`, `racah`) are already present with their requested roles, so membership expansion itself succeeded.
- `generated/sunbeam/juju_status_openstack-machines.txt`: all machine-model apps are `active`, confirming the machine-side join work completed.
- `generated/sunbeam/juju_status_openstack.txt`: only `nova` remains non-converged; the app is `waiting` and `nova/1` is stuck at `(workload) Container service not ready` while the rest of the OpenStack model is `active`.
- `generated/sunbeam/pods_openstack_logs.tgz` → `logs-openstack-nova-1.txt`:
  ```text
  00:05:30 ... Can't connect to MySQL server on 'nova-api-mysql-router-service...': [Errno 111] ECONNREFUSED
  00:07:06 Service "nova-conductor" stopped unexpectedly with code 1
  00:07:10 Failed to initialize placement client (is keystone available?): ... HTTP 502
  00:08:48 Charm is waiting in section 'Bootstrapping' due to 'Container service not ready'
  00:13:02 ... Can't connect to MySQL server on 'nova-mysql-router-service...': [Errno 111] ECONNREFUSED
  00:13:27 Failed to initialize placement client (is keystone available?): ... HTTP 500
  ```
  `nova-conductor` and `nova-scheduler` keep restarting, so the charm never records another successful completion of the guarded Bootstrapping section after `00:08:44Z`.

**Root cause:** `sunbeam cluster resize` had already admitted every requested node into the cluster, but the follow-up wait for the `openstack` model never completed because the newly added `nova/1` unit could not stabilise. During its bootstrap sequence, `nova/1` repeatedly lost access to both of its required control-plane dependencies: the Nova MySQL routers returned `ECONNREFUSED`, and Placement/Keystone calls from `nova-conductor` returned HTTP 502/500. Those failures caused `nova-conductor` and `nova-scheduler` to crash-loop, after which the charm remained stuck in `Bootstrapping` with `Container service not ready`. Because `sunbeam cluster resize` waits for the full model to converge, that single non-ready Nova unit exhausted the hard 3600-second timeout and surfaced as the fatal exit code 1.

**How to confirm:**
```bash
WORK=<work_dir>/<uuid>/generated/sunbeam

# Confirm the resize timeout and the waiting nova unit
grep -n 'cluster resize\|wait timed out after 3599\|nova/1\|Container service not ready' $WORK/output.log

# Show that membership/machine joins already succeeded
cat $WORK/sunbeam_cluster_list.txt
cat $WORK/juju_status_openstack-machines.txt

# Extract the decisive nova-1 crashes
for f in logs-openstack-nova-1.txt; do
  tar -xOf $WORK/pods_openstack_logs.tgz generated/sunbeam/$f \
    | grep -n 'ECONNREFUSED\|Failed to initialize placement client\|stopped unexpectedly\|Container service not ready'
done
```

**Distinguishing from related patterns:**
- vs. the parallel-join false-negative family: final `juju_status_openstack.txt` is not healthy here — `nova/1` is still waiting, so this is a real convergence failure, not a post-timeout recovery.
- vs. Pattern 15 (bootstrap-time Nova 502): this happens during `cluster resize`, not the initial single-node bootstrap, and the decisive additional signal is persistent MySQL-router `ECONNREFUSED` on the newly added Nova unit.
- vs. Pattern 19 (premature `sunbeam configure`): `sunbeam configure` never starts; the failure happens entirely inside `sunbeam cluster resize`.

**Observed in:**
- Run 26662396210 (UUID: c9f97030-6c7a-4272-81b3-685ab5acad99, tor3-sqa-testflinger cluster_1, branch `main`, bootstrap node `belome.maas`, openstack snap `2024.1/beta`, 2026-05-30).

---

### Pattern 21: Earlier compute-node `openstack-hypervisor/3` ceph-access hook times out querying snapd, and a later storage-node join inherits the model timeout

**Applies to:** `sunbeam_deploy` step, `sunbeam cluster join` phase on `tor3-sqa-testflinger`

**Symptom (in GitHub Actions log):**
```text
subprocess.CalledProcessError: Command
  ['ssh', ..., 'ledian.maas', '--', 'sunbeam', 'cluster', 'join', '<token>', '--role', 'storage', '--accept-defaults']
returned non-zero exit status 1.
```

**In `generated/sunbeam/output.log`:**
```text
Error: wait timed out after 1799.9999978239994s
...
'microceph/3': UnitStatus(workload_status=StatusInfo(current='active', since='29 May 2026 19:33:19Z'))
'cinder-volume/3': UnitStatus(workload_status=StatusInfo(current='active', since='29 May 2026 19:40:36Z'))
'openstack-hypervisor/3': UnitStatus(
  workload_status=StatusInfo(current='blocked', message='(workload) Error in charm (see logs): timed out', since='29 May 2026 19:36:36Z'),
  machine='5', public_address='10.241.2.36'
)
```
The outer failure surfaces on `ledian.maas`, but the timeout snapshot already shows `ledian` itself joined (`sunbeam_cluster_list.txt`: `Storage = active`).

**In `generated/sunbeam/sosreport-claydol-*.tar.xz`:**
- `var/log/juju/unit-openstack-hypervisor-3.log`
  ```text
  ceph-access:17: Exception raised in section 'Bootstrapping': timed out
  ...
  File ".../src/charm.py", line 808, in ensure_snap_present
    if not self._is_microovn_present():
  ...
  File ".../snap.py", line 983, in get_snap_information
    return self._request("GET", "find", {"name": name})[0]
  ...
  TimeoutError: timed out
  ```
- `var/log/syslog`
  ```text
  snap.openstack-hypervisor.hook.configure-...scope: Started
  apparmor="DENIED" profile="snap.openstack-hypervisor.hook.configure" name=".../libxtables.so..." comm="iptables-legacy"
  apparmor="DENIED" profile="snap.openstack-hypervisor.hook.configure" name=".../libip4tc.so..." comm="iptables-legacy"
  apparmor="DENIED" profile="snap.openstack-hypervisor.hook.configure" name="/sys/kernel/cpu_byteorder" comm="lscpu"
  ```

**Root cause:** `ledian.maas` was not the real failing node. Its storage-role join succeeded, but `sunbeam cluster join` waits for the whole `openstack-machines` model. Earlier node `claydol` had already started `openstack-hypervisor/3`; during `ceph-access-relation-changed`, the charm re-entered `Bootstrapping`, called `ensure_snap_present()`, and then hung long enough querying snapd for `microovn` metadata that the hook raised `TimeoutError`. That left `openstack-hypervisor/3` blocked at `19:36:36Z`, so the model never converged before `ledian`'s inherited 1800-second join wait expired.

**Key signals:**
- The timed-out node (`ledian`) is already `running` with `Storage = active` in `sunbeam_cluster_list.txt`
- `microceph/3` and `cinder-volume/3` on `ledian` both become `active`, so the storage join itself succeeded
- The real blocker is a different node: `claydol` `openstack-hypervisor/3` remains `blocked` with `Error in charm (see logs): timed out`
- Claydol's unit log shows the decisive traceback inside `_is_microovn_present()` / snap `find?name=microovn`
- Claydol syslog shows repeated `snap.openstack-hypervisor.hook.configure` AppArmor denials during the same configure-hook window

**How to confirm:**
```bash
WORK=<work_dir>/<uuid>/generated/sunbeam

# Outer symptom and timeout snapshot
grep -n 'ledian.maas\|wait timed out after 1799\|openstack-hypervisor/3\|cinder-volume/3\|microceph/3' $WORK/output.log
cat $WORK/sunbeam_cluster_list.txt

# Decisive claydol unit-log traceback
CLAY=$WORK/sosreport-claydol-*.tar.xz
UNIT=$(tar -tJf $CLAY | grep 'var/log/juju/unit-openstack-hypervisor-3.log$' | head -1)
tar -xJOf $CLAY "$UNIT" | grep -n 'ensure_snap_present\|_is_microovn_present\|get_snap_information\|TimeoutError'

# Correlated AppArmor denials during the same configure hook
SYS=$(tar -tJf $CLAY | grep 'var/log/syslog$' | head -1)
tar -xJOf $CLAY "$SYS" | grep -n 'snap.openstack-hypervisor.hook.configure\|apparmor="DENIED"\|iptables-legacy\|cpu_byteorder'
```

**Distinguishing from related patterns:**
- vs. the storage-convergence false-negative family: this run does **not** fully recover in the final `juju_status_openstack-machines.txt`; `openstack-hypervisor/3` is still blocked
- vs. the storage-node Snap Store outage pattern: `microceph/3` on the storage node succeeds and becomes active; the failure is on `claydol`'s `openstack-hypervisor/3`, not on `ledian`'s own snap install
- vs. k8s/MetalLB join-time waits: the decisive blocker here is a Juju hook traceback in `openstack-hypervisor/3`, not `k8s/*` workload waiting

**Observed in:**
- Run 26650775097 (UUID: 359ada6c-45c2-489d-a06a-9314b8244ffd, tor3-sqa-testflinger cluster_1, branch `main`, `ledian.maas` roles `storage`, earlier node `claydol.maas` `openstack-hypervisor/3` blocked in `ceph-access-relation-changed`, 2026-05-29).

---

>>>>>>> Stashed changes
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
<<<<<<< Updated upstream
=======
- **v2.10** (2026-05-25): Added bootstrap-time Charmhub token-service outage pattern — `sunbeam cluster bootstrap` fails with only a generic `TerraformException`, but post-failure Juju/Kubernetes snapshots show `mysql`, `rabbitmq`, `traefik`, and `traefik-public` stuck in `ImagePullBackOff`; pod details prove image auth failed because `https://api.charmhub.io/v1/tokens/docker-registry` returned `503 Service Unavailable`; from run 26226826932 (UUID: 3140b6d1-aa24-436f-aef4-b2dd22477cdd, tor3-sqa-shared_maas dh1_j9_1, branch `aipoc`, openstack rev 1004 / `2024.1/beta`).
- **v2.11** (2026-05-25): Added join-time Snap Store assertion outage pattern — `sunbeam cluster join` on storage node `claydol.maas` timed out after 1800s because `microceph/3` repeatedly failed `snap install microceph --channel squid/candidate` with `unable to contact snap store`; node-local `snapd` syslog showed `api.snapcraft.io` assertions fetches returning HTTP 503, and subordinate `epa-orchestrator/6` failed with `SnapAPIError` / `SnapNotFoundError`; from run 26220369033 (UUID: 0f7a43e5-db7a-4968-b7a4-80c97024b3f8, tor3-sqa-testflinger cluster_1, branch `main`).
- **v2.12** (2026-05-25): Added sixth confirmation of the parallel-join false-negative family on `tor3-sqa-testflinger` cluster_1 — `doble.maas` (`control,storage`) timed out after 1800s in `sunbeam cluster join` while earlier `openstack-hypervisor` units were still converging and later machines 4, 5, and 6 were still being admitted; final cluster/K8S/Juju snapshots showed `doble` fully joined and the model recovered; from run 26252890519 (UUID: 6ec1d2ac-4bba-4447-b337-a0dfa1acec2b, branch `main`).
- **v2.13** (2026-05-25): Added bootstrap false-failure pattern on `tor3-sqa-testflinger` — `sunbeam cluster bootstrap` on `bronzor.maas` went silent for 33m, the SSH transport returned `Timeout, server bronzor.maas not responding.` / exit 255, but post-failure `sunbeam_cluster_list.txt`, `juju_status_openstack.txt`, `kubectl_get_pod.txt`, and `juju_debug_log_openstack-machines.txt` proved the single-node bootstrap had already converged; from run 26285062285 (UUID: 4cd6f7f0-434b-442e-8ddf-9ce278b1b73e, branch `main`).
- **v2.14** (2026-05-25): Added runner-side DNS / SSH reachability loss pattern on `tor3-sqa-shared_maas` — `sunbeam cluster bootstrap` on `solqa-shared-maas-server-10.maas` timed out after ~38m with `Timeout, server ... not responding.` / exit 255, and all post-failure SSH/SCP attempts to every `.maas` host immediately failed with `Temporary failure in name resolution`, preventing status collection; from run 26288126836 (UUID: d5fa73a7-9822-4d9b-847e-373f59ea34da, branch `main`).
- **v2.15** (2026-05-25): Added a second confirmation of the control-plane join false-negative pattern on `tor3-sqa-shared_maas` / `dh1_j9_1` — servers 32, 34, and 36 each failed `sunbeam cluster join` with `Failed to get k8s nodes to update`, while node-local Sunbeam logs showed `No matching k8s node found` and post-failure `kubectl_get_node.txt` showed those same nodes `Ready control-plane,worker`; bootstrap `k8s/0` was `unknown/lost` by collection time; from run 26338138903 (UUID: 71805006-220e-4a49-a12b-89e235c314ba, branch `main`).
- **v2.16** (2026-05-25): Added seventh confirmation of the `tor3-sqa-testflinger` parallel-join false-negative family on cluster_1 — `ledian.maas` (`control,compute`) timed out after 1800s in `sunbeam cluster join`, but the timeout snapshot already showed machine `2` started with `k8s/1` Ready while `openstack-hypervisor/2` was still executing `config-changed`; final cluster/K8S/Juju snapshots showed `ledian` fully joined and the cluster recovered; from run 26338143617 (UUID: f20d40df-9e36-4c83-aaec-fd4953ea7236, branch `main`).
- **v2.17** (2026-05-25): Added bootstrap-time 3600s wait-timeout pattern on `tor3-sqa-shared_maas` — `sunbeam cluster bootstrap` on `solqa-shared-maas-server-10.maas` exited 1 after the model never converged; `logs-openstack-nova-0.txt` showed `nova-conductor` crashed on an early Keystone/Placement HTTP 502 and never drove Nova out of Bootstrapping afterward, while `cinder-volume-mysql-router` simultaneously showed a stale blocked app status despite an active backend-database relation; from run 26197342522 (UUID: 29af6577-4ec1-45f6-b2d2-f976dd214a67, branch `main`).
- **v2.18** (2026-05-27): Added bootstrap-node Snap Store HTTP 408 pattern — `sunbeam_deploy` on `tor3-sqa-shared_maas` / `dh1_j9_1` failed ~11s into the layer because the initial `sudo snap install openstack --channel 2024.1/beta` on `solqa-shared-maas-server-31.maas` returned `cannot query the store for updates: got unexpected HTTP status code 408` from `api.snapcraft.io/v2/snaps/refresh`; bootstrap never started and later `juju models` errors were only post-failure collection cascades; from run 26462983826 (UUID: ce4bafb6-62cf-42d1-a3ea-56ecf9b59752, branch `main`).
- **v2.19** (2026-05-27): Added bootstrap-time Juju controller charm download TLS-handshake timeout pattern — `sunbeam prepare-node-script --bootstrap` got through the initial `api.charmhub.io` reachability check and installed `openstack`/`juju`, but Juju bootstrap then failed downloading `juju-controller` from `canonical-bos01.cdn.snapcraftcontent.com`; no Sunbeam cluster artifacts were produced and later `juju models` errors were only collection cascades; from run 26457590888 (UUID: d489a2f8-1786-4909-888f-3ad1ddcc16e3, branch `main`).
- **v2.20** (2026-05-27): Added `tor3-sqa-shared_maas` parallel-join false-negative pattern — servers 32, 35, and 43 each hit the internal 1800s `sunbeam cluster join` wait timeout while storage/backend and certificate hooks were still converging, but a queued later join still finished and final `sunbeam_cluster_list.txt` / `juju_status_openstack-machines.txt` showed all seven nodes and all units healthy; from run 26435144943 (UUID: f14615e7-bfef-40ce-97df-7cb515cdb7ad, branch `main`).
- **v2.21** (2026-05-30): Added bootstrap 3600s wait-timeout pattern — `ovn-central/0` permanently blocked after `check_pebble_handlers_ready()` timed out calling `can_connect()` on a sidecar Pebble socket during `ovsdb-cms-relation-changed` at 10:29:23Z; the pod had 0 restarts and was 4/4 Running at teardown, indicating a transient resource-contention-driven socket unresponsiveness on a loaded testflinger node rather than a service crash; from run 26680401911 (UUID: 23f5cdb9-da4a-41c1-9fe4-b5ce5e049733, tor3-sqa-testflinger cluster_1, branch `main`, 2024.1/beta, 2026-05-30).
- **v2.22** (2026-05-30): Added eighth confirmation of the parallel-join false-negative family on `tor3-sqa-testflinger` cluster_2 — `mouser.maas` (control,compute) and `avery.maas` (control,storage) both timed out after 1800s in `sunbeam cluster join`; at timeout `k8s/0` was `maintenance: "Allocating Control_Plane Cluster tokens"` (11s before deadline) and `k8s/2` (mouser) was `maintenance: "Joining cluster"` (19s before deadline); remaining nodes (behaim, kulik, evans, ditto) all completed after the deadline; final `sunbeam_cluster_list.txt` and all Juju model snapshots confirmed full cluster convergence; from run 26678355136 (UUID: 7b8f6054-d5b2-4b45-b452-d1cf76574e92, branch `main`, 2026-05-30).
- **v2.23** (2026-05-30): Added premature-configure / Neutron ingress EOF pattern on `tor3-sqa-testflinger` cluster_3 — `sunbeam configure` started 9s after the final `sunbeam cluster list`, but the bootstrap node's terraform apply failed creating `openstack_networking_router_interface_v2.user_router_interface` because the Neutron API behind `traefik-public` (`10.241.4.104`) returned `OpenStack connection error, retries exhausted. Last error was: EOF`; `juju_debug_log_openstack.txt` still showed Traefik `Provider not ready` messages for Neutron (port 9696) throughout the configure window; from run 26665920907 (UUID: 3d5b2587-f94d-40e4-b90a-f640e3e43739, branch `main`, 2026-05-30).
- **v2.24** (2026-06-02): Added `sunbeam cluster resize` 3600s wait-timeout pattern on `tor3-sqa-testflinger` cluster_1 — all seven nodes joined and `openstack-machines` converged, but `nova/1` stayed `waiting` with `(workload) Container service not ready` after repeated Nova MySQL-router `ECONNREFUSED` errors plus Placement/Keystone HTTP 502/500 failures caused `nova-conductor` and `nova-scheduler` to crash-loop; from run 26662396210 (UUID: c9f97030-6c7a-4272-81b3-685ab5acad99, branch `main`, 2026-05-30).
- **v2.25** (2026-06-02): Added ninth confirmation of the parallel-join false-negative family on `tor3-sqa-testflinger` cluster_3 — `euler.maas` (`control,storage`) and then `cajal.maas` (`control,compute`) each hit the internal 1800s `sunbeam cluster join` wait timeout while `k8s/2` was still waiting for a cluster token and `openstack-hypervisor/2` was still converging on certificates/ceph hooks; later joins still completed and all final Sunbeam/Juju/Kubernetes snapshots showed full cluster convergence; from run 26657349467 (UUID: 87f87610-4c3f-4b6d-ad7e-7c17b5d2483a, branch `main`, 2026-05-29).
- **v2.26** (2026-06-02): Added an 1800s cluster_2 confirmation of the storage-convergence false-negative variant on `tor3-sqa-testflinger` — `ditto.maas` (`control,compute`) timed out in `sunbeam cluster join` while `cinder-volume/3` was `waiting: (cinder-volume) integration incomplete`, `cinder-volume-ceph/3` was still converging, and `openstack-hypervisor/3` was still executing relation hooks; later joins still completed and final cluster/Juju snapshots showed full recovery; from run 26664595605 (UUID: 1e9216b1-618d-4c01-89d5-0b358b6c6fdb, branch `main`, 2026-05-29).
- **v2.27** (2026-06-02): Added `openstack-hypervisor/3` microovn/snapd timeout pattern on `tor3-sqa-testflinger` cluster_1 — `ledian.maas` (`storage`) surfaced the fatal `sunbeam cluster join` timeout, but the real blocker was earlier node `claydol.maas` where `openstack-hypervisor/3` re-entered `Bootstrapping`, timed out inside `_is_microovn_present()` / snap `find name=microovn`, and remained blocked; claydol syslog showed repeated `snap.openstack-hypervisor.hook.configure` AppArmor denials during the same hook window; from run 26650775097 (UUID: 359ada6c-45c2-489d-a06a-9314b8244ffd, branch `main`, 2026-05-29).
>>>>>>> Stashed changes
