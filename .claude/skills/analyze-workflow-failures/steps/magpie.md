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
grep -i "error\|failed\|cannot add" /tmp/<uuid>/generated/magpie/log.txt

# Check which juju deploy calls succeeded vs failed
grep "Deploying magpie\|Command failed\|cannot add" /tmp/<uuid>/generated/magpie/log.txt

# Check for snap refreshes on infra hosts around the failure window
grep "2026-<date>T<HH>:" /tmp/maas-logs-*/*/var/log/syslog | grep -i "snap\|refresh\|juju"

# Check for VM-level events on infra hosts
grep "2026-<date>T<HH>:" /tmp/maas-logs-*/*/var/log/syslog \
  | grep -v "kernel\|audit\|apparmor\|named\|#011\|maas\|temporal\|haproxy\|nginx"
```

## Known Failure Patterns

### Pattern A: Juju controller MongoDB "not master and slaveOk=false"

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

### Pattern B: Physical node stuck in "Failed deployment: Loading ephemeral" → 30-minute timeout

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

### Pattern C: All nodes stuck in `Deploying` — slow simultaneous OS installation exceeds 30-min timeout

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

### Pattern D: All nodes stay `Ready` — MAAS rejects deploy with 400 (distro_series not available)

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

## Notes

- Deploys are issued sequentially per network space; any space can be the one that fails
- The Juju controller HA nodes' internal logs are not available in Swift artifacts
- `temporal-server` errors seen in MAAS infra syslog around the same time are unrelated
  background noise from the infra node services
- For `dedicated_maas`, the MAAS logs bundle may contain only binary systemd journal files
  (no `/var/log/syslog`); if the journal is truncated, lower-level ephemeral failure details
  may not be determinable from available artifacts

## Version History

- **v1.0** (2026-03-25): Initial version — MongoDB "not master" pattern from run 23512120069
  (UUID c6a6fdfd, tor3-sqa-virtual_maas cluster_3)
- **v1.1** (2026-04-08): Added Pattern B — "Failed deployment: Loading ephemeral" on physical
  node `duosion` (68dntx, zone1) during `deploy_maas_machines`; 4/5 nodes deployed; 30-min
  FCE timeout; no curtin_config for failing node; MAAS journal truncated; from run
  24111787100 (UUID b881ccd8, tor3-sqa-dedicated_maas dh1_j2, main, 2026-04-08).
- **v1.3** (2026-04-08): Added Pattern D — all 4 nodes remain `Ready` throughout (never `Deploying`); Juju provisioner receives `400 Bad Request: 'jammy' is not a valid distro_series` from MAAS deploy API; MAAS boot source had Jammy selection but sync only produced Noble images; not retryable without importing Jammy image first; from run 24120449876 (UUID 00a49a3b, tor3-sqa-virtual_maas, main, 2026-04-08, SKU master-magpie-snap-jammy-mixed).
