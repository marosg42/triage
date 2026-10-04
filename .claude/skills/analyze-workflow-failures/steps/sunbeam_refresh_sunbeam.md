# Step Knowledge: sunbeam_refresh_sunbeam

## Step Overview

Refreshes the Sunbeam control-plane charms from their current risk to `candidate`, driven by
`products/sunbeam/refresh_charms.py` in `sqa-cloud-deployment-pipeline`, executed over SSH
against the Sunbeam bootstrap node (e.g. `solqa-shared-maas-server-10.maas`).

The step walks a sequence of charm **groups** — typically `k8s`, `vault-k8s`, then the
`mysql` group (`mysql-k8s` + `mysql-router-k8s`) — and for each group:

1. `sunbeam manifest show latest` → scp locally → set `core.software.charms.<charm>.channel`
   → scp back.
2. Collect `diagnostics_<ts>_<charms>_before.txt` (juju status + `juju show-unit` for every
   relevant unit + model secrets).
3. `sunbeam cluster refresh <group> -m <manifest>` — **run twice**. The first pass only moves
   the channel; the charm upgrade itself happens on the second pass
   (worked around per `lp:snap-openstack:2164878`).
4. Collect `diagnostics_<ts>_<charms>_after.txt`.
5. Compare `original` / `expected` / `current` revisions and log a summary table.

Because all groups run inside the single `sunbeam_refresh_sunbeam` GitHub step, the step name
tells you nothing about *which* charm failed — always find the last
`Refreshing <charms> ... (attempt N/M)` line before the traceback.

Sibling steps `sunbeam_refresh_mysql` and `sunbeam_refresh_traefik` use the same script and
are skipped when this one fails.

## Swift Artifacts

| Path | Description | When to check |
|---|---|---|
| `generated/github-runner/run.log` | Full runner log — every ssh command, the refresh plan tables, and the traceback | Always — first stop |
| `generated/github-runner/jobs.json` | Step names + conclusions; `run_id` for `gh run view` | Always |
| `generated/sunbeam/manifest_<ts>_<charms>_original.yaml` | Manifest read from clusterd before the edit | Confirm pre-refresh channels |
| `generated/sunbeam/manifest_<ts>_<charms>.yaml` | Edited manifest actually applied | `diff` against `_original` to confirm only the intended keys changed |
| `generated/sunbeam/diagnostics_<ts>_<charms>_before.txt` | juju status + `juju show-unit` for all affected units, pre-refresh | Baseline: scale, revisions, unit IPs, relation data |
| `generated/sunbeam/diagnostics_<ts>_<charms>_after.txt` | Same, post-refresh | **The single most useful file** — upgrade-relation state per unit |
| `generated/sunbeam/juju_debug_log_openstack.txt` | Per-unit hook activity in the `openstack` model | Build the per-unit upgrade timeline |
| `generated/sunbeam/juju_debug_log_controller.txt` | Controller log — agent connect/disconnect, CAAS provisioner events | Pin pod recreation times and scale changes |
| `generated/sunbeam/juju_status_openstack.txt` | Post-failure app table: channel + `charm-rev` | Confirm channel/revision mismatch |
| `generated/sunbeam/kubectl_get_pod.txt` | Pod READY / RESTARTS / AGE | Rule out container-level faults |
| `generated/sunbeam/kubectl_get_pod_detailed.txt` | `-o yaml`-style pod dump | Container statuses, probes |
| `generated/sunbeam/sosreport-*.tar.xz` | Host sosreports | Rarely useful — workloads run in pods, not on the host |
| `generated/happy-bot/` | ⛔ quarantined until Step 8 | Has **charm-container** logs that the archive lacks |

`sunbeam_refresh_*` runs are typically on `tor3-sqa-shared_maas` → **no MAAS logs**; skip the
tgz-extraction steps (4b and 5) entirely.

> **Note:** the archive contains `juju debug-log` output (INFO and above) but **no workload
> container logs**. For mysql-k8s in particular the charm logs the interesting exception type
> only at the charm-container level, so the `happy-bot` cross-check in Step 8 is often the only
> place the concrete exception name appears.

## Grep Patterns

```bash
UUID=<uuid>; D=files/$UUID/generated; R=files/run_<run_id>_failed.log

# Which group was being refreshed when it died, and the plan
grep -a " - INFO - " $R | grep -aiE "Refreshing|attempt [0-9]/|revisions (before|after)|-> "

# The traceback + the failing ssh command
grep -an "Traceback|Process completed with exit code" $R

# Per-unit upgrade timeline for an app (e.g. mysql)
grep -aE "^unit-mysql-[0-9]+:" $D/sunbeam/juju_debug_log_openstack.txt \
  | awk '$2>="13:40:00" && $2<="14:00:00"' \
  | grep -av "proxyupdater|s3caller"

# Upgrade-relation state per unit (the decisive evidence)
grep -a -A45 "endpoint: upgrade$" $D/sunbeam/diagnostics_*_after.txt | grep -aE "upgrade-stack|state:|mysql/"

# Scale changes / pod recreation, from the controller
grep -a "caasapplicationprovisioner ignoring non-existent unit" $D/sunbeam/juju_debug_log_controller.txt
grep -a "agent (disconnected|login): unit-<app>-<n>" $D/sunbeam/juju_debug_log_controller.txt

# Rule out an infrastructure fault
grep -a "^openstack .*<app>-" $D/sunbeam/kubectl_get_pod.txt   # READY / RESTARTS

# Scale + revision before vs after
grep -a "^<app> " $D/sunbeam/diagnostics_*_before.txt
sed -n '1,200p' $D/sunbeam/diagnostics_*_after.txt | grep -a "^<app>/"
```

## Known Failure Patterns

### Pattern 1: mysql-k8s rolling-upgrade wedge — a unit fails to rejoin the InnoDB cluster

**Symptom (GitHub Actions log):**
```
Error: MySQL upgrade failed: Timed out waiting for units to settle to active:
       wait timed out after 3599.999999429998s
Consider rollback to revision <prev>, follow the instructions below: ...

subprocess.CalledProcessError: Command '['ssh', ..., 'sunbeam', 'cluster', 'refresh',
  'mysql', '-m', 'manifest_<ts>_mysql-k8s_mysql-router-k8s.yaml']' returned non-zero exit status 1.
```

**Root cause:** during the rolling charm upgrade one `mysql` pod is recreated on the new
revision and fails to rejoin the InnoDB cluster within the charm's ~8-minute recovery window.
The charm sets that unit's `upgrade`-relation `state: failed` and marks it `blocked` (often
with an **empty** status message). The upgrade coordinator will not advance past a failed unit,
so every unit still listed in `upgrade-stack` stays parked in
`waiting: "other units upgrading first..."` forever, and `sunbeam cluster refresh` aborts on
its 3600 s settle timeout. This is an upstream `mysql-k8s` (data-platform) charm defect, not an
infrastructure, MAAS or Kubernetes fault.

**Evidence to look for:**
- `diagnostics_*_after.txt`, `endpoint: upgrade`: `upgrade-stack: '[0, 1, 2]'` with one unit
  `state: failed`, the already-upgraded units `state: completed`, and the not-yet-upgraded
  units `state: idle`.
- `diagnostics_*_after.txt` status table: the failed unit `blocked` (no message), the `idle`
  units `waiting  "other units upgrading first..."`, upgraded units
  `maintenance  "upgrade completed"`.
- `juju_debug_log_openstack.txt`: for the failed unit, `Recovering unit`, then
  `Failed to get instance replication state`, then `Failed to fetch cluster status` repeating
  every ~15 s for ~8 minutes, ending in
  `ERROR ... Unit failed to rejoin the cluster after upgrade` and
  `ERROR ... Failed to get cluster endpoints`.
  *A single transient `Failed to fetch cluster status` is normal* — healthy units hit it once
  and recover within ~15 s. Only sustained repetition indicates the wedge.
- `kubectl_get_pod.txt`: **all** mysql pods `2/2 Running` with `0` restarts, and the unit still
  reports a `workload-version` → mysqld is up; the failure is at the group-replication layer.
- The failed unit's own `database-peers` local databag may still read
  `member-state: online` — **stale**, written before the restart. Do not use it as evidence.
- Charm-container logs (only via happy-bot / live access) show
  `CRITICAL juju-log Upgrade failed, follow the instructions below to rollback` and
  `charms.mysql.v0.mysql.MySQLGetClusterEndpointsError` looping every ~2 min.

**Aggravating factor seen in practice:** the second refresh attempt may *also* apply a scale
change (e.g. `mysql` 3 → 5) from the manifest, so brand-new members are cloned into the cluster
only minutes before every member is rotated through a restart. Check
`juju_debug_log_controller.txt` for
`caasapplicationprovisioner ignoring non-existent unit with provider id "<app>-<n>"` and compare
the `scale`/unit list in the `_before` vs `_after` diagnostics.

**Diagnosing quickly:** the terminal `state: failed` appears only ~8 minutes into the refresh,
but the step does not fail until the 3600 s timeout expires — so the *failure timestamp in the
GitHub log is ~50 minutes later than the actual breakage*. Always anchor the timeline on the
unit's `blocked`-since timestamp, not on the traceback timestamp.

**Not the root cause:** `vault` units `blocked  "Please unseal Vault"` are a normal steady-state
in these runs and typically pre-date the refresh — check the `since` timestamp before blaming
them.

---

### Pattern 2: the first refresh only moves the channel

Not a failure, but a frequent source of timeline confusion. `refresh_charms.py` logs:

```
Refreshing again: a refresh that changes the channel only moves the applications to it,
the charm upgrade itself happens on the next refresh. (lp:snap-openstack:2164878)
```

So `attempt 1/2` completes quickly with `MySQL refresh complete.` and **no** charm upgrade;
`attempt 2/2` does the real work (and any scale change). When correlating unit activity, use
the `attempt 2/2` start time, not the first `Refreshing ...` line.

---

### Pattern 3: microceph refresh blocked by upgrade guard false-positive on `radosgw`

**Symptom:**
```
Application 'microceph' is not ready: TimeoutError('wait timed out after 1799.999998...s
Error: wait timed out after 1799.999998...s
  app microceph: blocked '(workload) Cannot upgrade, one of microceph|ceph|rados|rbd commands is running'
  unit microceph/0: workload=blocked '(workload) Cannot upgrade, one of microceph|ceph|rados|rbd commands is running'
```

**Root cause:** when the manifest changes `microceph` and its `snap-channel` from
`squid/stable` to `squid/candidate`, the charm leader runs a snap-upgrade guard before
refreshing the snap. The guard uses `pgrep -f` with a broad full-command-line pattern for
`/snap/microceph/.*/(microceph|ceph|rados|rbd)`. On RGW-enabled deployments, the always-running
`radosgw` daemon can false-match that pattern via its config path
`/var/snap/microceph/<rev>/conf/radosgw.conf` (`rados` prefix of `radosgw.conf`). The guard
therefore permanently reports that a microceph/ceph/rados/rbd command is running, blocks the
leader unit, and `sunbeam cluster refresh` fails when its 1800 s readiness wait expires. This
is an upstream `charm-microceph` guard bug, not slow Sunbeam convergence.

**Evidence to look for:**
- The manifest diff changes `core.software.charms.microceph.channel` and
  `core.software.charms.microceph.config.snap-channel` from `squid/stable` to
  `squid/candidate`.
- `_before` diagnostics show `microceph` active on `squid/stable`; `_after` diagnostics show
  `microceph` on `squid/candidate` with only the leader unit blocked and peer units still
  active.
- `juju_debug_log_openstack-machines.txt` repeatedly logs
  `Adding unit status BlockedStatus('(workload) Cannot upgrade, one of microceph|ceph|rados|rbd commands is running')`
  on the leader.
- `microceph_status.txt` shows `rgw` in `Services:` on the microceph nodes. If live access is
  available, verify with
  `sudo pgrep -af '/snap/microceph/.*/(microceph|ceph|rados|rbd)'`; a sole `radosgw ... -c
  /var/snap/microceph/<rev>/conf/radosgw.conf` match confirms the false-positive.

**Not the root cause:** unrelated pre-existing blocked apps (for example hardware observer
resource warnings) when their `since` time predates the refresh and `sunbeam cluster refresh`
explicitly reports `microceph` as the non-ready app.

---

_Add more patterns below as they are discovered._

## Notes

- The step aggregates several charm groups; `k8s` and `vault-k8s` succeeding tells you nothing
  about the mysql group — always locate the last `Refreshing ... (attempt N/M)` line.
- `diagnostics_*_{before,after}.txt` are large (often ~14k lines). Use
  `grep -an "^===== <unit> ====="` to find section offsets, then `sed -n`.
- Unit pod IPs change across a pod recreation; the `_before`/`_after` diagnostics are the
  easiest way to spot which units actually restarted.
- The `mysql` workload container error log (`/var/log/mysql/error.log`) is **not** collected —
  the MySQL-level reason for a rejoin failure is generally unrecoverable post-mortem. Treat
  that as a known blind spot and say so rather than speculating.
