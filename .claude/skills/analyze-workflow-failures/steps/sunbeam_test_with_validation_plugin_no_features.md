# Step Knowledge: sunbeam_test_with_validation_plugin_no_features

## Step Overview

Runs Sunbeam's built-in validation suite (`sunbeam validation run smoke`) against the deployed
OpenStack cluster — **without** any optional plugins enabled. Executes ~129 Tempest tests
across API, scenario, and networking suites. The step runs remotely on the bootstrap node
via SSH. Failure means the validation command returned a non-zero exit code, which happens
whenever `Failed: N` is non-zero in the Tempest results.

A "quick" validation (`sunbeam validation run quick`) also runs earlier in the same step
and covers ~23 API-only tests (no VMs, no ping). If quick passes but smoke fails, the
data plane (VM networking, floating IPs) is the likely culprit.

## Swift Artifacts

Objects stored under `<uuid>/generated/sunbeam/` that are useful for diagnosing failures:

| Path | Description | When to check |
|---|---|---|
| `generated/sunbeam/validation_smoke_<timestamp>.log` | Full Tempest smoke run output | Always — contains FAILED test names, tracebacks, captured logs |
| `generated/sunbeam/validation_quick_<timestamp>.log` | Quick (API-only) validation output | Check first: if passed, data-plane issue; if failed, API/service issue |
| `generated/sunbeam/latest_validation.log` | Copy of most recent validation log | Convenience symlink |
| `generated/sunbeam/output.log` | Step script DEBUG log | Step execution context |
| `generated/sunbeam/juju_status_openstack.txt` | Juju model status snapshot | Check for non-active units |
| `generated/sunbeam/juju_debug_log_openstack.txt` | Juju model debug log | Charm errors during test window |
| `generated/sunbeam/kubectl_get_pod.txt` | K8s pod list | Check for CrashLoopBackOff or Pending pods |
| `generated/sunbeam/pods_kube-system_logs.tgz` | Logs from kube-system namespace pods (Cilium, CoreDNS, etc.) | **Check when `2024.1/beta` fails north-south** — Cilium may be attaching BPF to `br-ex` |
| `generated/sunbeam/pods_openstack_logs.tgz` | Logs from all openstack-namespace pods | OVN/Neutron pod-level errors; extract and check `logs-openstack-*.txt` |
| `generated/sunbeam/pods_metallb-system_logs.tgz` | MetalLB speaker/controller logs | Relevant if LoadBalancer IPs fail |
| `generated/sunbeam/pods_controller-sunbeam-controller_logs.tgz` | Sunbeam controller namespace | Sunbeam operator errors |

## Key Log Files

No tgz archive for this step — all logs are flat files in `generated/sunbeam/`.

### Finding the right validation log

Each `sunbeam validation run <type>` call writes to a timestamped file:
```
generated/sunbeam/validation_<type>_<timestamp>.log
```
where `<type>` is `quick`, `smoke`, or `refstack`, and `<timestamp>` matches the wall-clock
time the command was launched (visible in the GitHub Actions log as the SSH call timestamp).

`latest_validation.log` — if present — is a copy of the **most recently completed** validation
run (whatever type that was). It is the fastest starting point when only the last run matters.

**How to locate the right log:**
1. Check `latest_validation.log` first — if the step ran only one validation type it will be
   the one that failed.
2. If multiple types ran (e.g. quick then smoke), find each by type:
   ```bash
   ls <work_dir>/<uuid>/generated/sunbeam/validation_*.log
   ```
3. Confirm the right file by matching its timestamp suffix to the SSH call timestamp in the
   GitHub Actions log:
   ```bash
   # GitHub log shows: sunbeam validation run smoke --output validation_smoke_2026-03-28T041135Z.log
   # → use validation_smoke_2026-03-28T041135Z.log
   ```

Each validation log contains full Tempest output:
- Per-test results (`ok`, `FAILED`, `SKIPPED`)
- Captured Python tracebacks on failure
- Captured `pythonlogging` section with OpenStack API call history
- `Totals` summary at the end

## Grep Patterns

```bash
UUID=<uuid>
DIR=<work_dir>/${UUID}/generated/sunbeam

# Quick start: check latest_validation.log if it exists
[ -f ${DIR}/latest_validation.log ] && grep -E "FAILED|Ran:|Failed:" ${DIR}/latest_validation.log

# List all validation logs and their types
ls ${DIR}/validation_*.log 2>/dev/null

# Summary from every validation log (shows which types ran and their outcomes)
grep -E "Ran:|Passed:|Failed:|Skipped:" ${DIR}/validation_*.log

# Show all failed tests in smoke log
grep "FAILED" ${DIR}/validation_smoke_*.log

# Get full failure block for a specific test (adjust class name and log type)
grep -A 40 "<TestClassName>.*FAILED" ${DIR}/validation_smoke_*.log

# Check quick validation result
tail -20 ${DIR}/validation_quick_*.log

# Look for SSH or ping timeout in smoke log
grep -i "timeout\|reachable\|ping\|ssh" ${DIR}/validation_smoke_*.log | grep -i "fail\|error\|timed"

# Check refstack log if present
[ -f ${DIR}/validation_refstack_*.log ] && tail -30 ${DIR}/validation_refstack_*.log
```

## Known Failure Patterns

### Pattern 1: Floating IP / VM Public Network Unreachable

**Symptom:**
```
{N} tempest.scenario.test_network_basic_ops.TestNetworkBasicOps.test_network_basic_ops [145.002441s] ... FAILED

    AssertionError: False is not true : Public network connectivity check failed
    Timed out waiting for <floating-ip> to become reachable

{N} setUpClass (tempest.api.compute.servers.test_server_actions.ServerActionsTestJSON) [0.000000s] ... FAILED

    tempest.lib.exceptions.TimeoutException: Request timed out
    Details: None
```

**Root cause:** VMs can be created (Nova API is up) but are unreachable via floating IPs. The
OpenStack control plane is functional (API tests pass in "quick" validation), but the L3 data
plane — OVN routing, floating IP NAT, or external gateway forwarding — is not delivering ICMP
or TCP traffic to the VMs. Both tests fail on the same underlying issue: a VM's floating IP
does not respond to ping/SSH from outside the tenant network.

This can manifest in multiple tests simultaneously because Tempest runs workers in parallel;
any scenario test that requires `check_vm_connectivity` or `wait_for_ssh` will fail.

**Evidence to look for:**
- `validation_smoke_*.log`: Two or more FAILED tests both involving `ping_ip_address`, `check_vm_connectivity`, or `wait_for_ssh`
- `validation_quick_*.log`: All tests pass — confirming API plane is healthy, only data plane is broken
- VM console output (in `pythonlogging` section, fetched after ping timeout): look for the
  cloud-init metadata sequence — if instance-id fetch retried several times before succeeding,
  it indicates OVN port programming lag; if `failed to get user-data` appears, that is
  **expected** (Tempest doesn't set user-data; Nova returns 404, which CirrOS logs as a warning)
- `juju_status_openstack.txt`: Check for `ovn-central`, `ovn-relay`, or `neutron` units not in `active/idle` state
- `kubectl_get_pod.txt`: Check for OVN-related pods in non-Running state
- `pods_openstack_logs.tgz`: Extract and check `logs-openstack-neutron-*.txt` for `RowNotFound`
  errors and `logs-openstack-ovn-central-*.txt` for OVN NB/SB DB errors (see Pod Log Patterns below)

**Distinguishing east-west vs north-south failure:**
If the VM console shows DHCP worked and the metadata proxy eventually responded, intra-tenant
OVN east-west paths are functional. The failure is then specific to the OVN gateway chassis
and the external VLAN path (north-south), not a general OVN breakdown.

**Pod Log Patterns (background noise — do not over-interpret):**

When investigating floating IP failures, also check `pods_openstack_logs.tgz`:
```bash
mkdir -p <work_dir>/pods_openstack
tar -xzf <work_dir>/<uuid>/generated/sunbeam/pods_openstack_logs.tgz -C <work_dir>/pods_openstack

# Check for OVN NB/SB DB SSL errors (may be background noise)
grep -c "SSL_accept\|SSL_ERROR_ZERO_RETURN" <work_dir>/pods_openstack/generated/sunbeam/logs-openstack-ovn-central-0.txt

# Check for Neutron RowNotFound errors (race condition during parallel setup)
grep "Cannot find Logical_Router_Port" <work_dir>/pods_openstack/generated/sunbeam/logs-openstack-neutron-0.txt | grep "<test window>"

# Check relay health (should have zero SSL errors if healthy)
grep -c "SSL_accept" <work_dir>/pods_openstack/generated/sunbeam/logs-openstack-ovn-relay-*.txt
```

Two known-benign patterns (confirmed in run bf85bf5d):

1. **OVN NB/SB DB SSL `SSL_ERROR_ZERO_RETURN`** in `logs-openstack-ovn-central-*.txt`:
   Occurs every ~10 seconds from pod start throughout the entire run. A client (likely
   `ovn-controller` on compute nodes or a health-check probe) does a TLS connection and
   immediately closes without completing the handshake. **Verdict: background noise.** Compare
   against rate before the test — if it's been consistent for hours, it's not a new failure.

2. **Neutron `RowNotFound: Cannot find Logical_Router_Port`** in `logs-openstack-neutron-*.txt`:
   Occurs during parallel Tempest `setUpClass` credential setup. Tempest creates many
   user/project/router combos in parallel; Neutron's `after_update` callback fires before
   `ovn-northd` has processed the LRP creation event. **Verdict: benign race condition.**
   OVN's periodic reconciliation recovers. Watch the LRP IDs — if they match the failing
   test's own router port, that would be significant; if they're from other test workers, not.

### Pattern 2: Cilium `br+` Device Pattern Intercepts OVN `br-ex` (Beta Channel Regression)

**Symptom:** Identical to Pattern 1 (floating IP unreachable, quick passes, smoke fails)
but specifically on `2024.1/beta` deployments. `2024.1/stable` runs of the same test pass.

**Root cause:** Cilium is deployed with `--devices='br+,bond+,eth+,...'` in the beta
channel. The `br+` wildcard matches `br-ex` — the OVN external bridge that carries
north-south floating IP traffic. Whenever the OVN gateway chassis is elected on a K8s
cluster node, `br-ex` is created on that node. Cilium's device controller immediately
detects it and attaches `cil_from_netdev`/`cil_to_netdev` eBPF programs via `tcx`. These
programs intercept ALL traffic entering/leaving the external bridge. Floating IP ICMP
(Tempest's ping) and SSH are silently dropped because Cilium doesn't recognise them as
K8s pod endpoints.

**Evidence in `pods_kube-system_logs.tgz`:**
```bash
# Extract kube-system pod logs
mkdir -p <work_dir>/pods_kube_system
tar -xzf <work_dir>/<uuid>/generated/sunbeam/pods_kube-system_logs.tgz -C <work_dir>/pods_kube_system

# 1. Confirm br+ in devices config
grep "devices=" <work_dir>/pods_kube_system/generated/sunbeam/logs-kube-system-cilium-*.txt | grep "br+" | head -3

# 2. Look for br-ex in device detection events
grep "Devices changed\|br-ex" <work_dir>/pods_kube_system/generated/sunbeam/logs-kube-system-cilium-*.txt | grep "br-ex" | head -20

# 3. Confirm BPF attachment to br-ex
grep "attached to device br-ex" <work_dir>/pods_kube_system/generated/sunbeam/logs-kube-system-cilium-*.txt
```

**Key log lines (from run bf85bf5d):**
```
02:58:53  --devices='br+,bond+,eth+,eno+,ens+,enp+,em+,vlan+'  ← br+ matches br-ex
02:50:47  Devices changed: [br-ex ...] on server-01            ← OVN gateway chassis elected
02:50:54  Program cil_from_netdev attached to device br-ex     ← BPF hooks on OVN bridge
03:09:24  Devices changed: [... br-ex] on server-02            ← gateway chassis re-election
03:09:31  Program cil_from_netdev attached to device br-ex     ← BPF re-attached
03:14     br-ex cleared from both nodes (no explicit detach logged)
```

**Why `2024.1/stable` passes:** The stable channel does NOT include `br+` in `--devices`.
Cilium ignores `br-ex` entirely; OVN north-south traffic flows through unintercepted.

**Verification steps:**
1. `tc filter show dev br-ex` on the OVN gateway chassis node — confirms if BPF programs
   are attached at failure time
2. `cilium config view | grep devices` — compare stable vs beta
3. `cilium monitor --type drop` during test — would show dropped ICMP to floating IP

**Fix:** Remove `br+` from Cilium's `--devices` configuration, or add explicit exclusions
for OVS bridge interfaces (`br-ex`, `br-int`) using Cilium's `--devices-exclude` option.

### Pattern 3: Cilium `cil_from_netdev` on physnet1 NIC after Policy Update (edge/cilium Channel)

**Symptom:** Identical to Pattern 1/2 (floating IP unreachable, quick passes, smoke fails)
but on `2024.1/edge/cilium` with the `!br-ex` exclusion fix already applied. The Cilium fix
from Pattern 2 is confirmed working; this is a different failure mechanism.

**Key distinguishing feature:** One VM created BEFORE `sunbeam enable validation` (i.e. before
the Tempest K8s pod is scheduled) CAN be reached via its floating IP. All VMs created AFTER
are unreachable. The boundary is when Cilium's "UpdatePolicyMaps for all endpoints" event fires.

**Timeline from run 03618294 (2024.1/edge/cilium, dh1_j9_1, 2026-04-01):**
```
15:21:42  sunbeam_launch_vm: SSH to FIP 10.243.38.102 → SUCCESS
15:22:35  Cilium: "UpdatePolicyMaps for all endpoints" on ALL nodes (tempest-0 pod identity 40487)
15:28–29  Quick validation: 21/23 PASS (API-only, no traffic)
15:31:18  Tempest FIP 10.243.38.148 associated
15:31:19  ICMP ping starts → 120s timeout → FAIL
```

**Root cause hypothesis:** Cilium's `cil_from_netdev` and `cil_to_netdev` BPF programs are
attached (via TCX) to `enp1s0f1` — the **physical NIC used as an OVS port inside `br-ex`** for
physnet1 external traffic. Because `enp1s0f1` is in Cilium's `--devices` list (matching `enp+`),
it is treated as a managed network interface. When the tempest-0 pod is created and its identity
is propagated, Cilium runs "UpdatePolicyMaps for all endpoints" on all nodes, which reprograms
BPF maps for every tracked endpoint including the `enp1s0f1` device endpoint. After this update,
`cil_from_netdev` may enforce policy for traffic arriving from the external network on `enp1s0f1`
(VLAN 2734 packets from the physical router destined for the OVN gateway), dropping packets that
don't match a known K8s pod endpoint — making all new floating IPs unreachable.

Note: `cil_from_netdev` on `enp1s0f1` runs BEFORE OVS's `rx_handler` in the Linux TC ingress
chain, so if it drops a packet, OVS and OVN never see it.

**Evidence to confirm (not yet confirmed — needs live debugging):**
```bash
# 1. Check Cilium devices on all nodes — enp1s0f1 should be present
grep "devices=" <work_dir>/pods_kube_system/generated/sunbeam/logs-kube-system-cilium-*.txt

# 2. Look for UpdatePolicyMaps event around the time validation plugin is enabled
grep "UpdatePolicyMaps\|Processing identity update\|policyID.Added" \
  <work_dir>/pods_kube_system/generated/sunbeam/logs-kube-system-cilium-*.txt

# 3. Check timing: was FIP tested before or after UpdatePolicyMaps?
#    (test-instance FIP before update = works; Tempest FIPs after = fail)

# To confirm during a live debugging run:
# cilium monitor --type drop --from-label=any     # watch for dropped packets on enp1s0f1
# tc exec bpf dbg dev enp1s0f1 direction ingress  # trace incoming external traffic
```

**Why `!br-ex` patch alone is insufficient:** The `!br-ex` exclusion correctly prevents Cilium
from attaching to `br-ex` itself. However, the physical NIC (`enp1s0f1`) used as the OVS uplink
for `br-ex` is still in Cilium's device list via `enp+` matching. Traffic to/from floating IPs
traverses `enp1s0f1` → OVS (inside `br-ex`) → OVN NAT → VM. If Cilium intercepts `enp1s0f1`,
the external packets are still at risk.

**Fix candidates:**
1. Add `!enp1s0f1` (or `!enp+` more broadly) to Cilium's `--devices` exclusion alongside `!br-ex`
2. Or use Cilium's `direct-routing-device` to restrict which interfaces it manages
3. Ensure physnet1 OVS uplink NICs are excluded from Cilium device management
4. Alternatively: configure Cilium with `--install-no-conntrack-iptables-rules=false` or
   use a policy that explicitly permits transit traffic through device endpoints

### Pattern 4: `virtual_maas` SSH Tunnel Drops During Validation (False Refstack Failure)

**Symptom:** `quick` and `smoke` complete successfully (`Failed: 0`), then the step fails while
starting or waiting for `sunbeam validation run refstack` with:

```text
subprocess.CalledProcessError: Command '['ssh', ... 'sunbeam', 'validation', 'run', 'refstack', ...]'
returned non-zero exit status 255.
```

The failing stderr may contain only the host-key warning, with no Tempest traceback or failed
Refstack test summary.

**Root cause:** On `tor3-sqa-virtual_maas`, the bootstrap node is reached through an SSH tunnel to
the virtual lab infra. If that tunnel drops mid-step, the outer SSH session from the runner to the
bootstrap node dies with exit code 255 even though the Sunbeam control plane and earlier
validation profiles were healthy. The pipeline then reports the step as failed, but the failure is
transport loss between the runner and the virtual MAAS environment — not a confirmed Tempest or
OpenStack validation failure.

**Evidence to look for:**
- `generated/sshtest.txt`: periodic `infra1` successes followed by SSH errors such as
  `kex_exchange_identification: Connection closed by remote host`
- `generated/lastlines.txt` / GitHub failed log: `quick` and `smoke` both show `Failed: 0`
- The refstack command starts, then later raises `CalledProcessError` exit status 255 from
  `products/sqa_common/helpers.py:run_cmd`
- Expected `validation_refstack_*.log` may be missing from Swift because the runner lost access
  before later collection/copy steps could retrieve it

**Timeline check:**
1. Confirm the last successful `sshtest.txt` probe timestamp
2. Confirm the probe failure occurs before or during the refstack SSH window
3. Treat any MAAS log collection after the last successful tunnel probe as irrelevant to this run

**Fix candidates:**
1. Make the workflow fail explicitly on tunnel-loss detection before starting another validation profile
2. Add retry/reconnect logic around `run_remote_command()` when SSH exits 255 on `virtual_maas`
3. Persist validation logs directly on the runner as each profile finishes so a later tunnel loss does not hide refstack results

---

_Add more patterns below as they are discovered._

## Notes

- This step can run on `shared_maas` or `virtual_maas`; if the substrate is `virtual_maas`, check `generated/sshtest.txt` first because SSH tunnel loss can masquerade as a validation failure
- Validation commands are executed via SSH on the bootstrap node
- A non-zero exit code from `sunbeam validation` raises `subprocess.CalledProcessError`
  in `test_with_validation_feature.py`, which propagates as the step failure
- "quick" validation (API-only, ~23 tests) always runs before "smoke" (~129 tests); if quick
  passes but smoke fails, the data plane is the suspect (VMs/networking), not the API layer
- The guidance in this file applies to all `sunbeam_test_with_validation_plugin_*` step
  variants — the log layout and validation log naming scheme are identical across them

## Version History

- **v1.0** (2026-03-30): Initial version — Pattern A (floating IP unreachable) from run
  23674710083 (UUID bf85bf5d, tor3-sqa-shared_maas dh1_j8_1, branch main)
- **v1.1** (2026-03-31): Expanded Key Log Files and Grep Patterns sections — document
  `latest_validation.log` as the quick-start entry point; explain how to identify the right
  `validation_<type>_<timestamp>.log` by matching type (quick/smoke/refstack) and timestamp
  from the GitHub Actions SSH call; added refstack grep; removed hardcoded `/tmp` paths.
- **v1.2** (2026-03-31): Enriched Pattern A with VM console analysis — east-west OVN paths
  (DHCP, metadata proxy) work while only north-south (floating IP via external VLAN) fails;
  clarified that `failed to get user-data` in CirrOS console is expected behaviour (Tempest
  sets no user-data); added guidance on distinguishing east-west vs north-south scope using
  the metadata retry sequence; from re-analysis of run 23674710083 (UUID bf85bf5d).
- **v1.3** (2026-03-31): Added pod log patterns — `pods_openstack_logs.tgz` extraction
  recipe; documented two benign patterns: (1) OVN NB/SB DB `SSL_ERROR_ZERO_RETURN` every
  ~10s (persistent background health-check probe); (2) Neutron `RowNotFound` for LRPs
  during parallel Tempest credential setup (OVN sync race condition); from deep-dive on
  run 23674710083 (UUID bf85bf5d); confirmed neither explains the floating IP failure.
- **v1.4** (2026-03-31): Added Pattern B (Cilium `br+` intercepts OVN `br-ex`) — root cause
  of the `2024.1/beta` vs `2024.1/stable` regression; Cilium `--devices='br+,...'` causes
  `cil_from_netdev`/`cil_to_netdev` BPF hooks to be attached to `br-ex` (OVN external
  gateway bridge) on each gateway chassis election, silently dropping floating IP traffic;
  added `pods_kube-system_logs.tgz` and other pod namespaces to Swift Artifacts table;
  fix: remove `br+` from Cilium `--devices` or add OVS bridge exclusions.
- **v1.5** (2026-04-01): Added Pattern C (`cil_from_netdev` on physnet1 NIC after policy
  update) — on `2024.1/edge/cilium` with `!br-ex` fix applied; `enp1s0f1` (physnet1 OVS
  uplink, matches `enp+` in `--devices`) retains Cilium BPF hooks; after Cilium
  "UpdatePolicyMaps for all endpoints" (triggered by tempest-0 pod at 15:22:35), new
  floating IPs become unreachable; test-instance FIP created before the update worked;
  all Tempest FIPs after update failed; from run 23847613242 (UUID 03618294, dh1_j9_1, main).
- **v1.6** (2026-05-25): Added Pattern 4 (`virtual_maas` SSH tunnel drops during validation)
  from run 26208199607 (UUID 7862b0c0-1e97-4c96-9d50-abf1f14a645b, cluster_1): `quick` and
  `smoke` both passed with `Failed: 0`, but the outer SSH session to the bootstrap node died
  during `sunbeam validation run refstack`; `generated/sshtest.txt` showed the last successful
  probe at 09:53:38 UTC followed by `kex_exchange_identification: Connection closed by remote host`.
