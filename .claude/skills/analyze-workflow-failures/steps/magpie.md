# Step Knowledge: magpie

## Step Overview

The `magpie` layer deploys the [magpie charm](https://charmhub.io/magpie) to validate
network connectivity between cluster nodes. It:
1. Deploys MAAS machines for the Juju magpie model
2. Deploys multiple magpie application instances, one per network space
   (e.g. `magpie-public`, `magpie-oam`, `magpie-cloud-ceph-replicate`)
3. Each `juju deploy` call is issued sequentially; failure in any one aborts the step

The Juju controller deployed in the preceding `juju_maas_controller` step is used throughout.
Controller HA nodes typically run at IPs like `10.241.144.81–83` (virtual_maas cluster_3).

## Swift Artifacts

| Path | Description | When to check |
|---|---|---|
| `generated/magpie/log.txt` | FCE build log for the magpie layer | Always — first stop |
| `generated/maas/logs-<timestamp>.tgz` | Full MAAS infra host logs | Deep investigation (virtual_maas/dedicated_maas only) |
| `generated/github-runner/jobs.json` | GitHub API step conclusions | Quick step triage |

## Key Log Files (inside tgz archive)

> Only present for `virtual_maas` and `dedicated_maas` substrates.
> The Juju controller VMs (e.g. 10.241.144.81–83) run *inside* the MAAS infra hosts as KVM
> guests — their internal logs (MongoDB, Juju agent) are **not** collected in the tgz archive.
> Only the host-level syslog is available.

| File | What it contains | When to use |
|---|---|---|
| `var/log/syslog` | System, snapd, temporal-server, libvirt events | Primary investigation |
| `var/log/libvirt/qemu/*.log` | KVM guest console/QEMU logs | VM boot/crash events |

## Grep Patterns

```bash
# Find the primary juju deploy error in FCE log
grep -i "error\|failed\|cannot add" <work_dir>/<uuid>/generated/magpie/log.txt

# Check which juju deploy calls succeeded vs failed
grep "Deploying magpie\|Command failed\|cannot add" <work_dir>/<uuid>/generated/magpie/log.txt

# Check for snap refreshes on infra hosts around the failure window
grep "2026-<date>T<HH>:" <work_dir>/maas-logs-*/*/var/log/syslog | grep -i "snap\|refresh\|juju"

# Check for VM-level events on infra hosts
grep "2026-<date>T<HH>:" <work_dir>/maas-logs-*/*/var/log/syslog \
  | grep -v "kernel\|audit\|apparmor\|named\|#011\|maas\|temporal\|haproxy\|nginx"
```

## Known Failure Patterns

### Pattern 1: Juju controller MongoDB "not master and slaveOk=false"

**Symptom:**
```
ERROR cmd charm.go:372 cannot add application "magpie-<space>": getting model: not master and slaveOk=false
ERROR failed to deploy charm "magpie"
subprocess.CalledProcessError: Command '['juju', 'deploy', '-m', 'magpie', ..., '--channel=legacy/stable']' returned non-zero exit status 1.
```

**Root cause:** The Juju controller runs an internal MongoDB replica set (one member per HA
controller node). When `juju deploy` attempts to write the new application to the model, the
mgo (Go MongoDB) driver contacts the replica set. If the primary is temporarily unavailable
(election in progress, network hiccup, leader stepped down), the driver receives the "not
master and slaveOk=false" error from the secondary it connected to. The `juju deploy` command
does not retry — it fails immediately. Earlier `juju deploy` calls in the same step may have
succeeded; only the one that hits the election window fails.

**Timing signature:** The failed `juju deploy` runs for ~52 seconds before the error appears,
suggesting the mgo driver exhausted its connection retry window before giving up.

**Evidence to look for:**
- `generated/magpie/log.txt`: `cannot add application "magpie-<space>": getting model: not master and slaveOk=false`
- `generated/magpie/log.txt`: Earlier `juju deploy` calls for other spaces completed successfully
- MAAS infra host syslog: No VM restarts or libvirt events for controller VMs — the failure
  is transient and typically self-resolving; the hosts appear healthy

**Note:** The Juju controller VM logs (MongoDB, Juju agents) are not captured in the MAAS
tgz archive since they run as KVM guests. There is no direct log evidence of the MongoDB
primary election — the only evidence is the client-side error message.

**Retryable:** Yes. This is a transient infrastructure condition. Re-running the pipeline
typically succeeds.

---

---

### Pattern 2: Physical node stuck in "Failed deployment: Loading ephemeral" → 30-minute timeout

**Symptom (FCE magpie/log.txt and GitHub Actions log):**
```
foundationcloudengine.layers.magpielayer INFO Deployed: 0/5
Status: {'azurill': 'Deploying', 'duosion': 'Deploying', ...}
…
foundationcloudengine.layers.magpielayer INFO Deployed: 0/5
Status: {'azurill': 'Deploying', 'duosion': 'Failed deployment', ...}
…
foundationcloudengine.layers.magpielayer INFO Deployed: 4/5
Status: {'azurill': 'Deployed', 'duosion': 'Failed deployment', ...}
…
Exception: Nodes did not reach Deployed state after 1800 seconds.
##[error]Process completed with exit code 1.
```
One node transitions to `Failed deployment` ~8–9 minutes after entering `Deploying`; the
other nodes complete normally. The Juju machine status shows `provisioning error` with
message `"Failed deployment: Loading ephemeral"`.

**Root cause:** MAAS attempts to deploy the physical node by PXE-booting it into a RAM-based
ephemeral Ubuntu environment. The node fails to load this ephemeral system — either due to a
transient PXE/TFTP download failure, a power/BMC hiccup during the boot cycle, or a
transient NIC issue — and MAAS marks it as `provisioning error`. Because the failure occurs
during the ephemeral-loading phase, no Curtin config is generated for the node (distinguishing
it from failures that occur during OS installation).

FCE's `wait_till_deployed` requires **all** N nodes to reach `Deployed` state before
proceeding; one node stuck in `Failed deployment` causes the full 30-minute timeout to expire.

**Evidence to look for:**

- `generated/magpie/juju_status_foundations-maas_magpie.json`: affected machine has
  `machine-status.current: "provisioning error"` and
  `machine-status.message: "Failed deployment: Loading ephemeral"`
- `generated/magpie/log.txt`: node transitions from `Deploying` to `Failed deployment`
  approximately 8–10 minutes after `juju add-machine` — consistent with MAAS's ephemeral
  boot timeout
- Swift bundle: **no curtin_config file** for the failing node (only files for nodes that
  reached the OS installation phase exist in the archive)
- Earlier in the same run: the failing node passed commissioning successfully (confirming it
  is capable of PXE booting and is not permanently defective)
- MAAS syslog (if available): look for absence of `Status transition from DEPLOYING to DEPLOYED`
  for the affected node's system ID

**Distinguishing from Pattern A (MongoDB not master):**

| | Pattern A | Pattern B |
|---|---|---|
| Failure phase | `juju deploy` (after all nodes deployed) | `deploy_maas_machines` (OS install) |
| Error message | `cannot add application "magpie-<space>": not master` | `Failed deployment: Loading ephemeral` |
| MAAS node state | All nodes `Deployed` | One node `Failed deployment` |
| Retryable | Yes (transient Juju HA election) | Yes (transient hardware/network) |

**Retryable:** Yes. This is a transient infrastructure failure on physical hardware. The
machine successfully commissioned earlier in the same run, confirming it is not permanently
broken. Re-running the pipeline is expected to succeed.

**From run 24111787100 (UUID b881ccd8-749d-43dc-92d2-a3551ad7cb3f, tor3-sqa-dedicated_maas dh1_j2, 2026-04-08):**

`duosion` (system ID `68dntx`, zone1, 12 cores, 128GB RAM, HPE Gen9):
```
03:19:34 – juju add-machine (zone1 constraint) → allocates duosion
03:20:39 – all 5 nodes Deploying (duosion IP: 10.241.128.87)
03:28:58 – duosion: Failed deployment (others still Deploying)
03:30:57 – Juju records provisioning error: "Failed deployment: Loading ephemeral"
03:31:59 – 4/5 nodes Deployed; duosion still Failed deployment
03:49:55 – FCE 1800-second timeout
```
No curtin_config for `duosion` in the Swift bundle (meowth, pangoro, suicune, juju-4 present).
MAAS journal (10.241.128.4) was truncated; lower-level cause not determinable.

---

### Pattern 3: All nodes stuck in `Deploying` — slow simultaneous OS installation exceeds 30-min timeout

**Symptom (FCE magpie/log.txt and GitHub Actions log):**
```
foundationcloudengine.layers.magpielayer INFO Deployed: 0/4
Status: {'node1': 'Deploying', 'node2': 'Deploying', 'node3': 'Deploying', 'node4': 'Deploying'}
… (repeated every ~30s for full 30 minutes) …
Exception: Nodes did not reach Deployed state after 1800 seconds.
##[error]Process completed with exit code 1.
```

No node ever transitions to `Failed deployment`; all remain `Deploying` until timeout.

**Juju status at collection time (post-failure):**
```json
Machine 0: {"current": "allocating", "message": "Deploying: Configuring OS", "since": "01:37:47Z"}
Machine 1: {"current": "allocating", "message": "Deploying: Configuring OS", "since": "01:37:59Z"}
Machine 2: {"current": "allocating", "message": "Deploying: Rebooting",      "since": "01:58:22Z"}
Machine 3: {"current": "allocating", "message": "Deploying: Rebooting",      "since": "01:58:32Z"}
```

**Root cause:** With 4 simultaneous KVM VMs deploying, `install_kernel` requires downloading
~450 MB per node (including the 316 MB `linux-firmware` package) from `archive.ubuntu.com`.
Shared downlink bandwidth across all nodes means the total download is ~1.8 GB. The combined
download + curthooks time (grub install, kernel decompression, snap configuration) pushed 2
nodes past the 30-minute FCE `wait_till_deployed` timeout, and the other 2 nearly so (netboot
off at ~28 min, still waiting on installed-OS cloud-init to signal completion).

**Evidence to look for:**

- `generated/magpie/log.txt`: All N nodes show `Deploying` at every poll; none ever reach
  `Deployed`; no node shows `Failed deployment`
- `generated/magpie/juju_status_*.json`: machines in `"allocating"` state with message
  `"Deploying: Configuring OS"` or `"Deploying: Rebooting"` — no `"provisioning error"`
- MAAS syslog: `maasserver.models.node: [info] nodeN: Turning off netboot for node` appears
  for some/all nodes at ~26–28 min, confirming Curtin completed but Juju timeout had already
  fired or was imminent
- MAAS syslog: Curtin still posting status updates to MAAS API (`POST /MAAS/metadata/status/`)
  seconds *after* the FCE timeout timestamp — confirms OS installation was still in progress
- MAAS syslog node console: curtin `install_kernel` output shows
  `Need to get ~450 MB of archives` including `linux-firmware` (~316 MB)

**Distinguishing from Patterns A and B:**

| | Pattern A | Pattern B | Pattern C |
|---|---|---|---|
| Failure phase | `juju deploy` (post-machine-deploy) | `deploy_maas_machines` (ephemeral boot) | `deploy_maas_machines` (OS install) |
| Error message | `cannot add application: not master` | `Failed deployment: Loading ephemeral` | `Nodes did not reach Deployed state after 1800 seconds` |
| Node state | All nodes `Deployed` | One node `Failed deployment` | **All** nodes remain `Deploying` |
| MAAS sub-state | N/A | No sub-state (stuck in ephemeral) | `Configuring OS` / `Rebooting` |
| Curtin ran? | Yes (fully) | No | Yes (completed or nearly) |
| Hard failure? | Yes (mgo error) | Yes (PXE/TFTP failure) | No (just slow) |
| Retryable | Yes | Yes | Yes |

**Retryable:** Yes. This is a transient timing issue — no hard failure on any node. Re-running
is expected to succeed, particularly if fewer concurrent installations compete for bandwidth or
if cached packages are available.

**From run 24108540267 (UUID b57d9251-37ef-48ac-8d2f-bb8124011ae8, tor3-sqa-virtual_maas cluster_5, 2026-04-08):**

All 4 KVM VMs (node1–4; IPs .86–.89) started PXE deploy at 01:30. node3/4 completed Curtin
at 01:57:56 (~28 min; netboot off). node1/2 were still in Curtin at 02:00:37 (after the
02:00:24 FCE timeout). Curtin downloaded `linux-image-5.15.0-174-generic` and
`linux-firmware` (316 MB) at ~3.5–5 MB/s per node.

---

### Pattern 4: All nodes stay `Ready` — MAAS rejects deploy with 400 (distro_series not available)

**Symptom (FCE magpie/log.txt):**
```
foundationcloudengine.layers.magpielayer INFO Deployed: 0/4
Status: {'node1': 'Ready', 'node2': 'Ready', 'node3': 'Ready', 'node4': 'Ready'}
… (all nodes remain Ready every 30s poll for full 30 minutes) …
Exception: Nodes did not reach Deployed state after 1800 seconds.
```

Nodes briefly flash `Allocated` for a few polls immediately after `juju add-machine`,
then revert to `Ready` — they never transition to `Deploying`.

**Juju machine status at collection time (`juju_status_foundations-maas_magpie.json`):**
```json
"machine-status": {
  "current": "provisioning error",
  "message": "unexpected: ServerError: 400 Bad Request ({\"distro_series\": [\"'jammy' is not a valid distro_series.  It should be one of: '', 'ubuntu/noble'.\"]})"
}
```
All 4 machines show this identical error.

**Root cause:** The Juju model is configured with `default-series=jammy`. When Juju's MAAS
provisioner calls the MAAS deploy API it passes `distro_series=jammy`. MAAS rejects the
request with a 400 Bad Request because `ubuntu/jammy` images are not synced — only
`ubuntu/noble` is available. The Juju provisioner records a `provisioning error` and releases
the machine back to `Ready` in MAAS. FCE's `wait_till_deployed` only polls MAAS machine
status (which shows `Ready`, not an error state), so it polls for the full 1800 seconds
without seeing the Juju-level error.

**What to look for in the `maas` layer log:**
The `maas` layer created a boot source selection for the desired series:
```
maas root boot-source-selections create 1 os=ubuntu release=jammy 'subarches=*' ...
```
But the sync result contains only `ubuntu/noble` entries — no `ubuntu/jammy` appears
in any `images` list. The `maas` layer's validation passed without verifying that the
image was actually downloaded.

**Distinguishing from Patterns B and C:**

| | Pattern B | Pattern C | Pattern D |
|---|---|---|---|
| Node state | One `Failed deployment` | All `Deploying` | **All remain `Ready`** |
| MAAS sub-state | None (ephemeral boot fail) | `Configuring OS` / `Rebooting` | `Ready` (never allocated) |
| Juju machine status | `provisioning error: Failed deployment: Loading ephemeral` | `allocating` | `provisioning error: ServerError: 400 Bad Request` |
| Deployment started? | Yes (ephemeral env) | Yes (Curtin ran) | **No** (API rejected) |
| Retryable? | Yes | Yes | **No** — Jammy image must be imported first |

**Retryable:** ❌ No. The Jammy image is not in MAAS. Re-running will immediately hit the
same 400 error. The image must be imported (or the SKU updated to use `noble`) before
retrying.

**From run 24120449876 (UUID 00a49a3b-e5e5-43fc-94ab-a29e81186843,
tor3-sqa-virtual_maas, 2026-04-08, SKU master-magpie-snap-jammy-mixed):**

All 4 KVM nodes (ht6bk4, tf7444, cnwtwx, ppmp3g) were allocated at 08:31:56–08:31:59 UTC.
First MAAS poll at 08:32:25 showed all `Ready`. Juju machine status showed `provisioning
error: 400 Bad Request` for all 4 machines immediately. FCE timed out at 09:02:12 UTC
(1800 s after 08:32:12). MAAS 3.6.4, FCE 2.21.2, MAAS boot source had `ubuntu/jammy`
selection but sync only produced `ubuntu/noble` images.

---

### Pattern 5: MongoDB primary election disrupts juju-wait and renders Juju API completely unreachable

**Symptom (GitHub Actions and FCE log):**
```
ERROR:root:ERROR checking entity "user-admin" has permission:
  while obtaining controller user: while obtaining controller user:
  not master and slaveOk=false
ERROR:root:juju status --format=json failed: 1
Command 'juju-wait ... --retry_errors 0' returned non-zero exit status 1.
```
Then FCE's post-juju-wait status call:
```
ERROR no reachable servers
subprocess.CalledProcessError: Command '['juju', 'status', '-m', 'magpie', '--format=tabular']'
  returned non-zero exit status 1.
```

**Root cause:** The Juju HA controller's MongoDB replica set undergoes a primary election
while `juju-wait` is actively polling. The election causes two cascading failures:
1. juju-wait's internal permission check hits the "not master" error and exits immediately
   (because `--retry_errors 0` disables all retries)
2. The MongoDB election disrupts the Juju API (`jujud`) on all 3 HA controller VMs,
   rendering port 17070 completely unreachable for ~5–6 minutes

**Distinguishing from Pattern A:**

| | Pattern A | Pattern E |
|---|---|---|
| Failure phase | `juju deploy` (write to MongoDB) | `juju-wait` (permission check read) |
| Error message | `cannot add application: not master` | `not master` in permission check |
| API after error | Still reachable | `no reachable servers` for ~5–6 min |
| Trigger | mgo driver write during deploy | MongoDB primary election during wait |
| Juju API recovery | N/A (deploy fails, FCE moves on) | Yes — controller self-recovers |

**Timeline signature:**
- All units still `allocating` when juju-wait starts (agents bootstrapping on fresh VMs)
- juju-wait runs for only ~2 min before failing (vs 3600s timeout)
- `juju status` hung ~96 s then returned `no reachable servers`
- Version collector also sees `no reachable servers` / `connection is shut down` for 3–5 min
- juju-crashdump collected successfully a few minutes later (controller recovered)
- SSH tunnel healthy throughout (sshtest.txt shows no errors)

**Evidence to look for:**
- GitHub Actions log: `ERROR checking entity "user-admin" has permission: ... not master and slaveOk=false`
- GitHub Actions log: subsequent `juju status` → `ERROR no reachable servers`
- `generated/version_collector_magpie.log`: `juju models` → `ERROR no reachable servers` and `ERROR connection is shut down` within minutes of failure
- `generated/sshtest.txt`: no SSH tunnel errors — the infra hosts are reachable, but the Juju API on the controller VMs is not

**Retryable:** Yes. Transient Juju HA controller MongoDB election. Re-running is expected to succeed.

**From run 25455244438 (UUID 965bc456-75e9-4792-8ba1-bba0f6ca0bd0, tor3-sqa-virtual_maas cluster_4, main, 2026-05-06):**

juju-wait started at 23:35:41 with `--retry_errors 0`. All 30 units (5 apps × 6 machines)
were in `allocating` state. At 23:38:03, juju-wait encountered the `not master` error and
exited. Subsequent `juju status` returned `no reachable servers` (23:39:39), version
collector saw the same at 23:40:47 and `connection is shut down` at 23:41:02. Controller
recovered by 23:44:49 (crashdump collected). No snap auto-refreshes from controller VMs
(10.241.144.81–83) found in Squid proxy log during the window.

---

### Pattern 6: MAAS Temporal server deadlock during `deploy_maas_machines` → MAAS API hang → OAuth token expired (exit code 2)

**Symptom (FCE magpie/log.txt):**
```
2026-05-07-15:15:04 root DEBUG [localhost]: maas root machine read demn38
2026-05-07-15:27:59 root ERROR [localhost] Command failed: maas root machine read demn38
2026-05-07-15:27:59 root ERROR 1[localhost] STDOUT follows:
Authorization Error: 'Expired timestamp: given 1778166906 and now 1778167679,
has a greater difference than threshold 300'
subprocess.CalledProcessError: Command '['maas', 'root', 'machine', 'read', 'demn38']'
returned non-zero exit status 2.
```

A `maas root machine read` call in the `wait_till_deployed` polling loop hangs for
**12+ minutes** — far beyond the MAAS OAuth 300-second timestamp validity window. When MAAS
finally processes the request, it rejects the expired OAuth token with exit code 2.

**Distinguishing signals:**
- Earlier polling cycles show progressive slowdown: reads that took 3–4s each degrade to
  10–20s, then 38s–4m per node across successive cycles before the final hang
- The hanging node is the **first** machine in the next polling cycle (not the last in the
  previous cycle), so the OAuth token is generated fresh at the start of the hung call
- Exit code is **2** (OAuth error from MAAS), not 1 (subprocess error). This distinguishes
  it from network-level SSH errors (exit code 255)
- No SSH tunnel errors in `sshtest.txt` — the runner can reach the infra nodes fine;
  the hang is in the MAAS Temporal backend, not the SSH tunnel

**Root cause chain:**

1. **PostgreSQL serialization pressure:** Deploying 6 nodes simultaneously generates
   concurrent writes to `maasserver_node` and `bmc/power-parameters` secrets from
   multiple power-polling Temporal workflows. PostgreSQL emits `ERROR: could not serialize
   access due to concurrent update` and `WARNING: canceling wait for synchronous replication
   due to user request` (HA standby falling behind) from the onset of node deployment.

2. **Temporal DB transaction timeouts:** The MAAS Temporal server's task queue managers
   time out trying to begin DB transactions — `UpdateTaskQueue failed. Failed to start
   transaction. Error: context deadline exceeded` — starting ~3 minutes into deployment
   (15:03:17 in this run).

3. **Temporal goroutine deadlock:** From ~4 minutes in, Temporal's internal goroutines
   deadlock every ~30 seconds (`potential deadlock detected`). Temporal can no longer
   dispatch workflow tasks.

4. **Temporal shard loss:** ~7 minutes in, Temporal reports `shard status unknown` across
   all task queues, becoming fully non-functional.

5. **MAAS API hang:** Machine read API calls enter the MAAS request queue but the Temporal
   backend cannot drive them to completion. The TCP connection stays open but no response
   arrives for 12+ minutes.

6. **OAuth expiry:** MAAS CLI generates an OAuth token at the moment `maas root machine read`
   is invoked. After 773 seconds, MAAS rejects it as expired (threshold: 300s).

**Key log correlation:**

| Time (UTC) | Event |
|---|---|
| 15:00 | Six magpie nodes added; deployment starts |
| 15:00:17 | First PostgreSQL serialization error (`bmc/power-parameters`) |
| 15:01:32 | HA replication cancellation (standby overloaded) |
| 15:03:17 | First Temporal DB transaction timeout (`mnc73y@agent:main/3`) |
| 15:04:50–15:05:20 | FCE poll cycle: individual reads taking 14–20s (was 4s) |
| 15:06:00–15:14:34 | FCE poll cycle: reads take 15s–4m09s each |
| 15:07:13 | Temporal: "potential deadlock detected" (repeats every ~30s) |
| 15:10:47 | Temporal: "shard status unknown" — fully non-functional |
| 15:15:04 | FCE starts `maas root machine read demn38` — hangs |
| 15:27:59 | MAAS returns OAuth expiry error (exit code 2) |

**Grep to confirm:**
```bash
# Temporal errors in MAAS syslog
grep -h "maas-temporal" <work_dir>/maas-logs/*/var/log/syslog | python3 -c "
import sys,json,re
for l in sys.stdin:
  m=re.search(r'\{.*\}',l)
  if m:
    try:
      d=json.loads(m.group())
      if d.get('level')=='error': print(d.get('ts'), d.get('msg'), d.get('error','')[:80])
    except: pass
"

# PostgreSQL serialization errors
grep "could not serialize\|synchronous replication" \
  <work_dir>/maas-logs/*/var/log/postgresql/postgresql-*-ha.log
```

**Retryable:** Yes. This is a transient infrastructure overload condition. The MAAS Temporal
server self-recovers once the deployment pressure subsides; re-running the pipeline is
expected to succeed. However, recurrence is possible if deploying ≥6 nodes simultaneously
on this substrate.

**From run 25492917651 (UUID 35f56c6f-264f-47ae-aafd-3b679723f4ff, tor3-sqa-virtual_maas
cluster_6, MAAS 3.5.12-16413-g.7fb94f378, main, 2026-05-07):**

Six magpie nodes (node1–6) deployed concurrently. PostgreSQL serialization errors started
at 15:00:17. Temporal deadlock from 15:07:13. FCE's `maas root machine read demn38` hung
for 773 seconds (15:15:04–15:27:59). OAuth threshold: 300s; actual elapsed: 773s. Exit
code 2. Juju controller status at end: `controller/0` and `controller/1` in `error/lost`
state (consistent with infra node overload affecting KVM-hosted VMs on the same hosts).

**Second occurrence — run 25442241821 (UUID 4ec765e5-3cbb-4b2c-92f0-f712e63585fb,
tor3-sqa-virtual_maas cluster_1, MAAS 3.5.12-16413-g.7fb94f378, main, 2026-05-06):**

Six magpie nodes (node1–6, system IDs wfxywm/g3xkde/pgf3dp/mxa7bx/pxcq6x/cpbfba) deployed
concurrently. A burst of 71 Curtin status callbacks from `pxcq6x` at 16:58 (15+ POSTs in
2 seconds) preceded progressively degrading machine-read response times: `mxa7bx` 91s,
`wfxywm` 93s, then `g3xkde` hung for **663 seconds** (17:00:19–17:11:23). OAuth threshold:
300s; actual elapsed: 663s. Exit code 2. The full MAAS Temporal/PostgreSQL syslog was not
available (infra2 syslog sparse during that window; infra1/3 syslog absent), so direct
Temporal deadlock evidence is not confirmed in logs — but the escalating response-time pattern
and matching MAAS version/substrate are consistent with the same mechanism. All 6 nodes
remained `Deploying` at failure; no hardware errors.

---

## Notes

- Deploys are issued sequentially per network space; any space can be the one that fails
- The Juju controller HA nodes' internal logs are not available in Swift artifacts
- `temporal-server` errors in MAAS infra syslog can be either background noise **or** the
  root cause of machine-read hangs (Pattern F). Check whether the errors correlate with
  FCE polling slowdowns and whether a DB transaction timeout cascade follows.
- For `dedicated_maas`, the MAAS logs bundle may contain only binary systemd journal files
  (no `/var/log/syslog`); if the journal is truncated, lower-level ephemeral failure details
  may not be determinable from available artifacts

