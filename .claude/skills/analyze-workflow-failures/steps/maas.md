# Step Knowledge: maas

## Step Overview

The `maas` layer deploys and configures the virtual MAAS infrastructure: it creates KVM VMs
(via virsh on a libvirt host), commissions them in MAAS, configures networking/interfaces,
deploys them (with `install_kvm=True` or `register_vmhost=True` for KVM hypervisor nodes),
and waits for all nodes to reach `Deployed` state. It is the foundation layer for all
subsequent virtual_maas pipeline steps.

Relevant MAAS sub-steps (FCE `configuremaas.py`):
- `maas:configure_nodes` — interface and subnet setup
- `maas:deploy_kvm_nodes` — issues `maas … machine deploy … install_kvm=True/register_vmhost=True`
- `_wait_for_deployed` — polls MAAS every ~30s, raises after timeout if any node stays `Deploying`

## Swift Artifacts

| Path | Description | When to check |
|---|---|---|
| `generated/maas/log.txt` | Short FCE build log for the MAAS layer | Always — first stop |
| `generated/maas/logs-<timestamp>.tgz` | Full per-node MAAS infrastructure log archive | Deep investigation |
| `generated/maas/maas-api` | MAAS API URL | Cross-check infra node IPs |
| `generated/lastlines.txt` | Tail of all log streams | Quick triage |

## Key Log Files (inside tgz archive)

The archive contains per-infra-node directories (e.g. `10.241.144.2/`, `10.241.144.3/`,
`10.241.144.4/` mapped to `infra1`, `infra2`, `infra3`).

| File | What it contains | When to use |
|---|---|---|
| `var/log/syslog` | Everything: systemd, snapd, MAAS, AppArmor | Primary — check infra1 first |
| `var/log/maas/var.lib.maas.log` | MAAS service log (filtered from syslog) | MAAS-specific events |
| `var/snap/maas/common/log/snap-perms.txt` | Snap version / file permissions | Identify snap revision |
| `node<N>.*.solutionsqa-curtin_config.txt` | Curtin preseed for each target node | APT proxy, cloud-config |

## Grep Patterns

```bash
# Status transitions for failing nodes
grep "Status transition" <work_dir>/maas-logs/10.241.144.2/var/log/syslog | grep -i "deploy"

# AppArmor virsh denials (pkttyagent / syscall)
grep "apparmor.*DENIED.*virsh\|DENIED.*pkttyagent" <work_dir>/maas-logs/10.241.144.2/var/log/syslog

# Snap installation / refresh event
grep "Installing snap\|auto-refresh\|snap-maas-" <work_dir>/maas-logs/10.241.144.2/var/log/syslog | head -20

# Curtin callbacks from target nodes (shows installation progress)
grep "POST /MAAS/metadata/status/<system_id>" <work_dir>/maas-logs/10.241.144.2/var/log/syslog

# Pacemaker VIP warnings (HA instability)
grep "Unexpected result.*not running.*res_" <work_dir>/maas-logs/10.241.144.2/var/log/syslog | head -10

# Node reaching installed OS (post-curtin boot)
grep "maas-machine.*\[node[0-9]\].*cloud-config\|cloud-init.*finished" <work_dir>/maas-logs/10.241.144.2/var/log/syslog
```

## Known Failure Patterns

### Pattern 1: AppArmor virsh denial blocks KVM host registration → nodes stuck in Deploying

**Symptom (GitHub Actions log):**
```
foundationcloudengine.layers.configuremaas WARNING node5…: Deploying
foundationcloudengine.layers.configuremaas WARNING node6…: Deploying
…
Exception: Not all KVM hosts were deployed in time.
##[error]Process completed with exit code 1.
```
Preceded by 30 minutes of 30-second poll entries showing `{'node5': 'Deploying', 'node6': 'Deploying'}`.

**Root cause:** MAAS snap revision 41649's AppArmor profile for `snap.maas.pebble` denies
`virsh` from executing `/usr/bin/pkttyagent` (polkit agent), and also blocks certain
syscalls (`sys_getuid` / `sys_geteuid`) via seccomp. When MAAS attempts to register
newly-deployed nodes as VM hosts (the post-install `install_kvm` / `register_vmhost` step),
it invokes `virsh` from within the snap confinement. The AppArmor denial causes virsh to
fail silently, so MAAS never completes the VM-host registration and never transitions the
nodes from `DEPLOYING` to `DEPLOYED`. FCE's `_wait_for_deployed` times out after 30 minutes.

The nodes themselves boot fine — curtin installs Ubuntu, the nodes reboot into the installed
OS (visible in syslog as normal `cloud-config.service`, `sysstat-collect` etc.), but MAAS
never receives the registration confirmation it needs to mark them deployed.

**Evidence to look for:**

- `var/log/syslog` (infra1): `apparmor="DENIED" operation="exec" … profile="snap.maas.pebble" name="/usr/bin/pkttyagent" … comm="virsh"`
- `var/log/syslog` (infra1): `syscall=122` / `syscall=123` (sys_getuid/sys_geteuid) seccomp violations for `virsh` inside `snap.maas.pebble`
- `var/log/syslog` (infra1): `snapd[…]: api_snaps.go:536: Installing snap "maas" revision unset` — snap newly installed just before the pipeline run (not an auto-refresh of an existing install)
- `var/log/syslog` (infra1): no `maas.node: [info] node5: Status transition from DEPLOYING to DEPLOYED` entry
- `var/log/syslog` (infra1): target node logs show normal post-install OS activity (sysstat-collect cron, update-notifier) while MAAS still polls them as `Deploying`

**Timing:**
- Snap installed: ~23:53
- Deploy commands issued: ~00:15:52 / 00:16:05
- First Deploying poll: ~00:16:21
- FCE timeout: 00:46:35 (30 minutes)

**Known affected revisions:** snap.maas rev 41649 and rev 41764 (channel `3.6/beta/release-prep`)

**⚠️ Substrate-specific reliability of this indicator:**

On **dedicated_maas**, AppArmor virsh/pkttyagent denials are a reliable signal — confirmed as the genuine root cause in runs 5dd2bf63 and fde2213c.

On **virtual_maas**, these same denials appear in **every run, including passing ones** (144+ occurrences is normal). Seeing them in a failing virtual_maas run is almost always coincidental noise. Do **not** conclude Pattern A on virtual_maas from AppArmor denials alone — you must confirm:
1. Nodes are stuck in `Deploying` (not `unknown`, not PXE-looping)
2. No other failure mechanism explains the stuck state (Temporal crash, SSH tunnel, vendor-data issue, etc.)
3. The AppArmor denials correlate *temporally* with the virsh VM-host registration step, not scattered throughout the run

In multiple virtual_maas failure analyses (1078296e, 3830828c, 8e226262, 473c2885), AppArmor denials were explicitly confirmed as benign with the actual root cause being something else entirely.

---

### Pattern 2: MAAS BMC deduplication causes self-deletion → `taillow` stuck in `unknown`

**Symptom (GitHub Actions log):**
```
foundationcloudengine.layers.configuremaas DEBUG Current states:
  {'azurill': 'Ready', 'duosion': 'Ready', ..., 'taillow': 'unknown'}
…
Exception: Nodes did not reach Ready after 1801 seconds.
##[error]Process completed with exit code 1.
```
One specific node shows `unknown` from the very first poll after all other nodes begin
commissioning. The affected node never transitions to `New` or `Commissioning` at any point.

**Root cause:** FCE's machine recreation pattern is: call `machines create` → then
`machine delete <old_id>`. When `machines create` is called with a BMC address
(`power_parameters_power_address`) that already exists in MAAS, MAAS resolves the request
against the existing machine record and returns its system ID — it does NOT create a new,
distinct record. FCE then deletes `<old_id>` (the "old" system ID stored from the initial
`machines read`), which is the same record MAAS just returned from the create call. This
removes the only machine record for that hostname from MAAS entirely. FCE's subsequent polls
see the machine as `unknown` (not found) for the entire 30-minute timeout.

**Evidence to look for:**

- GitHub Actions log: `machine delete <old_id>` issued ~10–15 seconds after
  `machines create hostname=<node>` for the affected node
- GitHub Actions log: affected node shows `unknown` from the very first post-creation poll,
  while all other nodes progress through `New → Commissioning → Ready`
- MAAS syslog (noma): `<node>: Deleting node` fires seconds after
  `<node>: Commissioning started`
- MAAS HTTP log (noma): only ONE `DELETE /MAAS/api/2.0/machines/<old_id>/` exists for
  the affected node — no separate old/new system IDs

**From run 23791511796 (UUID a25bcd5f, tor3-sqa-dedicated_maas dh1_j2, 2026-03-31):**

`taillow` (BMC `10.240.21.54`, system ID `p3xmgq`) was self-deleted:
```
10:52:11 – FCE reads existing machines; old taillow system ID = p3xmgq
10:53:20 – machines create hostname=taillow BMC=10.240.21.54
           → MAAS returns p3xmgq (existing record, BMC already registered)
10:53:24 – taillow: Enlisted new machine → NEW to COMMISSIONING
10:53:32 – taillow: Commissioning started
10:53:33 – machine delete p3xmgq  ← FCE deletes "old" taillow = the only taillow
10:53:34 – taillow: Deleting node + Deleting my BMC '8 (10.240.21.54:type=STICKY)'
10:54:09–11:24:40 – taillow: unknown (30 min × 30s polls)
```

All other 6 nodes succeeded: MAAS assigned them fresh system IDs from their create calls,
so the old IDs stored in FCE's initial read were different from the new IDs returned.

**Distinguishing from Pattern A (AppArmor):**

| | Pattern A | Pattern B |
|---|---|---|
| Node state | Stuck in `Deploying` | Stuck in `unknown` |
| Phase | Deploy phase (after commissioning) | Enlist/commission phase |
| MAAS log | AppArmor DENIED virsh | `Deleting node` seconds after `Commissioning started` |
| Affected nodes | All KVM host nodes | One specific node with a shared/reused BMC IP |

---

### Pattern 3: Curtin `install_kernel` fails — `apt-get update` makes no network requests → `linux-generic` unfindable

**Symptom (GitHub Actions log):**
```
foundationcloudengine.layers.configuremaas WARNING node5…: Deploying
foundationcloudengine.layers.configuremaas WARNING node6…: Deploying
…
Exception: Not all KVM hosts were deployed in time.
##[error]Process completed with exit code 1.
```
Both nodes PXE-loop for the full 30-minute timeout. Unlike Pattern A (where nodes
boot into the installed OS), here nodes loop back to PXE on every boot cycle because
curtin never completes and never calls `netboot_off`.

**Root cause:** During curtin's `install_kernel` curthook, `apt-get update` runs with
`--option=Dir::Etc::sourcelist=/tmp/tmpsyktkyxm/sources.list` but makes **zero network
requests** (confirmed by absence of Squid proxy log entries for the deploying node during
the ~250ms window). The freshly-extracted Noble squashfs target has no cached
`/var/lib/apt/lists/`, so after the no-op update the package database is empty.
`apt-get install linux-generic` immediately fails with "E: Unable to locate package
linux-generic" (exit code 100). Curtin exits without running `late_commands`, the
`wget … op=netboot_off` call never fires, MAAS never flips the boot order, and nodes
continue PXE booting.

The most likely mechanism: Curtin creates a temp sources.list inside the chroot
(`/tmp/tmpsyktkyxm/sources.list`) with repository content for the `apt-get update` call.
If this file is empty, contains a literal `$RELEASE` (unsubstituted), or has an
unreachable URL, APT exits immediately with no network traffic. Consistent with the
observed log: `got primary mirror: None / got security mirror: None` during
`writing-apt-config` indicates the target's existing sources.list was empty/absent.

**Key distinguishing evidence:**
- All curthook stages up to and including `configuring-mdadm-service` succeed
- `install_kernel` fails: `apt-get update` produces only `Reading package lists...` in
  < 50ms, then `apt-get install linux-generic` immediately fails
- **Zero Squid proxy entries** for the node IP between the ZFS pre-curtin phase and the
  second PXE boot cycle — confirmed on all infra nodes
- Total `install_kernel` stage wall time ≈ 246ms (far too fast for any network I/O)
- Nodes show repeated DHCP PXE requests (every ~90s) throughout the 30-minute window
- No AppArmor denials, no snap activity, no MAAS status transitions

**Evidence to look for:**

```bash
# Find install_kernel FAIL in syslog
grep "installing-kernel: FAIL\|Unable to locate package" <work_dir>/maas-logs/10.241.144.2/var/log/syslog

# Confirm zero squid requests during apt-get update window
grep "<node_ip>" <work_dir>/maas-logs/10.241.144.3/var/log/syslog | grep "squid" | grep "<timestamp_window>"

# Curtin traceback in node console
grep "\[node5\].*Traceback\|\[node5\].*ProcessExecutionError\|\[node5\].*install_kernel" <work_dir>/maas-logs/10.241.144.2/var/log/syslog
```

**Timing from run 23910974753 (UUID f8251919, cluster_6, 2026-04-02):**
- 17:43:06/14 — Deploy commands issued (node5/q686c3, node6/spn4d3)
- 17:44:11 — Curtin starts on node5
- 17:44:11–17:44:57 — All partitioning, network, extract, early curthooks: SUCCESS
- 17:44:57 — `install_kernel`: `apt-get update` (no network), `linux-generic` unfindable
- 17:44:57 — Curtin exits without calling `netboot_off`
- 17:45:27 — Node5 second PXE boot begins (partitions from cycle 1 visible)
- 18:13:27 — FCE 30-minute timeout

**Environment:** tor3-sqa-virtual_maas cluster_6, Curtin `23.1.1-1124-g7324b43b`, noble squashfs `20260223`.

Second confirmed occurrence: run 23936994907 (UUID 72dc244a, tor3-sqa-virtual_maas cluster_5, 2026-04-03). Same Curtin version; squashfs `5f64c83` (`ga-24.04/noble/stable`). Both node5 (egk8km) and node6 (r6dabc) cycled through 17 PXE-loop cycles (~2 min each) over 30 minutes. `apt-get update` inside the chroot ran in ~64ms with only "Reading package lists..." output; no Squid proxy entries found for node5 IP (10.241.144.81) in the infra1 syslog. `apt-get install --download-only linux-generic` started at 07:52:03.548Z, node rebooted at 07:52:38Z — consistent with immediate failure (empty package DB). Concurrent AppArmor DENIED for `snap.maas.pebble` exec `virsh→pkttyagent` (rev 41764) also present, but secondary to the curtin failure.

---

### Pattern 4: Curtin `installing-missing-packages` fails — EFI boot packages unfindable → Temporal retries exhaust FCE timeout

**Symptom (GitHub Actions log):**
```
foundationcloudengine.layers.configuremaas WARNING suicune…: Deploying
foundationcloudengine.layers.configuremaas WARNING pangoro…: Deploying
…
Exception: Not all KVM hosts were deployed in time.
##[error]Process completed with exit code 1.
```
Both KVM host nodes show `Deploying` for the full 30-minute poll window. Unlike Pattern C
(PXE-loop), nodes do PXE-boot repeatedly but curtin fails before reaching `netboot_off`.

**Root cause:** Curtin's `installing-missing-packages` curthook attempts
`apt-get install --download-only efibootmgr grub-efi-amd64 grub-efi-amd64-signed shim-signed`
inside the target chroot. The noble deploy squashfs has an empty `/var/lib/apt/lists/`
directory (no APT package index cached). This step does **not** run `apt-get update` first;
it relies on pre-populated lists from the squashfs. With an empty database, all four EFI
packages are unfindable (exit code 100) in ~200ms. Curtin aborts at curthooks.

Unlike Pattern C (`install_kernel`), this failure occurs one step earlier in the curthooks
pipeline — before any `apt-get update` is attempted. The underlying cause is the same:
the noble squashfs carries an empty APT package index.

The Temporal `DeployWorkflow` retries the deployment on each failure. Each retry takes
~5 minutes (PXE boot + partitioning + extract + early curthooks → same failure). After
4–5 retry cycles the 30-minute FCE timeout is reached with both nodes still `Deploying`.

**Key distinguishing evidence:**
- Curthook failure at `installing-missing-packages` (not `install_kernel` as in Pattern C)
- Missing packages: `efibootmgr`, `grub-efi-amd64`, `grub-efi-amd64-signed`, `shim-signed`
- `apt-get install --download-only` exits in <200ms (confirms empty package DB, no network)
- Multiple Curtin PID re-starts visible in syslog (e.g. PID 1532 → 1538 → 1542) — each is a Temporal retry
- Nodes successfully PXE-boot, partitioning and extract stages complete (`stage_extract took 39–44 seconds`), then curthooks fails immediately
- No `netboot_off` wget ever fires; nodes remain in `Deploying`

**Evidence to look for:**

```bash
# Confirm installing-missing-packages failure in syslog
grep "installing-missing-packages: FAIL\|Unable to locate package efibootmgr" \
  <work_dir>/maas-logs/10.241.128.2/var/log/syslog

# Confirm multiple Curtin restart PIDs (Temporal retries)
grep "\[suicune\].*start: cmd-install:" <work_dir>/maas-logs/10.241.128.2/var/log/syslog | head -10

# Verify <200ms apt-get duration
grep "\[suicune\].*cloud-init\[1532\].*Reading package lists\|Installing packages on target" \
  <work_dir>/maas-logs/10.241.128.2/var/log/syslog
```

**Timing from run 23969669421 (UUID 16f351cf, dh1_j2, 2026-04-04):**
- 04:01:28/40 — Deploy commands issued (pangoro mqpnn6, suicune p4bapc)
- 04:05:08/09 — First Curtin starts (pangoro PID 1590, suicune PID 1532)
- 04:06:33/45 — `installing-missing-packages` FAIL (efibootmgr etc. not found), exit 100
- 04:09+/04:10:35 — Second Temporal retry starts (pangoro PID 1586, suicune PID 1538)
- 04:33+ — Third attempt still running when FCE timed out (pangoro PID 1596, suicune PID 1542)
- 04:32:18 — FCE 30-minute timeout raised

**Environment:** tor3-sqa-dedicated_maas dh1_j2, MAAS 1:3.6.4-17623-g.46a516275, Curtin `23.1.1-1124-g7324b43b`, noble squashfs `5f64c83` (ga-24.04/noble/stable). Nodes: suicune (p4bapc, LVM layout) and pangoro (mqpnn6, bcache layout).

---

### Pattern 5: Ubuntu security mirror package replacement race → `python3-django` 404 → `maas-region-controller` install fails

**Symptom (GitHub Actions log):**
```
E: Failed to fetch http://security.ubuntu.com/ubuntu/pool/main/p/python-django/python3-django_4.2.11-1ubuntu1.15_all.deb  404  Not Found [IP: 91.189.91.83 80]
E: Unable to fetch some archives, maybe run apt-get update or try with --fix-missing?
subprocess.CalledProcessError: Command '['ssh', ..., 'ubuntu@10.241.144.2', '--', 'sudo',
  "bash --login -c 'DEBIAN_FRONTEND=noninteractive apt-get -q install -y maas-region-controller'"]'
  returned non-zero exit status 100.
##[error]Process completed with exit code 1.
```
The failure occurs in `maas:maas_install` — the sub-step that installs `maas-region-controller`
onto the infra nodes via `apt-get`. All other packages (including the MAAS PPA packages) succeed;
only one `noble-security` dependency returns HTTP 404.

**Root cause:** Ubuntu's security mirror was in the middle of publishing a new version of the
`python-django` source package. The CDN at `security.ubuntu.com` removed the old `.deb`
(`python3-django_4.2.11-1ubuntu1.15_all.deb`) while FCE's `apt-get install` download was in
progress. The local apt index (fetched seconds earlier by `apt-get update`) still referenced the
old version, causing apt to attempt a fetch of a file that no longer existed on the CDN → HTTP 404
→ apt exit code 100.

This is a transient **Ubuntu security mirror package replacement race condition**. It occurs when:
1. `apt-get update` caches a Packages index referencing version `N` of a package
2. The Ubuntu security team publishes version `N+1` and removes the `.deb` for `N` from the CDN
3. `apt-get install` runs moments later and cannot find the old `.deb`

**Distinguishing characteristics:**
- Only a **single package** fails with 404 — all others (including larger packages) succeed
- The failing package is from `noble-security` (not the PPA or ubuntu/main)
- apt exit code is **100** (unresolved dependency), not 1 or 255
- No MAAS snap refresh, no AppArmor denial, no connectivity issue — other `noble-security`
  packages fetch fine
- `apt-get update` ran only seconds before the install command (so the index is not stale in
  the traditional sense)

**From run 24111793277 (UUID fc17932b, tor3-sqa-virtual_maas cluster_7, 2026-04-08, branch main):**
```
01:31:43 – apt-get update on all three infra nodes (index cached)
01:31:47 – apt-get install maas-region-controller on infra1 (10.241.144.2)
01:33:16 – Err:1 python3-django_4.2.11-1ubuntu1.15 → 404 Not Found
           110 other packages fetched successfully (80.9 MB in 89s at 914 kB/s)
           apt exits code 100 → FCE CalledProcessError
```

**Recovery:** Re-trigger the pipeline. By the time the retry runs, the CDN will have stabilised
with the new package version. No infrastructure change is required.

**Code fix:** FCE's `install_packages()` could retry with `apt-get update && apt-get install
--fix-missing` when exit code 100 is detected, to pick up the replacement version automatically.

---

### Pattern 6: Boot resource import fails (internal mirror unreachable) → `machines create` rejected with empty architecture list

**Symptom (GitHub Actions log):**
```
root ERROR [localhost] Command failed: maas root machines create hostname=node1 power_type=virsh
  architecture=amd64/generic mac_addresses=52:54:56:58:03:01 ...
{"architecture": ["'amd64/generic' is not a valid architecture.  It should be one of: ''."]}

subprocess.CalledProcessError: Command '['maas', 'root', 'machines', 'create', ...]'
  returned non-zero exit status 2.
##[error]Process completed with exit code 1.
```
The failure occurs in `maas:enlist_nodes` — the first `machines create` call is rejected
immediately (no timeout, no polling). The MAAS API response contains an **empty** architecture
list (`'It should be one of: ''.`), which means the region's PostgreSQL `BootResource` table
has no entries at all. All preceding steps succeed, including `configure_maas` (which
superficially reports `{'synced'}`).

**Root cause:** FCE's `configure_maas` step updates the MAAS boot source URL to an internal
Canonical image mirror/proxy (e.g., `http://10.141.186.167/maas/images/ephemeral-v3/stable/`).
This internal IP is **not routable** from the freshly-deployed infra KVM VMs, so every boot
resource import attempt fails with `[Errno 113] No route to host`. MAAS's PostgreSQL DB never
receives any boot resources.

FCE's readiness check (`boot-resources is-importing`) is fooled by stale data: when the import
flag returns `false`, FCE calls `list_boot_images` on each rack controller. The rack controllers
still have ubuntu/noble/amd64 files in their TFTP directories from **previous pipeline runs** on
the same cluster, so they report `status: synced`. FCE logs `{'synced'}` and proceeds — but
`list_boot_images` returns rack-controller TFTP state, NOT the region DB state. When
`machines create` later validates `architecture=amd64/generic` against `BootResource.objects.all()`,
it gets an empty result set.

**Evidence to look for:**

- GitHub Actions log: `maas root boot-source update 1 url=http://<internal_IP>/maas/images/...`
  early in `configure_maas`
- GitHub Actions log: `Boot resources still importing, sleeping 30 seconds` — many iterations,
  then suddenly `is-importing` returns false without FCE logging a success/failure distinction
- MAAS syslog (infra1): `maasserver.bootresources: [critical] Importing boot resources failed.`
  with traceback ending in `urllib.error.URLError: <urlopen error [Errno 113] No route to host>`
- MAAS syslog (infra1): `maas.bootsources: [error] Failed to import images from http://<internal_IP>/...`
- MAAS API response at `machines create`: `{"architecture": ["'amd64/generic' is not a valid architecture.  It should be one of: ''."]}` — empty list

```bash
# Confirm unreachable boot source in GitHub Actions log
grep "boot-source update" <work_dir>/run_<id>_failed.log

# Confirm CRITICAL import failure in syslog
grep "Importing boot resources failed\|No route to host" \
  <work_dir>/maas-logs/10.241.144.2/var/log/syslog | grep "Apr  8 0[78]:"

# Confirm region DB has no boot resources (cross-check)
# The machines create MAAS API response will have "It should be one of: ''."
grep "is not a valid architecture" <work_dir>/run_<id>_failed.log
```

**Timing from run 24122101622 (UUID 01dd77f9, tor3-sqa-virtual_maas cluster_6, 2026-04-08):**
```
07:46:58 – FCE sets boot source: maas root boot-source update 1 url=http://10.141.186.167/maas/images/ephemeral-v3/stable/
07:47:01 – FCE triggers: maas root boot-resources import
07:47:04 – FCE starts polling is-importing (every 30s)
08:00:47 – MAAS [warn] Failed to synchronise boot resources: Child Workflow execution timed out
08:00:50 – MAAS [error] Failed to import images from http://10.141.186.167/...: No route to host
08:00:54 – MAAS [critical] Importing boot resources failed.
08:01:22 – FCE: is-importing → false; reads boot sources, adds focal/jammy selections
08:01:37 – FCE triggers second import
08:01:46 – MAAS [critical] Importing boot resources failed. (second time)
08:02:13 – FCE: is-importing → false; calls list_boot_images on rack controllers
08:02:31 – FCE logs {'synced'} — falsely concludes readiness from stale TFTP cache
08:07:59 – machines create node1 architecture=amd64/generic → 400 (empty arch list)
```

**MAAS version:** 3.6.4-17624-g.e460934c2 (snap revision 41799)

**Distinguishing from other maas patterns:**

| | Pattern F | Patterns C/D |
|---|---|---|
| Failed sub-step | `maas:enlist_nodes` | `maas:deploy_kvm_nodes` or `_wait_for_deployed` |
| Error message | `'amd64/generic' is not a valid architecture` | `Unable to locate package`, nodes stuck `Deploying` |
| Phase | Node enlistment (before commissioning) | Node deployment (after commissioning) |
| Root cause | Boot resource DB empty (import failed) | Curtin APT failure during OS install |

**Recovery:** Retry the pipeline run. The root cause (unreachable mirror) is transient — if
network routing to the internal mirror is restored, the boot resource import will succeed on
the next attempt. Alternatively, if the mirror is permanently unreachable from this cluster,
the boot source URL needs to be updated to use `http://images.maas.io/ephemeral-v3/stable/`
instead.

---

### Pattern 7: Internal boot source mirror provides incomplete catalog → noble commissioning image deleted → all nodes immediately `FAILED_COMMISSIONING`

**Symptom (GitHub Actions log):**
```
foundationcloudengine.layers.configuremaas DEBUG Current states:
  {'node1': 'Failed commissioning', 'node2': 'Failed commissioning', ..., 'node6': 'Failed commissioning'}
…
Exception: Nodes did not reach Ready after 1801 seconds.
##[error]Process completed with exit code 1.
```
All 6 nodes show `Failed commissioning` from the **very first** poll (~90 seconds after
being created), unlike deploy-phase failures which take 30 minutes. `machines create` itself
succeeds (architecture is accepted) — commissioning starts but immediately fails.

**Root cause:** The `maas_boot_sources` addon changes the MAAS boot source URL to an
internal Canonical mirror (e.g., `http://10.141.186.167/maas/images/ephemeral-v3/stable/`).
Unlike Pattern F where this mirror is unreachable, here the mirror IS reachable and provides
a new image catalog. However, MAAS's import reconciliation process deletes the existing
noble (Ubuntu 24.04) commissioning squashfs from the region DB (because the new catalog
references different image hashes). If the new catalog from the internal mirror does not
include the `ubuntu/amd64/no-such-kernel/noble` commissioning image, MAAS loses the
ability to commission noble nodes.

When nodes PXE-boot to start commissioning, MAAS's TFTP server calls `GetBootConfig` on
the region. The region returns `Unknown Error` because it cannot find
`ubuntu/amd64/no-such-kernel/noble`, and each node is immediately marked
`FAILED_COMMISSIONING`. FCE's `list-boot-images` check returns `{'synced'}` (from rack
controllers that pulled other new images) and does not catch the missing noble image.

**Evidence to look for:**

- GitHub Actions log: `maas root boot-source update 1 url=http://<internal_IP>/maas/images/...`
  early in `configure_maas`
- infra syslog: `maas.bootresources: [info] Importing images from source: http://<internal_IP>/...`
  — confirms mirror is reachable
- infra syslog: `maas_nginx: ... "GET /MAAS/boot-resources/<old_noble_hash>/ HTTP/1.1" 404`
  — noble squashfs deleted from DB during reconciliation (~2–3 min after import triggered)
- infra syslog: `maas.rpc.boot: [warn] failed to compute a bootable amd64/no-such-kernel system for ubuntu/noble`
- infra syslog: `maas.node: [error] nodeN: Marking node failed: Missing boot image ubuntu/amd64/no-such-kernel/noble.`
- infra syslog: `maas-rackd: [critical] TFTP back-end failed.` / `UnhandledCommand: Unknown Error [infra2:cmd=GetBootConfig]`
- GitHub Actions log: `{'synced'}` logged before enlistment — FCE falsely concludes readiness

```bash
# Confirm commissioning failure reason in syslog
grep "Marking node failed\|no-such-kernel\|GetBootConfig\|TFTP back-end" \
  <work_dir>/maas-logs/10.241.144.2/var/log/syslog | grep "18:0[234]:"

# Confirm noble squashfs deleted (404) during import reconciliation window
grep "boot-resources.*404" <work_dir>/maas-logs/10.241.144.*/var/log/syslog | grep "17:4[789]:"
```

**Timing from run 24090300838 (UUID 16c8a59a, tor3-sqa-virtual_maas cluster_1, 2026-04-07):**
```
17:45:03 – Previous import completed; noble squashfs 5f64c83 present in region DB
17:46:31 – FCE sets boot source: maas root boot-source update 1 url=http://10.141.186.167/...
17:46:37 – MAAS downloads new catalog from internal mirror (reachable)
17:46:38 – Importing images from source: http://10.141.186.167/...
17:49:18 – GET /MAAS/boot-resources/5f64c83/ → 404 (noble squashfs deleted from DB)
17:54:28 – FCE logs {'synced'} — false positive from rack-controller TFTP cache
18:02:04 – machines create node1 → accepted (arch valid: 200)
18:02:08 – node1: Status transition from NEW to COMMISSIONING
18:02:24 – maas.rpc.boot: failed to compute amd64/no-such-kernel for ubuntu/noble
18:02:24 – node1: Status transition from COMMISSIONING to FAILED_COMMISSIONING
18:03:22 – node3: FAILED_COMMISSIONING (same reason)
18:03:50 – node4: FAILED_COMMISSIONING
18:05:09 – node6: FAILED_COMMISSIONING
18:05:36 – First FCE poll: all 6 nodes 'Failed commissioning'
18:35:49 – FCE 30-minute timeout raised
```

**Distinguishing from Pattern F:**

| | Pattern G (this) | Pattern F |
|---|---|---|
| Mirror reachable? | ✅ Yes | ❌ No (`No route to host`) |
| Region DB after import | Partially populated — noble image missing | Completely empty |
| `machines create` | ✅ Accepted | ❌ Rejected (empty arch list) |
| Failure phase | Commissioning (16s after enlisting) | Enlistment |
| Error | `Missing boot image ubuntu/amd64/no-such-kernel/noble` | `'amd64/generic' is not a valid architecture. It should be one of: ''` |

**Environment:** tor3-sqa-virtual_maas cluster_1, MAAS 3.6.4-17624-g.e460934c2,
snap rev 41799, ADDON `maas_snap_nehjoshi5_maas-3.6-next`, features: `maas_boot_sources`.

---

### Pattern 8: `install_kvm=True` deploy — cloud-init receives vendor-data but does not install libvirt → no 2nd netboot-finished → node stuck in Deploying

**Symptom:** Node deployed with `install_kvm=True` transitions through curtin successfully
(1st `netboot-finished` received) but never reaches Deployed. Cloud-init runs, fetches
vendor-data, and sends a final status POST — but libvirt is never installed.

**FCE error:**
```
##[error]Process completed with exit code 1.
# FCE maas layer times out polling: {'node5': 'Deploying', 'node6': 'Deployed'}
# (no explicit error — silent 30-minute timeout)
```

**KVM deploy two-signal flow:**
1. Curtin installs Ubuntu, calls `op=netboot_off` → 1st `netboot-finished` to Temporal worker
2. Node reboots → iPXE receives 299-byte local-boot config
3. Ubuntu boots → cloud-init fetches `/MAAS/metadata/2012-03-01/meta-data/vendor-data`
4. vendor-data should contain `packages: [libvirt-daemon-system, ...]` + `package_upgrade: true`
5. Cloud-init installs libvirt → MAAS detects it → 2nd `netboot-finished` → node Deployed

In this pattern, step 4 fails silently: vendor-data is served (HTTP 200) but does not trigger
package installation. Cloud-init finishes in ~32 seconds with zero `.deb` downloads.

**Distinguishing evidence:**

```bash
# 1. Temporal worker receives only ONE netboot-finished for the failing node
grep "netboot-finished\|deploy:tgdgmm\|deploy:<system_id>" <work_dir>/maas-logs/*/var/log/syslog \
  | grep "temporal-worker"
# Expect: exactly 1 entry in failing run; 2 entries in passing run

# 2. Squid log shows apt-update but ZERO .deb downloads from node5 IP
grep "10.241.144.81" <work_dir>/maas-logs/*/var/log/syslog | grep "squid.*GET.*deb"
# Expect: 0 results in failing run; 50+ MB of .deb downloads in passing run

# 3. Cloud-init finishes in ~32s (vendor-data fetch to final status POST)
grep "metadata.*vendor-data\|POST.*metadata.*status.*tgdgmm" <work_dir>/maas-logs/*/var/log/syslog \
  | grep "maas-regiond"
# Short gap (32s) = packages module skipped; long gap (2+ min) = packages installed

# 4. Vendor-data byte count comparison (same size does not mean same content)
grep "vendor-data" <work_dir>/maas-logs/*/var/log/syslog | grep "200"
# Both failing and passing runs return 1101 bytes — actual content differs but is unconfirmable
# from HTTP logs (MAAS does not log response bodies at INFO level)
```

**Root cause:** MAAS generates vendor-data dynamically from machine DB state. Despite FCE
invoking `maas root machine deploy <system_id> install_kvm=True`, the vendor-data served at
reboot lacks `packages: [libvirt-daemon-system, ...]`. This is intermittent — other runs on
the same cluster and MAAS revision succeed — suggesting a race condition or transient DB state
in MAAS 3.6.4 where the `install_kvm` flag is not reflected at vendor-data generation time.

**Confirmed NOT AppArmor**: `snap.maas.pebble` → virsh → pkttyagent denials appear in ALL
runs, including passing runs with 144+ occurrences. Completely benign.

**Runs:** 24117574587 (UUID 3830828c-9ebb-4da3-9b87-f5731d041fc1, tor3-sqa-virtual_maas
cluster_4, node5=tgdgmm, MAAS snap rev 41799, 2026-04-08)

---

### Pattern 9: `install_kvm=True` deploy — Ubuntu never boots after curtin (cross-disk grub layout) → complete network silence → stuck in Deploying

**Symptom:** Node deployed with `install_kvm=True`. Curtin completes and sends final status
POST (1st `netboot-finished` received). iPXE fetches local-boot config (299 bytes). After that:
**zero DHCP, HTTP, or squid traffic from node5 MAC address**. The post-install Ubuntu boot
never completes; cloud-init never runs; no 2nd `netboot-finished` arrives.

**FCE error:**
```
##[error]Process completed with exit code 1.
# FCE maas layer times out polling: {'node5': 'Deploying', 'node6': 'Deployed'}
# (no explicit error — silent 30-minute timeout)
```

**Distinguishing evidence:**

```bash
# 1. DHCP lease committed but never renewed after iPXE local-boot
grep "52:54:56:58:07:01\|<node5_mac>" <work_dir>/maas-logs/*/var/log/syslog \
  | grep "dhcpd\|DHCP"
# Expect: one lease at ~T+14s (iPXE DHCP), no renewal 30s later

# 2. Zero HTTP/squid from node5 IP after iPXE local-boot
grep "10.241.144.81\|<node5_ip>" <work_dir>/maas-logs/*/var/log/syslog \
  | awk -F'T' '{print $2}' | sort | tail -5
# Last entry should be the iPXE local-boot request; nothing after

# 3. Temporal worker receives only ONE netboot-finished for the failing node
grep "deploy:bsg8t7\|deploy:<system_id>" <work_dir>/maas-logs/*/var/log/syslog \
  | grep "temporal-worker"
```

**Root cause:** Curtin storage config assigns `grub_device: true` to the disk that contains
only a bios_grub partition and the bcache cache partition — with NO /boot or EFI partition.
The /boot and EFI partitions are on a second disk. In working runs, grub_device and /boot are
on the same disk. This cross-disk layout causes the GRUB bootloader installed to the MBR of
the first disk to be unable to locate `/boot` on next boot.

**Disk layout comparison (examine curtin config to distinguish):**

```bash
# Find curtin config in MAAS logs
find <work_dir>/<uuid>/generated/ -name "*curtin_config*"

# Check grub_device and /boot assignment
grep -A5 "grub_device\|/boot" <curtin_config_file>
```

| Config | grub_device | /boot | Result |
|---|---|---|---|
| Working (3830828c, d7cca7ab) | sda (SCSI-0) | sda-part3 | Same disk ✅ |
| Failing (1078296e) | sda (SCSI-2) | sdb-part3 (SCSI-0) | Cross-disk ❌ |

**Note**: Cannot confirm exact failure mode (GRUB, initramfs, or kernel panic) without serial
console output from the KVM host (10.241.144.1). This host is not included in the FCE log
bundle. node6 deploying successfully in the same run confirms MAAS infrastructure is healthy.

**Runs:** 24120446627 (UUID 1078296e-a98c-4404-8a8d-4abe91eb23e1, tor3-sqa-virtual_maas
cluster_2, node5=bsg8t7, MAAS snap rev 41799, 2026-04-08)

---

---

### Pattern 10: `machines create` hangs 15 minutes → nginx 499 → OAuth expired 401 (dedicated_maas, MAAS 3.7.x)

**Symptom:** FCE `maas:enlist_nodes` calls `maas root machines create hostname=<node>` and
waits ~15 minutes before receiving an error. The error looks like an OAuth expiry, not a
connection failure:

```
Authorization Error: 'Expired timestamp: given 1776200433 and now 1776201334,
has a greater difference than threshold 300'
subprocess.CalledProcessError: Command '['maas', 'root', 'machines', 'create', ...]'
returned non-zero exit status 2.
```

The machine **is** enlisted in MAAS internally (logs show `Enlisted new machine` and
`Status transition NEW→COMMISSIONING`), but FCE never receives the HTTP 200 response.

**Distinguishing evidence:**

```bash
# 1. MAAS did enlist the node internally
grep "duosion\|<hostname>" <work_dir>/maas-logs/*/var/log/syslog | grep "Enlisted\|COMMISSIONING"
# Expect: "duosion: Enlisted new machine" at ~T+3s; "NEW → COMMISSIONING" at ~T+3s

# 2. PowerOnWorkflow (IPMI commissioning power-on) started but never completed
grep "PowerOnWorkflow" <work_dir>/maas-logs/*/var/log/syslog | grep "Starting\|completed"
# For passing node: both "Starting" AND "Workflow has completed" present
# For failing node: only "Starting" — no completion entry

# 3. nginx 499 + immediate 401 on the same endpoint
grep "POST.*machines.*499\|POST.*machines.*401" <work_dir>/maas-logs/*/var/log/syslog
# Expect at ~T+901s: 499 (client disconnect) followed immediately by 401 (OAuth expired retry)

# 4. No HTTP 200 for the failing node
grep "POST.*machines.*200" <work_dir>/maas-logs/*/var/log/syslog
# Only passing nodes get 200; the failing node has no 200 in nginx logs
```

**Root cause:** MAAS 3.7.x changed the `POST /api/2.0/machines/` HTTP handler to block on
the Temporal `PowerOnWorkflow` (IPMI power-on for commissioning) before returning the HTTP
response. Evidence: for a passing node (`azurill`), nginx logged HTTP 200 at the **exact same
second** that the Temporal `PowerOnWorkflow` logged `"Workflow has completed"`. For the
failing node (`duosion`), the `PowerOnWorkflow` was dispatched to IPMI BMC `10.240.21.47`
and never returned — the IPMI BMC was unresponsive. No HTTP response was ever sent.

After ~900 seconds, `python-httplib2` (the `maas` CLI HTTP backend) timed out and disconnected
(nginx 499). It immediately retried the same `POST /machines/` with the **original OAuth token**
(now 901 seconds old). MAAS's OAuth replay-prevention window is 300 seconds, so it returned
HTTP 401 (`Authorization Error: Expired timestamp`). The `maas` CLI exited code 2.

**This is distinct from all other patterns** — the machine *was* enlisted, commissioning *did*
start, and MAAS infrastructure *was* healthy. The hang is caused entirely by the IPMI BMC
being unresponsive on a single node.

**Note on service disruption during hang window:**
Additional secondary effects were observed during the 15-minute hang: (1) pebble health checks
from `maas-agent` gapped for 16–17 minutes on all nodes (anahuac 21:00:01→21:17:31, sunset
21:00:23→21:16:53) — likely because the commissioning PXE boot generated unusual traffic loads;
(2) HTTP 502 on `bootx64.efi` (PXE boot file) at 21:01:57 on noma; (3) rsyslog socket errors
(`Connection refused`) at 21:05:09 and 21:12:49 on sunset; (4) a regiond worker restart
(new PID `maas-regiond[25022]` on anahuac at 21:10:01). These are consequences, not causes.

**What it is NOT:**
- Not an OAuth token generation issue (token was valid when issued; expired only due to 901s wait)
- Not a snap auto-refresh (MAAS snap was installed at 20:45 during `Redeploy dedicated MAAS
  infra nodes`; no refresh occurred during the failure window)
- Not Pattern B (BMC deduplication): the failing node had no pre-existing machine with the
  same BMC IP; enlistment was clean
- Not a PostgreSQL issue (no DB errors in any node's PG logs)

**Remediation:**
1. Investigate the failing node's IPMI BMC (hardware, firmware, network reachability from
   MAAS rack controllers to BMC VLAN)
2. Report MAAS 3.7.x synchronous `PowerOnWorkflow` blocking to the MAAS team — a hung IPMI
   call should not cause an indefinite HTTP hang
3. FCE should use a shorter httplib2 socket timeout (<300s) or regenerate OAuth on retry;
   with a 900s timeout and 300s OAuth replay window, all retries after timeout are guaranteed
   to get 401

**Runs:** 24420487838 (UUID 6400c87a-d617-4828-9f5f-f512d681d0c1, tor3-sqa-dedicated_maas
dh1_j2, MAAS 3.7.2-17972-g.35e297c4d / snap rev 41649, machine `duosion`, BMC 10.240.21.47,
2026-04-14)

---

### Pattern 11: Mirror catalog lists new image set (20260430) but files are absent → Temporal `download-bootresourcefile` HTTP 404 → `is_importing` stuck → FCE `wait_for_not_boot_resource_importing` timeout

**Symptom (FCE maas/log.txt):**
```
2026-05-08-09:38:33 root DEBUG [localhost]: maas root boot-resources is-importing
2026-05-08-09:38:36 root INFO Boot resources still importing, sleeping 30 seconds.
… (repeats every ~30s for ~27 minutes) …
Exception: Timed out waiting for regions to import images.
##[error]Process completed with exit code 1.
```

The failure occurs in FCE's `maas:configure_maas` → `_sync_images()` → `wait_for_not_boot_resource_importing()`.
No node enlists; `machines create` is never reached. This is distinct from Pattern F (where the mirror
is unreachable and `machines create` fails with an empty architecture list) and Pattern G (where nodes
enlist but commissioning fails).

**Root cause:** The internal image mirror (`http://10.141.186.167/maas/images/ephemeral-v3/stable/`) had
updated its **catalog index** to list a new image set (datestamp `20260430` for jammy/amd64) before the
actual image files were synced to the mirror. When MAAS's Temporal engine dispatched
`download-bootresource` child workflows to fetch the files, every attempt returned **HTTP 404**:

```
temporalio.exceptions.ApplicationError: ClientResponseError: 404, message='Not Found',
url=URL('http://10.141.186.167/maas/images/ephemeral-v3/stable/jammy/amd64/20260430/ga-22.04/generic/boot-initrd')
```

MAAS's Temporal retry policy re-attempted each workflow indefinitely. The `is_importing` API endpoint
returns `true` as long as any `bootresource-download` workflow is active. Since all 13 download
workflows were stuck in the retry loop (reach attempt 38+ over ~27 min), `is_importing` never cleared.
FCE's 30-minute poll timeout then fired.

**13 distinct 404 URLs observed (all `jammy/amd64/20260430/<kernel>/<flavor>/<file>`):**
- `ga-22.04/generic/boot-initrd`, `ga-22.04/generic/boot-kernel`
- `ga-22.04/lowlatency/boot-initrd`, `ga-22.04/lowlatency/boot-kernel`
- `hwe-22.04/generic/boot-initrd`, `hwe-22.04/generic/boot-kernel`
- `hwe-22.04/lowlatency/boot-initrd`, `hwe-22.04/lowlatency/boot-kernel`
- `hwe-22.04-edge/generic/boot-initrd`, `hwe-22.04-edge/generic/boot-kernel`
- `hwe-22.04-edge/lowlatency/boot-initrd`, `hwe-22.04-edge/lowlatency/boot-kernel`
- `squashfs`

**Key distinguishing evidence:**

```bash
# Temporal download-bootresourcefile failures in syslog
grep "download-bootresourcefile.*Completing activity as failed" \
  <work_dir>/maas-logs/10.241.144.2/var/log/syslog | head -10
# Expect: attempt=1 failures at ~09:35; same workflows at attempt=N by 10:00

# 404 URLs confirming incomplete mirror sync
grep "404, message='Not Found'" \
  <work_dir>/maas-logs/10.241.144.2/var/log/syslog \
  | grep -oP "url=URL\('\S+'\)" | sort -u
# All URLs follow the pattern: /stable/jammy/amd64/<datestamp>/<kernel>/<flavor>/<file>

# Confirm import WAS triggered and catalog WAS fetched (not Pattern F)
grep "Importing images from source\|Started importing of boot images" \
  <work_dir>/maas-logs/10.241.144.2/var/log/syslog | head -5

# Count Temporal shard errors (startup churn, not the root cause)
grep -c "Failed to lock shard" <work_dir>/maas-logs/10.241.144.2/var/log/syslog
```

**Distinguishing from related patterns:**

| | Pattern L (this) | Pattern F | Pattern G |
|---|---|---|---|
| Mirror reachable? | ✅ Yes (catalog fetched) | ❌ No (`Errno 113`) | ✅ Yes |
| Image files present? | ❌ No (HTTP 404) | ❌ N/A | ⚠️ Partial (noble squashfs deleted) |
| `is_importing` | Stuck true indefinitely | Clears quickly → false positive | Clears after import |
| FCE failure point | `wait_for_not_boot_resource_importing` timeout | `machines create` (empty arch) | `_wait_for_deployed` (commissioning FAIL) |
| `machines create` | Never reached | Fails with empty architecture list | Succeeds (arch valid) |
| Error message | `Timed out waiting for regions to import images` | `'amd64/generic' is not a valid architecture` | `Missing boot image ubuntu/amd64/no-such-kernel/noble` |

**Timing from run 25545041966 (UUID 6db10053-3c89-4fa5-a875-46373eb3fc0e, tor3-sqa-virtual_maas cluster_2,
MAAS 3.5.12-16413-g.7fb94f378, snap rev 41917, branch main, 2026-05-08):**
```
09:17:57 – systemd reload + Corosync/Pacemaker start (MAAS snap rev 41917 install completes)
09:18:03 – Pacemaker starts with empty CIB (pgsql resource permanently blocked — benign for MAAS 3.5)
09:24:55 – MAAS pebble starts regiond + rackd (snap rev 41917); HTTP serving begins
09:31:34 – FCE POST boot-resources op=import → MAAS starts catalog download from 10.141.186.167
09:31:35 – MAAS schedules 13 bootresourcefile downloads for jammy/amd64/20260430 images
09:35:38 – First download-bootresourcefile failures: HTTP 404 (attempt=1) for all 13 files
09:38:33 – FCE begins polling is_importing (already stuck=true from the Temporal retry loops)
10:00:01 – workflow bootresource-download:upstream:99e93312233c reaches attempt=38 (still failing)
10:01:07 – FCE raises: Exception: Timed out waiting for regions to import images.
```

**Note on Pacemaker/PostgreSQL errors:** All three infra nodes showed Pacemaker starting with an empty
CIB at 09:18, leading to pgsql being permanently blocked. This is a separate issue: MAAS 3.5 (snap rev
41917) manages its own Pebble-managed PostgreSQL, independent of Pacemaker. The Pacemaker failure did
**not** affect MAAS operations in this run.

**Recovery:** Retry the pipeline. If the mirror completes its sync of the 20260430 image set before the
next run, the downloads will succeed. If the mirror consistently serves an incomplete 20260430 set,
escalate to the team managing the internal mirror.

---

## Notes

- This step is only executed on `virtual_maas` and `dedicated_maas` substrates; it is
  skipped for `shared_maas`, testflinger, AWS, and Azure.
- The MAAS log archive (`logs-<timestamp>.tgz`) is only present for virtual/dedicated MAAS.
  For `shared_maas` or cloud substrates, skip MAAS log analysis entirely.
- infra1 (first IP in the archive) is typically the MAAS region controller and has the most
  useful consolidated syslog via the `maas-log` relay daemon.
- Node system IDs (e.g. `8knrhr`, `nkbr6q`) appear in MAAS API calls; map them to
  hostnames via `maas.api: [info] Request from user root to acquire machine: node<N>…` entries.

### Pattern 12: HAProxy restart on infra2 drops in-flight `vm-host refresh` → "Remote end closed connection without response"

**Symptom (GitHub Actions log):**
```
subprocess.CalledProcessError: Command '['maas', 'root', 'vm-host', 'refresh', '2']'
returned non-zero exit status 2.

STDERR:
  Remote end closed connection without response
```

Preceded by successful `vm-host refresh 1` (infra1). The step is `maas:setup_infra_kvm`.
`maas-rack support-dump --networking` also fails on infra2 (exit 64) during log collection.

**Root cause:** HAProxy on infra2 (the second infra node) is restarted (graceful SIGTERM
to worker, exit code 143) at the exact moment `maas root vm-host refresh 2` is in progress.
This drops the TCP connection to the MAAS API VIP (`10.241.144.5:80`), causing the MAAS CLI
to return exit code 2 with "Remote end closed connection without response". FCE's
`refresh_vmhost()` has no retry logic — any network-level failure is immediately fatal.

**Timeline (this occurrence):**
- 05:47:07 — `maas root vm-hosts create type=lxd name=infra2 power_address=10.241.144.3 zone=zone2`
- 05:47:28 — `maas root vm-host refresh 2` started
- 05:48:11 — HAProxy on infra2 restarted (per `var/log/haproxy.log` on 10.241.144.3)
- 05:48:12 — `maas root vm-host refresh 2` failed

**HAProxy restart trigger:** Pacemaker manages haproxy as a cloned resource
(`crm configure clone haproxy-clone haproxy` with 15s monitor interval, configured by FCE
at ~05:42:34). The exact trigger for the 05:48:11 restart is not determinable without
syslog (not present in this archive), but likely candidates are:
- MAAS region/agent triggering a haproxy config reload as part of zone2 rack-controller
  registration or vm-host creation processing.
- Pacemaker resource event during vm-host registration.

**Diagnostic grep:**
```bash
grep "05:48\|05:47" /path/to/c9e24a7e-maas-logs/10.241.144.3/var/log/haproxy.log
# Expect: haproxy master exit + new worker fork at 05:48:11
```

**From run:** 25417149963 (UUID c9e24a7e-3390-4ded-b4fc-374cc8e42910,
tor3-sqa-virtual_maas cluster_2, MAAS 3.5/beta rev 41917, branch main, 2026-05-06).

---

### Pattern 13: Runner-side `maas` CLI hangs after successful API 200 responses → `compose_vms` false failure

**Symptom (GitHub Actions log):**
```
2026-05-25-14:35:03 root DEBUG [localhost]: maas root machines read
... 4 hours of silence ...
subprocess.CalledProcessError: Command '['maas', 'root', 'machines', 'read']' died with <Signals.SIGKILL: 9>.
##[error]Process completed with exit code 1.
```

The failure occurs inside `maas:compose_vms` after `juju-1`, `sunbeam-1`, `juju-2`, and
`sunbeam-2` have already been composed and tagged.

**Root cause:** The MAAS API stayed healthy, but the **runner-side snapped `maas` CLI**
stalled locally after receiving successful HTTP responses. The key proof is that mienfoo's MAAS
logs show the supposedly hanging requests completing with HTTP 200 in a few seconds, while the
runner-side CLI never returned any stdout/stderr and was eventually killed. Later diagnostics in
the same job reproduced the behavior for small endpoints too (`version read`, `rack-controllers
read`, `machines read hostname=quilava`): all timed out locally, yet mienfoo logged matching
HTTP 200 responses immediately. This makes the failure a **false negative in the client on the
runner**, not a MAAS server outage, DB stall, or bad machine state.

A same-day runner snap refresh is a strong clue, but not fully proven as the direct trigger:
`maas` on the runner was revision 41649 with `refresh-date: today at 13:48 UTC`, and the CLI's
profile DB lived under `/home/ubuntu/snap/maas/41649/.maascli.db`. The available artifacts do
not expose the exact blocked code path after the HTTP response returns.

**Distinguishing evidence:**

```bash
# 1. The hung FCE call's API request actually completed successfully
# Expect: GET /MAAS/api/2.0/machines/ ... --> 200 OK at ~14:35:06

grep "2026-05-25T14:35:0[3-6]:" <work_dir>/maas-logs/10.241.144.3/var/log/syslog \
  | grep "/MAAS/api/2.0/machines/"

# 2. Later diagnostic MAAS CLI calls also hang locally, but server logs show 200 responses
# Expect: version/rackcontrollers/machines?hostname requests at 18:48 all return 200 in syslog

grep "2026-05-25T18:48:" <work_dir>/maas-logs/10.241.144.3/var/log/syslog \
  | grep "/MAAS/api/2.0/version/\|/MAAS/api/2.0/rackcontrollers/\|/MAAS/api/2.0/machines/?hostname=quilava"

# 3. Snapshot proves the composed VMs existed and were already Ready despite the failure
python3 - <<'PY'
import glob, json
path = glob.glob('<work_dir>/<uuid>/generated/marvin-the-happy-bot/snapshot-*/maas/machines.json')[0]
arr = json.load(open(path))
for name in ['juju-1', 'sunbeam-1', 'juju-2', 'sunbeam-2']:
    m = next(x for x in arr if x['hostname'] == name)
    print(name, m['status_name'], m['system_id'])
PY
```

**Key timeline (run 26388147932 / UUID 59a74d0f-d147-4b31-969c-d1c24a20259a):**
- 13:48 UTC — runner `maas` snap revision 41649 refreshed
- 14:32–14:34 — `juju-1`, `sunbeam-1`, `juju-2`, `sunbeam-2` composed successfully
- 14:35:03 — FCE starts `maas root machines read`
- 14:35:06 — mienfoo logs `GET /MAAS/api/2.0/machines/` → `200 OK` (429734-byte response)
- 18:35:03 — runner kills the still-stuck CLI process with SIGKILL
- 18:48 — ad-hoc `maas root version read`, `rack-controllers read`, and `machines read hostname=quilava` all hang locally; mienfoo logs matching 200 responses immediately

**What it is NOT:**
- Not Pattern J (`machines create` server-side hang on PowerOnWorkflow): here the server replied 200
- Not a MAAS API VIP/network outage: `curl http://10.241.144.5:80/MAAS/api/2.0/version/` returned 200, ping and TCP/80 succeeded
- Not a bad VM state: snapshot `machines.json` shows `juju-1`, `sunbeam-1`, `juju-2`, and `sunbeam-2` all `Ready`

**Recommendations:**
1. Add an explicit timeout/retry wrapper around MAAS CLI calls in FCE and log when the server already returned 200 but the client stayed stuck.
2. Prefer direct MAAS API probes (or a secondary validation path) before failing the whole layer on a stuck CLI subprocess.
3. Investigate the runner-side snapped `maas` CLI on rev 41649, especially post-response hangs after the 2026-05-25 13:48 refresh.

---

### Pattern 14: Intel IOMMU (DMAR) DMA mapping bug corrupts bcache reads → Deterministic segfaults

**Symptom (GitHub Actions log):**
```
panic: runtime error: invalid memory address or nil pointer dereference
[signal SIGSEGV: segmentation violation code=0x1 addr=0x1 pc=0x63f3dc8a8860]

goroutine 1 [running]:
internal/godebug.init.0()
	/usr/lib/go-1.22/src/internal/godebug/godebug.go:197
...
root WARNING snap install maas --channel 3.7/stable failed. Attempting to retry in 60 seconds.
```
The `snap install maas` command repeatedly panics with a nil pointer dereference inside the early Go initialization (`internal/godebug.init.0()`) at the exact same instruction offset (e.g. `...860`).

**Root cause:** An Intel IOMMU (DMAR) mapping bug interacts with the kernel's `bcache_writebac` thread on specific hardware (e.g. HP ProLiant DL320e Gen8) when using an NVMe caching device. The IOMMU fails to correctly map DMA page table entries (`DMA PTE for vPFN ... already set`). Because DMA mappings are corrupted, block transfers from the storage device into RAM place garbage data into the page cache.
This causes userspace binaries stored on the root filesystem to be silently corrupted when executed. The binaries read the corrupted pages from the cache and execute garbage instructions, leading to deterministic segfaults.

**Distinguishing evidence:**
1. **DMAR errors in syslog at boot:** Immediately after `bcache` registers the NVMe cache, `syslog` logs `DMAR: ERROR: DMA PTE for vPFN 0xf1f80 already set` and a kernel warning at `__domain_mapping`. The PID is `bcache_writebac`.
2. **Multiple binaries segfault at deterministic offsets:** The node's `syslog` shows other userspace applications segfaulting deterministically (e.g. `ModemManager` repeatedly segfaulting at offset `...595` in `ld-linux.so`, `snapd` failing with `INVALIDARGUMENT`).
3. **Hardware specific:** The issue only affects nodes with the specific IOMMU hardware/bug (e.g., DL320e Gen8), while identical `snap install maas` commands on sibling nodes (e.g. DL360e Gen8) running the same kernel succeed.

**Evidence to look for:**
```bash
# 1. Look for DMAR errors and bcache warnings in syslog
grep -A2 "DMAR: ERROR: DMA PTE" <work_dir>/maas-logs/<ip>/var/log/syslog
# Expect: WARNING: CPU: X PID: Y at drivers/iommu/intel/iommu.c:2227 __domain_mapping

# 2. Check for deterministic segfaults in other services
grep "segfault" <work_dir>/maas-logs/<ip>/var/log/syslog
# Expect: ModemManager segfaulting repeatedly at identical instruction pointer offsets
```

**Remediation:** Pass `intel_iommu=off` or `intel_iommu=pt` as a kernel parameter via MAAS for the affected node, or disable `bcache` if IOMMU is strictly required on that hardware.

**From run:** 26719477705 (UUID 5eb8d383-dd43-47da-9175-481d8e62aa89, tor3-sqa-dedicated_maas dh1_j2, Ubuntu 24.04 noble, linux 6.8.0-124-generic, node `anahuac` / HP ProLiant DL320e Gen8).

### Pattern 15: Infra node hardware failure / disk corruption → `apt-get` exit code 100 → `rsync` fails (Logs wiped by retries)

**Symptom (GitHub Actions log):**
```
subprocess.CalledProcessError: Command '['ssh', '-o', 'StrictHostKeyChecking=no', '-o', 'UserKnownHostsFile=/dev/null', '-o', 'LogLevel=ERROR', 'ubuntu@10.241.128.2', '--', 'sudo', "bash --login -c 'DEBIAN_FRONTEND=noninteractive apt-get -q install -y qemu-system libvirt-daemon-system libvirt-clients bridge-utils'"]' returned non-zero exit status 100.
##[error]Process completed with exit code 1.
```

**Root cause:** A physical infra node (e.g. `anahuac`) suffers from hardware instability or disk corruption. During the `Redeploy dedicated MAAS infra nodes` step (via Terraform), the node fails to deploy and enters the `Failed deployment` state. The pipeline's `prepare_for_retry.sh` script wipes the cluster to try again, which completely destroys the MAAS logs of the failure. On a subsequent retry, the node may finally transition to `Deployed`, but its filesystem or dpkg database is corrupted/locked. When FCE connects via SSH to run `apt-get install`, it fails with exit code `100`. Finally, the node becomes so unresponsive that the log collection `rsync` fails completely.

**Distinguishing evidence:**

```bash
# 1. Previous attempts failed in Terraform deployment for the node
grep "unexpected state 'Failed deployment'" <work_dir>/run_<id>_failed.log
# Expect multiple occurrences matching the failed node before the final apt-get failure

# 2. apt-get failed with exit code 100 during maas:preflight
grep "returned non-zero exit status 100" <work_dir>/<uuid>/generated/maas/log.txt

# 3. Log collection (rsync) failed for the node
grep "rsync.*failed" <work_dir>/<uuid>/generated/github-runner/run.log
```

**Remediation:** Remove the faulty physical node from the cluster (e.g., exclude `anahuac` from `dh1_j2`) as it is unable to reliably complete a MAAS deployment or run basic package installations.

---

### Pattern 16: Initial automatic public mirror import blocks FCE's internal mirror configuration → slow public download exceeds 30-minute timeout

**Symptom (FCE maas/log.txt):**
```
2026-06-11-15:32:13 root INFO Boot resources still importing, sleeping 30 seconds.
... (repeats for 30 minutes) ...
Exception: Timed out waiting for regions to import images.
##[error]Process completed with exit code 1.
```

**Root cause:**
When the MAAS snap is newly installed and initialized, it immediately schedules and starts an automatic boot resources import from the default public mirror (`images.maas.io`). Shortly after, FCE's `configure_maas` step attempts to update the boot source to the fast internal Canonical mirror (`http://10.141.186.167/maas/images/ephemeral-v3/stable/`) and trigger a fresh import. 

However, since the automatic public mirror import is already active and downloading large `squashfs` image files (each around 400-500 MB), the MAAS API rejects/skips the new import request, logging `Skipping import as another import is already running.` in the syslog. The initial import from the slow public mirror continues. In environments where the external network bandwidth to `images.maas.io` is severely bottlenecked, the public mirror downloads take longer than the 30-minute hardcoded poll timeout of FCE, leading to a timeout exception before any nodes can be enlisted or commissioned.

**Evidence to look for:**
- **FCE log.txt** shows the `configure_maas` step timed out waiting for regions to import images:
  `Exception: Timed out waiting for regions to import images.`
- **MAAS syslog** shows that the second import request was skipped:
  `maas.bootresources: [info] Skipping import as another import is already running.`
- **MAAS syslog** shows long durations for public mirror downloads:
  `Workflow has completed ... id: download-bootresource:upstream:..., elapsed_time_seconds: 1800+`
- **MAAS syslog** shows that the active download requests are going to `images.maas.io` rather than the configured internal mirror:
  `HTTP Request: GET http://images.maas.io/ephemeral-v3/stable/.../squashfs`

**Remediation:**
1. Configure MAAS to initialize with the internal boot source directly, or wait/cancel the default automatic import before applying the FCE mirror configuration.
2. Increase the hardcoded 30-minute timeout in FCE's `wait_for_not_boot_resource_importing` helper.
3. Improve external network speed to `images.maas.io` from the cluster infrastructure.

**From run:** 27356099991 (UUID f051f1f8-6a10-49b7-8d91-ca8d43b79ef5, tor3-sqa-dedicated_maas dh1_j6, MAAS 3.7.2-17972-g.35e297c4d / snap rev 41649, 2026-06-11).

---

### Pattern 17: Edge MAAS snap AppArmor denies `blkid` exec → regiond crash-loops → `maas login` "Remote end closed connection"

**Symptom (GitHub Actions / `generated/maas/log.txt`):**
The FCE step `maas:login_maas_infra` fails even though `maas_install` and
`maas_admin_setup` (createadmin/apikey) succeeded:

```
subprocess.CalledProcessError: Command '['ssh', ..., 'ubuntu@<infra1>',
  "bash --login -c 'maas login root http://<vip>:80/MAAS <apikey>'"]'
  returned non-zero exit status 2.

STDERR:
  usage: maas [-h] COMMAND ...
  ...
  Remote end closed connection without response
```

Note: `maas createadmin` and `maas apikey` succeed (they hit the DB directly),
but `maas login` needs the HTTP API — which is down — so it prints usage and exits 2.

**Root cause:** The MAAS region controller (`maas-regiond`) crash-loops on startup
because the snap's bundled `machine-resources/amd64` helper cannot exec `blkid`.
The snap AppArmor profile `snap.maas.pebble` is missing exec permission for
`/usr/sbin/blkid`:

```
apparmor="DENIED" operation="exec" profile="snap.maas.pebble"
  name="/usr/sbin/blkid" comm="amd64" requested_mask="x"
```

regiond startup aborts in `inner_start_up` → `get_or_create_running_controller`
→ `_find_running_node` → `get_mac_addresses`/`get_ip_addr`:

```
provisioningserver.utils.shell.ExternalProcessError:
  Command `/snap/maas/<rev>/usr/share/maas/machine-resources/amd64` returned non-zero exit status 1:
ERROR: Failed retrieving storage information: Failed retrieving filesystem UUID from
  device "/dev/sda1": Failed running: blkid -s UUID -o value /dev/sda1:
  fork/exec /usr/sbin/blkid: permission denied
```

With regiond never serving, HAProxy keeps all backends DOWN and the VIP returns no
response — hence "Remote end closed connection without response" from the CLI.

**Distinguishing evidence (grep):**
```bash
# 1. regiond crash-loop: many identical blkid failures over several minutes
grep -c "Failed retrieving storage information" <work_dir>/maas-logs/*/*/var/log/syslog

# 2. AppArmor denial for blkid under the snap pebble profile
grep "DENIED" <work_dir>/maas-logs/*/*/var/log/syslog | grep blkid

# 3. HAProxy has no MAAS backend during the login window
grep "no server available\|maas-api-.* is DOWN" <work_dir>/maas-logs/*/*/var/log/haproxy.log
```

**Key differentiator from Pattern 12:** Pattern 12 is a *transient* HAProxy restart
dropping a single in-flight request (`vm-host refresh`) — the API is otherwise healthy.
Here the API backends are DOWN for the entire window because regiond never starts.

**Recommendation:** Pin the MAAS snap to a known-good stable revision instead of
`latest/edge`, and report the missing `/usr/sbin/blkid` exec permission in the
`snap.maas.pebble` AppArmor profile as an edge snap regression.

**From run:** 27746796813 (UUID efc40c3d-c010-4531-b2de-bfff3ab02c18,
tor3-sqa-virtual_maas cluster_4, MAAS snap rev 42119 latest/edge / Python 3.14,
branch main, 2026-06-18).

---

### Pattern 18: Concurrent `fetch_manifest_and_update_cache` activities → PostgreSQL `SerializationError` → aborted transaction → `boot-resources import` returns "Workflow execution failed" (exit 2)

**Symptom (GitHub Actions log / FCE log.txt):** `maas:configure_maas` fails almost immediately
(~40s) after triggering the import — not a timeout. The CLI command itself returns non-zero:

```
2026-06-24-07:18:46 root DEBUG [localhost]: maas root boot-source update 1 url=http://10.141.186.167/maas/images/ephemeral-v3/stable/
2026-06-24-07:18:49 root DEBUG [localhost]: maas root boot-resources import
2026-06-24-07:19:29 root ERROR [localhost] Command failed: maas root boot-resources import
   STDOUT follows: Workflow execution failed
   STDERR follows: b''
subprocess.CalledProcessError: Command '['maas', 'root', 'boot-resources', 'import']' returned non-zero exit status 2.
```

FCE traceback ends in `configuremaas.py:_ensure_boot_sources()` → `maas_cli.py:boot_resources_import()`.

**Root cause:** The `maas boot-resources import` CLI synchronously executes MAAS's
`fetch-manifest` Temporal workflow (`FETCH_MANIFEST_AND_UPDATE_CACHE_WORKFLOW`). Two
`fetch_manifest_and_update_cache` activities run concurrently (started ~17 ms apart) and both
issue `UPDATE maasserver_imagemanifest`. PostgreSQL aborts one with `asyncpg SerializationError:
could not serialize access due to concurrent update`. MAAS's handler does **not** roll back the
aborted transaction and keeps issuing SQL on it (e.g. a `SELECT` from `maasserver_notification`),
raising `InFailedSQLTransactionError: current transaction is aborted`. This bubbles up as a
Temporal `ActivityError` → `WorkflowFailureError: Workflow execution failed`, the region API
returns HTTP 500, and the CLI exits status 2. This is a MAAS-side concurrency/error-handling
defect observed on the **3.8.0~alpha1 (rev 42148, Python 3.14)** snap.

**Evidence (infra1 = region/MAAS API host):**
- infra1 syslog (`maas-temporal-worker`): two `Starting activity fetch_manifest_and_update_cache`
  entries milliseconds apart, then
  `ERROR Could not fetch manifest for boot source ...: SerializationError: could not serialize
  access due to concurrent update [SQL: UPDATE maasserver_imagemanifest ...]`
- infra1 syslog (`maas-regiond`): `Exception: Workflow execution failed` →
  `temporalio.exceptions.ApplicationError: InFailedSQLTransactionError: current transaction is
  aborted` → `temporalio.client.WorkflowFailureError: Workflow execution failed`
- infra1 syslog: `POST /MAAS/api/2.0/boot-resources/?op=import HTTP/1.1 --> 500 INTERNAL_SERVER_ERROR`

**Distinguishing from timeout patterns (6/7/11/16):** This is NOT a polling timeout. There are
no `Boot resources still importing, sleeping 30 seconds` loops and no
`Timed out waiting for regions to import images`. Failure is immediate (<1 min) and the CLI
returns exit 2 with STDOUT `Workflow execution failed`. The secondary `404 Not Found` errors for
`noble/.../boot-initrd` `download-bootresourcefile` activities are retried noise, not the terminal
cause returned to the CLI.

**Grep hints:**
```bash
# Terminal workflow failure returned to the CLI
grep -n "Workflow execution failed ###\|InFailedSQLTransactionError\|op=import.*500" \
  <work_dir>/maas-logs/*/var/log/syslog

# The serialization conflict that started it
grep -n "could not serialize access due to concurrent update\|Could not fetch manifest" \
  <work_dir>/maas-logs/*/var/log/syslog
```

**Recommendation:** Report against MAAS (3.8.0~alpha1 snap) — serialize the manifest-fetch
activity and roll back/retry on `SerializationError` instead of continuing on an aborted
transaction. For the pipeline, pin to a stable MAAS snap revision (avoid alpha) and/or add a
bounded retry around FCE's `boot_resources_import()` for transient 500s.

**From run:** 28079848487 (UUID c85047c7-16df-46ee-be17-60bb4481c5f1,
tor3-sqa-virtual_maas cluster_2, MAAS snap rev 42148 / 3.8.0~alpha1 / Python 3.14,
branch main, 2026-06-24).

---

### Pattern 19: SSH tunnel disconnects mid-run → local `maas` commands time out with `[Errno 110] Connection timed out`

**Symptom (FCE `maas/log.txt`):**
The FCE build process hangs for over 4 minutes on a local `maas` CLI command (e.g. `maas root machine power-parameters <id>`), then crashes:

```
2026-06-24-17:24:19 root DEBUG [localhost]: maas root machine power-parameters xdrt8k
2026-06-24-17:28:34 root ERROR [localhost] Command failed: maas root machine power-parameters xdrt8k
...
[Errno 110] Connection timed out
...
subprocess.CalledProcessError: Command '['maas', 'root', 'machine', 'power-parameters', 'xdrt8k']' returned non-zero exit status 2.
##[error]Process completed with exit code 1.
```

The failing step is typically `maas` (Step 40) or another early MAAS step.

**Root cause:**
For `virtual_maas` substrates, the pipeline runner communicates with the isolated virtualized MAAS region API via an SSH tunnel (configured to bind the VIP to localhost or local routes). When the SSH tunnel drops mid-run, all subsequent MAAS CLI API commands attempted by FCE on the runner will block, eventually timing out after 4+ minutes with a connection timeout error.

Because the SSH tunnel is down, subsequent log-collection scripts cannot reach the virtual MAAS region controllers (`infra1`, `infra2`, `infra3`). If they share an IP space with the underlying physical MAAS nodes, the log-collection script may fall back and download logs from physical nodes (with Pokémon names like `leafeon`), which are irrelevant to the virtual MAAS failure.

**Distinguishing evidence:**
1. **SSH tunnel health log shows a sudden disconnect:** `generated/sshtest.txt` will record successful connections (returning `infra1`) up to a certain timestamp, after which all entries fail with `Connection closed by <IP> port 22` or `kex_exchange_identification: Connection closed by remote host`.
2. **MAAS CLI hangs precisely for ~4 minutes:** The timestamp gapping between the starting `DEBUG [localhost]: maas` and the resulting `ERROR [localhost] Command failed:` is more than 4 minutes.
3. **Collected MAAS logs contain incorrect hostnames (Pokémon names):** The syslog or haproxy logs in the extracted MAAS log archive refer to physical nodes (e.g. `leafeon`) instead of `infra1`/`infra2`/`infra3`, confirming the log collector hit the underlying physical nodes because the SSH tunnel to the virtual lab was dead.

**Remediation:**
1. Implement auto-reconnection and retry loop logic for the SSH tunnel on the pipeline runner.
2. Add a pre-flight tunnel check in the FCE MAAS command layer to fail-fast with a clear "SSH tunnel disconnected" message if `sshtest.txt` fails, rather than waiting for 4-minute command timeouts.

**From run:** 28113757830 (UUID 8ed53fb4-9491-4452-8f7c-35d832470453, tor3-sqa-virtual_maas cluster_6, MAAS 3.6/beta, branch main, 2026-06-24).

---

### Pattern 20: Stale aborted transaction from prior workflow failures → `boot-resources import` returns "Workflow execution failed" (exit 2)

**Symptom (GitHub Actions log / FCE log.txt):** `maas:configure_maas` fails ~40s after triggering
the import — not a timeout. The CLI command returns non-zero:

```
subprocess.CalledProcessError: Command '['maas', 'root', 'boot-resources', 'import']' returned non-zero exit status 2.
STDOUT: Workflow execution failed
STDERR: b''
```

**Root cause:** During the preceding `maas:maas_install` step, MAAS Temporal workflows for
existing boot resources fail due to a brief HAProxy VIP availability gap
(`"Region not available: No route to host"`). Child workflows are cancelled with
`ChildWorkflowError` and `ActivityError`. These cascading failures leave the database session
in an **aborted transaction state** (`InFailedSQLTransactionError`).

When `maas:configure_maas` later triggers a new `boot-resources import`, the MAAS API handler
submits a `FetchManifestWorkflow`. The `fetch_manifest_and_update_cache` activity uses the
**same connection pool** and hits the already-aborted transaction:
```
InFailedSQLTransactionError: current transaction is aborted, commands ignored until end of
transaction block
```
The MAAS Temporal worker retries (attempt 2) but the transaction is permanently wedged. After
~21 seconds, the workflow fails, the region API returns HTTP 500, and the CLI exits status 2.

**Distinguishing from Pattern 18:** Pattern 18 is triggered by a concurrent `SerializationError`
during `UPDATE maasserver_imagemanifest`. This variant is triggered by a **stale aborted
transaction** from earlier workflow failures caused by HAProxy VIP availability gap during
`maas_install`. The terminal symptom is the same: `InFailedSQLTransactionError` → "Workflow
execution failed" → exit 2. Both are MAAS 3.8.0~alpha1 (rev 42148) defects.

**Evidence (infra1 = region/MAAS API host):**
- infra1 syslog (`maas-rackd`): `"Region not available: No route to host"` at the HAProxy VIP
- infra1 syslog (`maas-temporal-worker`): Multiple `ChildWorkflowError: Child Workflow execution
  cancelled` and `ActivityError: Activity cancelled` during maas_install
- infra1 syslog (`maas-temporal-worker`): `fetch_manifest_and_update_cache` attempt 1 fails
  with `InFailedSQLTransactionError`, attempt 2 fails with same error
- infra1 syslog (`maas-regiond`): `WorkflowFailureError: Workflow execution failed`

**Grep hints:**
```bash
# HAProxy VIP gap that triggered the cascade
grep "No route to host.*10.241.144.5" <work_dir>/maas-logs/*/var/log/syslog

# Workflow cancellations
grep "ChildWorkflowError.*cancelled\|ActivityError.*cancelled" \
  <work_dir>/maas-logs/10.241.144.2/var/log/syslog

# Terminal workflow failure
grep "InFailedSQLTransactionError\|Workflow execution failed" \
  <work_dir>/maas-logs/10.241.144.2/var/log/syslog
```

**Recommendation:** Report against MAAS (3.8.0~alpha1 snap) — the Temporal worker should
explicitly issue `ROLLBACK` and obtain a fresh connection when encountering
`InFailedSQLTransactionError`, rather than retrying on the same wedged transaction. For the
pipeline, investigate the HAProxy VIP availability gap during `maas_install` (region controller
startup sequencing may briefly take the VIP offline).

**From run:** 28106239752 (UUID 57e8c8cb-b8f5-4a7b-ad9b-f01ab37d9002,
tor3-sqa-virtual_maas cluster_7, MAAS snap rev 42148 / 3.8.0~alpha1 / Python 3.14,
branch main, 2026-06-24).
