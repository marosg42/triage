# Step Knowledge: sunbeam_maas_deploy

## Step Overview

Runs `sunbeam cluster bootstrap`, `sunbeam cluster deploy`, and `sunbeam configure`
on a set of MAAS-allocated VMs. Bootstrap provisions a 3-node HA Juju controller on
`juju-controller`-tagged machines, then deploys the OpenStack k8s workloads on the
`sunbeam`-tagged machines.

Substrate: `tor3-sqa-dedicated_maas` (Pokémon-named KVM hosts, VMs named
`juju-1/2/3` and `sunbeam-1/2/3`).

## Swift Artifacts

Objects stored under `<uuid>/generated/` that are useful for diagnosing failures:

| Path | Description | When to check |
|---|---|---|
| `generated/sunbeam/output.log` | Full FCE sunbeam_maas_prepare_env + deploy log | Always — first stop |
| `generated/maas/logs-<timestamp>.tgz` | Full MAAS infrastructure log archive | Deep investigation |
| `generated/version_collector_sunbeam_maas_deploy.log` | Snap/apt versions + juju status at failure time | Check juju controller state |
| `generated/sunbeam/manifest.yaml` | Sunbeam manifest used for bootstrap | Check cluster config |
| `generated/versions.yaml` | Software versions for all layers | Identify snap revisions |

## Key Log Files (inside MAAS logs tgz archive)

Hosts: `10.241.128.2` (anahuac), `10.241.128.3` (sunset), `10.241.128.4` (noma).
These are the physical KVM hosts hosting the `juju-*` and `sunbeam-*` VMs.

| File | What it contains | When to use |
|---|---|---|
| `var/log/syslog` | MAAS node status transitions, libvirtd events | Primary investigation |
| `var/log/libvirt/qemu/juju-N-serial0.log` | VM console output during OS install | Check curtin timing, cloud-init finish |
| `var/log/libvirt/qemu/sunbeam-N-serial0.log` | Sunbeam VM console output | Check sunbeam node deploy |
| `var/log/libvirt/qemu/juju-N.log` | QEMU startup/shutdown log | Check I/O errors on KVM host |

## Grep Patterns

```bash
# Find VM deploy transitions
grep "juju-\|sunbeam-" <work_dir>/maas-logs/*/var/log/syslog | grep "DEPLOYING\|DEPLOYED\|READY\|transition"

# Find cloud-init finish timestamps (crucial for HA controller timing)
grep "cloud-init.*finished\|Cloud-init.*finished" <work_dir>/maas-logs/*/var/log/libvirt/qemu/juju-*-serial0.log

# Find KVM host I/O errors
grep "End of file while reading\|Input/output error" <work_dir>/maas-logs/*/var/log/libvirt/qemu/*.log

# Find juju wait-for in GitHub Actions log
grep "wait-for\|timeout\|returned non-zero" run_<run_id>_failed.log
```

## Known Failure Patterns

### Pattern 1: HA Juju controller `wait-for` timeout — slow KVM deployment

**Symptom:**
```
An unexpected error has occurred.
Error: Command '['/snap/openstack/945/juju/bin/juju', 'wait-for', 'application',
       '-m', 'controller', 'controller', '--timeout', '15m']'
       returned non-zero exit status 1.
Process completed with exit code 1.
```

**Root cause:** `sunbeam cluster bootstrap` deploys a 3-node HA Juju controller on
MAAS VMs. After bootstrapping `juju-1`, it simultaneously deploys `juju-2` and
`juju-3`, then calls `juju wait-for application controller -m controller --timeout
15m`. If one of the HA nodes takes longer than ~12–13 minutes for OS installation
(curtin) + cloud-init (which installs the Juju agent), the deadline expires before
all 3 `controller` units can become active. The `wait-for` then exits with code 1.

Observed on `dedicated_maas dh1_j2`: `juju-3` (hosted on KVM host `sunset`) took
12m 46s for OS deployment vs 7m 24s for `juju-2` (on KVM host `noma`). Cloud-init
on `juju-3` didn't finish until 15:49:24, 38 seconds after the error was reported.

**Evidence to look for:**
- `maas-logs/10.241.128.3/var/log/syslog`: `juju-3: Status transition from DEPLOYING to DEPLOYED` — check this timestamp; if it is >10m after deploy start, the 15m deadline is likely tight
- `maas-logs/10.241.128.3/var/log/libvirt/qemu/juju-3-serial0.log`: Look for `cloud-init.*finished at.*Up NNN seconds` — if finished time is after the `juju wait-for` deadline, this confirms the pattern
- `maas-logs/*/var/log/libvirt/qemu/juju-3.log`: `End of file while reading data: Input/output error` — repeated during VM startup signals transient I/O contention on the KVM host
- GitHub Actions log: `sunbeam cluster bootstrap` started timestamp vs error timestamp should be ~22m (15m wait + ~7m setup overhead)

**Observed in:** run 24403605251 (UUID 69c781c2, tor3-sqa-dedicated_maas dh1_j2,
openstack rev 945, 2024.1/stable, 2026-04-14); run 24410111949 (UUID f3d1c1f9,
same substrate/cluster, 2026-04-14); run 24415173684 (UUID e9de9830, same
substrate/cluster, 2026-04-15); run 24465842464 (UUID 2c232149, same
substrate/cluster, 2026-04-15); run 24472204170 (UUID 210a50ea, same
substrate/cluster, 2026-04-15) — juju-3 on `sunset` finished cloud-init 24s after
deadline; no I/O errors in juju-3.log this time (curtin latency alone caused the miss);
run 24478433479 (UUID 0b8b31f3, same substrate/cluster, 2026-04-15) — juju-3 on
`sunset` finished cloud-init 42s before deadline but Juju agent did not register in time;
curtin took 11m31s, cloud-init 2m54s (total 14m25s); no I/O errors in QEMU logs;
run 24482841742 (UUID 98ef576d, same substrate/cluster, 2026-04-16) — juju-3 on
`sunset` finished cloud-init 11s after deadline; curtin took 12m18s, cloud-init 2m47s
(total 15m05s); juju-2 on `noma` deployed in 7m33s for comparison; no I/O errors in
QEMU logs.

**Bug:** [lp:openstack:2148312](https://bugs.launchpad.net/openstack/+bug/2148312)

---

### Pattern 2: `sunbeam cluster deploy` false-negative 30-minute timeout after apparent convergence

**Symptom:**
```
+ sunbeam cluster deploy
wait timed out after 1799.9999981459987s
Error: wait timed out after 1799.9999981459987s
Process completed with exit code 1.
```

**Root cause:** `sunbeam cluster deploy` hit its internal ~30 minute wait deadline even though the deployment had effectively converged. The timeout snapshot in the GitHub log already showed the `openstack-machines` model in `available` state with all machine-model applications (`cinder-volume`, `k8s`, `microceph`, `openstack-hypervisor`, `sunbeam-machine`) `active`, and post-failure collection showed the `openstack-infra`, `openstack-machines`, and `openstack` models all active. This points to a false-negative readiness gate inside `sunbeam cluster deploy` rather than a workload actually failing.

**Evidence to look for:**
- GitHub failed log: `sunbeam cluster deploy` starts immediately after bootstrap, then fails exactly `1799.999...s` later with `wait timed out`
- The timeout snapshot dumped by the command shows `model_status=StatusInfo(current='available'...)` for `openstack-machines`
- The same timeout snapshot shows all machine-model apps and units `active`/`idle`
- Post-failure `generated/sunbeam/juju_status_openstack-infra.txt`, `juju_status_openstack-machines.txt`, and `juju_status_openstack.txt` show all models active; only some `mysql` units still report `Agent=executing` while workload stays `active`

**Observed in:** run 25950047554 (UUID 543ceda1-ee88-4631-ad00-8604348744c6, tor3-sqa-dedicated_maas dh1_j2, branch `main`, addon `sunbeam_2024.1_beta`, 2026-05-16).

---

_Add more patterns below as they are discovered._

## Notes

- The `15m` timeout in `juju wait-for application controller` is hardcoded inside the
  openstack snap's `sunbeam cluster bootstrap` implementation. It cannot be changed
  without modifying the snap.
- The MAAS node deploy sequence is: `juju-1` first (bootstrap), then `juju-2` and
  `juju-3` simultaneously (enable-ha). The critical path is from when the simultaneous
  pair starts deploying to when the slower one's Juju agent is ready.
- Cloud-init on deployed VMs includes Telegraf installation via `late_commands` — this
  requires reaching an external apt repo and can add 1–2 minutes on top of the base
  curtin install time.
- Physical KVM hosts are Pokémon-named: `anahuac` (10.241.128.2), `sunset`
  (10.241.128.3), `noma` (10.241.128.4). VM serial logs for each juju node are in the
  MAAS tgz under `var/log/libvirt/qemu/juju-N-serial0.log` on the respective host.

## Version History

- **v1.0** (2026-04-15): Initial version — Pattern A from run 24403605251
  (UUID 69c781c2, dedicated_maas dh1_j2).
- **v1.1** (2026-04-15): Added two more confirmed occurrences of Pattern A
  (UUIDs f3d1c1f9, e9de9830, same substrate/cluster); linked bug lp:openstack:2148312.
- **v1.2** (2026-04-15): Fourth confirmed occurrence of Pattern A — UUID 2c232149,
  run 24465842464, same substrate/cluster (dedicated_maas dh1_j2), openstack rev 945.
- **v1.3** (2026-04-15): Fifth confirmed occurrence — UUID 210a50ea, run 24472204170,
  same substrate/cluster; juju-3 on `sunset` missed deadline by 24s; no I/O errors in
  QEMU log — pure curtin install latency caused the miss.
- **v1.4** (2026-04-15): Sixth confirmed occurrence — UUID 0b8b31f3, run 24478433479,
  same substrate/cluster; juju-3 on `sunset` finished cloud-init 42s before deadline
  but Juju agent did not register in time; curtin 11m31s + cloud-init 2m54s = 14m25s
  total; no I/O errors in QEMU logs.
- **v1.5** (2026-04-16): Seventh confirmed occurrence — UUID 98ef576d, run 24482841742,
  same substrate/cluster; juju-3 on `sunset` finished cloud-init 11s after deadline;
  curtin 12m18s + cloud-init 2m47s = 15m05s total; juju-2 on `noma` deployed in 7m33s;
  no I/O errors in QEMU logs.
- **v1.6** (2026-05-19): Added Pattern 2 — `sunbeam cluster deploy` hit its hardcoded
  1800s timeout even though the `openstack-machines` timeout snapshot was already fully
  active and post-failure collection showed all Sunbeam models active; likely a false-
  negative readiness gate inside the command rather than a broken deployment. Observed in
  run 25950047554 (UUID 543ceda1-ee88-4631-ad00-8604348744c6, tor3-sqa-dedicated_maas
  dh1_j2).
