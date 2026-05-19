# Step Knowledge: openstack

## Step Overview

The `openstack` layer deploys the full OpenStack charm bundle onto bare-metal machines
provisioned by MAAS. It uses FCE (`fce build openstack`) which: deploys the Juju bundle
(`juju deploy bundle.yaml`), waits for all units to become ready (`juju-wait`), then runs
post-deploy validation. The model is `foundations-maas:openstack`.

Key components deployed: vault, mysql-innodb-cluster, rabbitmq-server, keystone, nova,
neutron, glance, cinder, ceph-osd, ceph-mon, ceph-radosgw, ovn-central, ovn-chassis,
octavia, barbican, designate, heat, placement, openstack-dashboard, and subordinates.

## Swift Artifacts

Objects stored under `<uuid>/generated/openstack/` that are useful for diagnosing failures:

| Path | Description | When to check |
|---|---|---|
| `generated/openstack/log.txt` | FCE build log for this layer | Always — first stop |
| `generated/openstack/juju_status_foundations-maas_openstack.json` | Juju status JSON snapshot (captured at bundle deploy time, NOT at failure time) | Check unit/machine state at deploy start |
| `generated/openstack/juju_status_foundations-maas_openstack.txt` | Human-readable juju status (same timing caveat) | Quick overview of all apps |
| `generated/openstack/juju-crashdump-openstack-<timestamp>.tar.gz` | Full juju crashdump (captured at failure time) | Deep investigation: agent logs, juju status at failure |
| `generated/openstack/juju-dump-db-openstack-<timestamp>.tar.gz` | Juju database dump | Rarely needed |
| `generated/openstack/bundle.yaml` | The deployed bundle | Verify config/overlays applied |
| `generated/openstack/juju-lint.out` | juju-lint pre-validation output | Check for lint errors before deploy |
| `generated/openstack/model_config.yaml` | Juju model configuration | Verify model settings |

## Key Log Files (inside juju-crashdump tgz)

The crashdump is extracted under `<uuid-dir>/` and contains per-machine directories named
by machine number (e.g., `9/baremetal/`, `1/lxd/0/`, etc.).

| File | What it contains | When to use |
|---|---|---|
| `juju_status.txt` | Juju status at crash time (authoritative — NOT the early snapshot) | Always — actual state at failure |
| `juju_status.yaml` | YAML version of above | Parsing/scripting |
| `<machine>/baremetal/var/log/juju/unit-<app>-<n>.log` | Unit agent log on bare metal | Unit-level charm/agent errors |
| `<machine>/lxd/<n>/var/log/juju/unit-<app>-<n>.log` | Unit agent log in LXD container | Container unit errors |
| `<machine>/baremetal/` | **Empty directory = machine was unreachable via SSH** | Key evidence for bare-metal provisioning failure |

## Grep Patterns

```bash
# Find the primary failure in log.txt
grep -n "ERROR\|CalledProcessError\|timed out\|failed:" generated/openstack/log.txt | tail -30

# Find juju-wait invocation and its arguments
grep -n "juju-wait\|juju exec\|is-leader" generated/openstack/log.txt | grep -v "^Binary"

# Find which units were stuck (not idle) during juju-wait
grep -n "juju agent status is\|workload status is" generated/openstack/log.txt | grep -v "idle\|active" | tail -50

# Find the "all units idle" line (juju-wait success report)
grep -n "All units idle" generated/openstack/log.txt

# Check machine provisioning state in early juju status JSON
python3 -c "
import json
with open('generated/openstack/juju_status_foundations-maas_openstack.json') as f:
    d = json.load(f)
for mid, m in d.get('machines', {}).items():
    ms = m.get('machine-status', {})
    js = m.get('juju-status', {})
    if ms.get('current') not in ('running', 'deployed') or js.get('current') != 'started':
        print(f'Machine {mid}: juju={js.get(\"current\")}, maas={ms.get(\"current\")}, msg={ms.get(\"message\")}, host={m.get(\"hostname\")}, ip={m.get(\"ip-addresses\")}')
"

# Check vault status in crashdump juju_status
grep -A 5 "^vault " <crashdump>/juju_status.txt
grep "vault/0" <crashdump>/juju_status.txt

# Check if a machine directory is empty (unreachable)
find <crashdump>/<machine_id>/baremetal/ -type f | wc -l
# If output is 0, the machine was SSH-unreachable at crashdump time
```

## Known Failure Patterns

### Pattern 1: Bare-metal machine stuck in MAAS provisioning (vault/0 juju exec timeout)

**Symptom:**
```
INFO:root:All units idle since 2026-03-22 18:22:54.937035Z (..., vault/0)
ERROR:root:ERROR timed out waiting for results from: unit vault/0
ERROR:root:juju exec --format=yaml --unit vault/0 -- is-leader --format=json failed: 1

subprocess.CalledProcessError: Command '['juju-wait', '-m', 'foundations-maas:openstack',
  '-t', '14400', ..., '-x', 'vault', ...]' returned non-zero exit status 1.
```

**Root cause:** A bare-metal machine assigned to `vault/0` (or another unit) failed to
progress through MAAS deployment — it stalls at "Deploying: Loading ephemeral" or a
similar intermediate state. The Juju agent never fully starts, so the unit remains in
`allocating` state. Despite vault being excluded from juju-wait via `-x vault`, juju-wait
still dispatches `juju exec -- is-leader` to vault/0 as part of its leader-election probe.
The controller may briefly report vault/0 as "idle" (transient controller state), causing
juju-wait to attempt the exec, which then times out because the machine is unreachable.

**Evidence to look for:**
- `generated/openstack/juju_status_foundations-maas_openstack.json`: vault/0's machine shows `machine-status: "allocating", message: "Deploying: Loading ephemeral"` captured during deploy
- `<crashdump>/juju_status.txt`: vault/0 shows `waiting / allocating` — still not up at failure time
- `<crashdump>/<machine_id>/baremetal/` is an **empty directory** — crashdump could not SSH in
- `generated/openstack/log.txt`: "All units idle" briefly reports vault/0 idle, then immediately `timed out waiting for results from: unit vault/0`

**Investigation steps:**
1. Identify vault/0's machine number from `juju_status_foundations-maas_openstack.json`
2. Check that machine's `machine-status` in the JSON (should show "Deploying: Loading ephemeral" or similar stuck state)
3. Check the crashdump juju_status.txt for vault/0 status at failure time
4. Verify the machine's baremetal dir in the crashdump is empty (unreachable)
5. The MAAS substrate logs (from the MAAS layer's generated artifacts) may show the machine's event history

**Note:** This pattern can affect any bare-metal unit, not just vault. The symptom is
always `juju exec timed out` for the specific unit whose machine failed to provision.

---

### Pattern 2: juju-wait timeout on a non-vault charm unit

**Symptom:**
```
ERROR:root:ERROR timed out waiting for results from: unit <charm>/<n>
subprocess.CalledProcessError: Command ['juju-wait', ..., '-t', '14400', ...] returned non-zero exit status 1.
```

**Root cause:** A charm unit that is NOT excluded from juju-wait failed to reach an
`active/idle` state within the 4-hour timeout. This could be caused by a charm hook error,
a missing relation, or a dependent service being unavailable. Check the unit's workload
status message in the juju status and the unit's charm agent log in the crashdump.

**Evidence to look for:**
- `generated/openstack/log.txt`: repeated `DEBUG:root:<unit> workload status is <status>` for the stuck unit
- `<crashdump>/juju_status.txt`: unit shows non-idle agent or non-active workload with an error message
- `<crashdump>/<machine>/[baremetal|lxd/<n>]/var/log/juju/unit-<charm>-<n>.log`: charm hook errors

---

### Pattern 3: octavia-ovn-chassis hook error — `ovs-vsctl` not found in LXD container

**Symptom:**
```
subprocess.CalledProcessError: Command ['juju-wait', '-m', 'foundations-maas:openstack',
  '-t', '14400', ..., '--retry_errors', '5', ...] died with <Signals.SIGKILL: 9>.

# Unit status:
octavia-ovn-chassis/N  error  idle  <machine>/lxd/<n>
  Message: hook failed: "ovsdb-subordinate-relation-joined" for octavia-ovn-chassis:ovsdb-subordinate

# Hook traceback (unit-octavia-ovn-chassis-N.log in crashdump):
File ".../hooks/relations/ovsdb-subordinate/provides.py", line ..., in _get_ovs_value
  cp = subprocess.run(('ovs-vsctl', 'get', tbl, rec or '.', col), ...)
FileNotFoundError: [Errno 2] No such file or directory: 'ovs-vsctl'
```

**Root cause:** `ovs-vsctl` (from `openvswitch-switch` or equivalent snap) is absent in the
specific LXD container hosting the affected `octavia-ovn-chassis` unit. The
`ovsdb-subordinate-relation-joined` hook calls `ovs-vsctl get Open_vSwitch . external_ids:hostname`
to publish the chassis name. If the package failed to install (e.g., transient apt/network
issue at charm install time), the binary is missing and the hook fails with
`FileNotFoundError` on every invocation, including after `juju resolve`.

**Key distinguishing features:**
- Only one of N units is affected; the others are `active/idle` — package install failed on
  one specific machine/container.
- The error first appears around the time the `ovsdb-subordinate` relation is formed (well
  before the second juju-wait starts).
- The openstack layer uses **two** juju-wait passes: the first (at bundle-deploy time)
  excludes `octavia-ovn-chassis`; the second (post-configuration) does **not**. The unit
  passes the first wait silently and only blocks the second, causing a 4-hour timeout.
- Process termination is SIGKILL (juju-wait's own 14400s timeout expired), not exit code 1.

**Evidence to look for:**
- `unit-octavia-ovn-chassis-N.log` in the crashdump: `FileNotFoundError: 'ovs-vsctl'`
  repeated from initial hook execution onwards.
- `juju_status_foundations-maas_openstack.txt`: other `octavia-ovn-chassis` units are
  `active`; only one is in error.
- GitHub Actions log: two `juju-wait -m foundations-maas:openstack` invocations; the second
  omits `octavia-ovn-chassis` from the `-x` exclusion list.

**Investigation steps:**
1. Find the failing unit and machine: `grep "octavia-ovn-chassis" generated/openstack/juju_status_foundations-maas_openstack.txt`
2. Extract crashdump: `tar -xzf generated/openstack/juju-crashdump-openstack-*.tar.gz -C crashdump-openstack`
3. Read unit log: `tail -100 crashdump-openstack/<uuid>/<machine>/lxd/<n>/var/log/juju/unit-octavia-ovn-chassis-<N>.log`
4. Confirm first failure timestamp vs. juju-wait start times in `log.txt`

**From run:** 23356105586 (UUID c0a6632d, tor3-sqa-shared_maas dh1_j9_1, branch main, 2026-03-20)

---

## Notes

- The `juju_status_foundations-maas_openstack.json` / `.txt` files are captured **at bundle deploy time**, not at failure time. For the authoritative post-failure status, always use the crashdump's `juju_status.txt`.
- vault is always excluded via `-x vault` in juju-wait, but the `juju exec is-leader` probe can still target vault units.
- The juju-wait timeout is 14400 seconds (4 hours), with `--machine-error-timeout 1800` (30 min). The machine error timeout only fires if juju reports the machine in an error state — it does NOT catch machines that appear "allocating/idle" but are actually unreachable.
- `hacluster-vault` with 0 units in juju status is expected when vault deployment is not yet complete.

## Version History

- **v1.0** (2026-03-30): Initial version, based on analysis of run fbca1fd5-9264-4007-af42-62700e5afb3c
- **v1.1** (2026-04-01): Added pattern: `octavia-ovn-chassis` hook error — `ovs-vsctl` not found in LXD container; from run 23356105586 (UUID c0a6632d, tor3-sqa-shared_maas dh1_j9_1, 2026-03-20).
