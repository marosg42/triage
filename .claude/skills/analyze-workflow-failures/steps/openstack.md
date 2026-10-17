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

### Pattern 4: LXD container stuck in "Instance already exists" provisioning loop

**Symptom:**
```
# juju-wait abort (generated/openstack/log.txt):
ERROR:root:Machine <m>/lxd/<n> is in down for 30 minutes.
subprocess.CalledProcessError: Command ['juju-wait', ..., '--machine-error-timeout', '1800', ...] returned non-zero exit status 1.

# Host machine agent log (crashdump <m>/baremetal/var/log/juju/machine-<m>.log):
INFO  juju.container.lxd starting new container "juju-<uuid>-<m>-lxd-<n>" (image ...)
WARNING juju.worker.lxdprovisioner machine <m>/lxd/<n> failed to start:
        Failed creating instance record: Instance "juju-<uuid>-<m>-lxd-<n>" already exists ... retrying
ERROR juju.worker.lxdprovisioner cannot start instance for machine "<m>/lxd/<n>":
        Failed creating instance record: Instance "juju-<uuid>-<m>-lxd-<n>" already exists
```

**Root cause:** Juju's LXD provisioner issues the container create; the instance record is
created on the LXD side, but Juju never records a successful result (lost/timed-out API
response). Every retry then fails with `Instance "..." already exists` because the orphaned
record from the first attempt is never cleaned up. The container keeps `instance-id: pending`,
status `down / Creating container`, and its unit stays `waiting / allocating`. If that unit is
NOT in the juju-wait `-x` exclusion list, juju-wait's `--machine-error-timeout 1800` (30 min)
fires and the openstack step fails. Often several containers on the **same host machine** stall
in the same window → points to a transient LXD/host issue on that specific bare-metal node.

**Evidence to look for:**
- `<crashdump>/juju_status.txt`: `<m>/lxd/<n> down ... pending ... Creating container`; affected unit `waiting/allocating "waiting for machine"`.
- `<crashdump>/<m>/baremetal/var/log/juju/machine-<m>.log`: repeated `Instance "..." already exists` retries ending in `cannot start instance`.
- The host machine itself is `started/Deployed` and reachable (its `baremetal/` dir is populated) — failure is container-level, not bare-metal provisioning.
- Model UUID prefix in the instance name matches this run's model → stale record is from THIS run's first create attempt, not a previous run.

**Investigation steps:**
1. From juju-wait error, note the stuck `<m>/lxd/<n>` and find its unit in `juju_status.txt`.
2. Extract `machine-<m>.log` from the crashdump and grep `<m>/lxd/<n>` + `already exists`.
3. Confirm the host machine `<m>` is healthy and check whether sibling `<m>/lxd/*` containers stalled in the same window.

**Recommendation:** Transient — re-run usually succeeds. If recurring on one host, check that
host's LXD daemon health, disk/inode pressure, and image cache, and remove orphaned
`juju-*-<m>-lxd-*` instances Juju failed to adopt.

**From run:** 28003993883 (UUID 4794cd6e, tor3-sqa-shared_maas dh1_j8_1, branch main, 2026-06-23; designate/1 on 4/lxd/2)

---

### Pattern 5: Host bare-metal machine has no device in a required network space (ceph LXD containers stuck)

**Symptom:**
```
# juju-wait abort (generated/openstack/log.txt):
ERROR:root:Machine <m>/lxd/<n> is in down for 30 minutes.
subprocess.CalledProcessError: Command ['juju-wait', ..., '--machine-error-timeout', '1800', ...] returned non-zero exit status 1.

# juju_status_foundations-maas_openstack.txt:
<m>/lxd/<n>  down  pending  ubuntu@24.04  host machine "<m>" has no available device in space(s) "cloud-ceph-access", "cloud-ceph-replicate"
```

**Root cause:** The openstack bundle deploys ceph-related units (ceph-mon, ceph-radosgw,
glance, gnocchi, cinder, etc.) with Juju network-space constraints such as
`spaces=cloud-ceph-access,cloud-ceph-replicate,oam`. To give an LXD container a NIC in a
space, the **host bare-metal machine** must have an interface/bond/VLAN linked to a MAAS
subnet in that space. If the chosen host lacks a device in the required space, Juju cannot
create the container NIC and the container stays `down / pending` with the explicit message
`host machine "<m>" has no available device in space(s) "<space>"`. Containers on the same
host that do **not** require that space start fine — so the host is up and its LXD daemon is
healthy; only space-constrained containers stall. If the affected unit is not in the
juju-wait `-x` exclusion list, `--machine-error-timeout 1800` (30 min) fires and the step
fails.

**Key distinguishing features:**
- The juju status message names the missing space(s) directly — no need to infer.
- The host machine is `started / Deployed` and reachable; sibling containers on it are up.
- This is a **deterministic host/MAAS network configuration gap**, not transient — it will
  recur on every run where Juju places a ceph container on that specific host. Look at the
  host **hostname** (e.g., `solqa-shared-maas-server-19`): the same physical server can fail
  this way across multiple runs even though its Juju machine number differs each run.

**Evidence to look for:**
- `juju_status_foundations-maas_openstack.txt`: grep `no available device in space` — lists
  every stuck container and the missing space(s).
- Cross-check the stuck `<m>/lxd/<n>` against its unit (grep the lxd id) — typically
  ceph-mon/ceph-radosgw/glance/gnocchi.
- The host machine line (`grep "^<m> "`) shows the hostname; non-ceph containers on the same
  host are `started`.

**Investigation steps:**
1. From the juju-wait error, note the stuck `<m>/lxd/<n>`.
2. `grep "no available device in space" juju_status_foundations-maas_openstack.txt` to list all affected containers and spaces.
3. `grep "^<m> " juju_status_foundations-maas_openstack.txt` to get the host hostname.
4. Map containers to units to confirm they are ceph/space-constrained apps.

**Recommendation:** Fix MAAS networking on the named host so it has an interface in the
required space(s), matching its peers; or remove/tag the host so ceph units aren't scheduled
onto it. Not a re-run fix — it recurs until the host's MAAS network config is corrected.

**From runs:** 28051182800 (UUID 2e9902cf, shared_maas, branch main, 2026-06-23; ceph-mon/0 on
3/lxd/1 + glance/0 on 3/lxd/3, host solqa-shared-maas-server-19) and 28049265712 (UUID
09542059, shared_maas, branch main, 2026-06-23; ceph-radosgw/0 on 6/lxd/2 + gnocchi/0 on
6/lxd/5, same host solqa-shared-maas-server-19).

---

### Pattern 6: rabbitmq-server unit stuck in error — `rabbit` app left stopped after coordinated cluster join (juju-wait SIGKILL)

**Symptom:**
```
# juju-wait abort (GitHub log / generated/openstack/log.txt):
subprocess.CalledProcessError: Command '['juju-wait', '-m', 'foundations-maas:openstack',
  '-t', '14400', '--machine-error-timeout', '1800', '--retry_errors', '5', ...]'
  died with <Signals.SIGKILL: 9>.
# (rabbitmq-server is NOT in the -x exclusion list)

# crashdump juju_status.txt:
rabbitmq-server/2  error    idle  8/lxd/2  ... hook failed: "amqp-relation-changed" for heat:amqp
rabbitmq-server/0* waiting  idle  6/lxd/1  ... RabbitMQ is clustered, but not all charms are ready
# all AMQP consumers waiting: barbican/cinder/designate/glance/heat/neutron-api/
# nova-cloud-controller → "Incomplete relations: messaging" / "'amqp' incomplete"

# unit-rabbitmq-server-<n>.log (crashdump <m>/lxd/<n>/var/log/juju/):
Error: this command requires the 'rabbit' app to be running on the target node.
       Start it with 'rabbitmqctl start_app'.
subprocess.CalledProcessError: Command '['/usr/sbin/rabbitmqctl', 'cluster_status',
  '--formatter=json']' returned non-zero exit status 64.
```

**Root cause:** To join the RabbitMQ cluster, the charm runs `rabbitmqctl stop_app`,
`join_cluster`, then `start_app`, serialized across peers by the charm's coordinator
("Restart lock" / "Cluster lock"). On the affected unit the app is stopped for the join but
the restart/cluster lock is **never re-granted** afterwards ("Restart lock not granted"
repeatedly; no `start_app` command ever runs again). With the `rabbit` app down, the charm's
`assess_status` path (`amqp-relation-changed` → `assess_cluster_status` → `clustered()` →
`running_nodes()` → `get_cluster_status()`) calls `rabbitmqctl cluster_status --formatter=json`,
which exits 64 ("requires the 'rabbit' app to be running"). This raises `CalledProcessError`
and fails the hook — converting a transient "app stopped to join" window into a **permanent**
hook error. The unit never leaves `error`, so the amqp/`messaging` relation never completes
for any consumer, and since rabbitmq-server is not excluded from juju-wait, the wait runs its
full `-t 14400` (4h) and is SIGKILLed.

**Key distinguishing features:**
- juju-wait dies with **SIGKILL** (its own 4h timeout), not exit status 1.
- Exactly one rabbitmq-server unit is in `error`; the peers are `waiting/idle` "RabbitMQ is
  clustered, but not all charms are ready".
- The whole OpenStack control plane is `waiting` on "messaging"/"amqp incomplete" — a single
  rabbitmq unit fans out to a full-deployment stall.
- Timeline: single "Restart lock granted" early, then `rabbitmqctl stop_app`, then only
  "Restart lock not granted" — the hook error repeats every retry (e.g. ~52 times) until crashdump.

**Evidence to look for:**
- `unit-rabbitmq-server-<n>.log`: `stop_app` executed once, no subsequent `start_app`; repeated
  `cluster_status ... exit status 64` and "requires the 'rabbit' app to be running".
- `juju_status_foundations-maas_openstack.txt` / crashdump `juju_status.txt`: one rabbitmq unit
  in error on `amqp-relation-changed`, all AMQP consumers waiting on messaging.
- GitHub log: juju-wait `-x` list does NOT contain `rabbitmq-server`.

**Investigation steps:**
1. Confirm juju-wait died with SIGKILL and rabbitmq-server is not excluded.
2. Find the errored unit in the crashdump juju_status.txt; note its `<m>/lxd/<n>`.
3. In `unit-rabbitmq-server-<n>.log`, grep `stop_app`, `start_app`, `Restart lock`, and
   `cluster_status` to confirm the app was stopped and never restarted.

**Recommendation:** Charm-level — `assess_status` should treat `cluster_status` exit 64 /
"app not running" as a transient waiting state instead of raising and hard-failing the hook,
so the node recovers on the next coordinator turn. Pipeline-level — auto `start_app` +
`juju resolved` the stuck unit within the wait window. This is a timing-dependent
coordinated-restart race, not stale state; a re-run usually succeeds.

**From run:** 30566384159 (UUID 6dd5b264, tor3-sqa-shared_maas dh1_j8_2, branch main,
2026-07-30; rabbitmq-server/2 on 8/lxd/2).

---

### Pattern 7: `configure_network` NameResolutionError — runner cannot resolve the MAAS-managed zone

**Symptom (right after `Starting step: openstack:configure_network`):**
```
socket.gaierror: [Errno -3] Temporary failure in name resolution
urllib3.exceptions.NameResolutionError: ... keystone.<cluster>...solutionsqa:5000
keystoneauth1.exceptions.connection.ConnectFailure: Unable to establish connection to https://keystone.<cluster>...solutionsqa:5000/...
```
Five attempts about 20 s apart, then the step fails.

**Root cause:** UDP/53 from the runner network (`10.152.185.0/24`) to MAAS DNS gets no reply,
while TCP traffic (including Juju) works. MAAS DNS is configured on the runner by the
*Set DNS* step (`custom-dns.conf`, `maas-dns-servers.conf` drop-ins). This is the first
runner-side lookup of a cluster FQDN, so earlier layers pass. The cloud and the DNS record are
fine. This is an environment/network issue.

**Evidence to look for (in the MAAS archive):**
- `generated/github-runner/run.log`: `maas ... dnsresources create fqdn=keystone...` succeeded.
- `var/snap/maas/current/bind/zone.<domain>` contains the keystone record.
- The BIND config has `allow-query { any; }`, `named` is listening on the DNS VIP, and the VIP
  stayed on one node in pacemaker during the window.

**Fix:** restore UDP/53 from the runner network to MAAS DNS, or add a DNS delegation. Stopgap:
`/etc/hosts` entries. Pipeline: add a pre-check that resolves the keystone FQDN before
`configure_network`.

**From run:** 38626738859 (tor3-sqa-dedicated_maas dh1_j2) and 38572507468
(tor3-sqa-dedicated_maas dh1_j6).

---

### Pattern 8: juju-wait exit 1 on a transient controller-API connection drop (`connection is shutdown before send`)

**Symptom (mid-wait, well before the 14400 s timeout):**
```
ERROR:root:ERROR connection is shutdown before send: connection is shut down
ERROR:root:juju status --format=json failed: 1
subprocess.CalledProcessError: Command '['juju-wait', '-m', 'foundations-maas:openstack', '-t', '14400', ...]' returned non-zero exit status 1.
```

**Root cause:** juju-wait's internal `juju status` poll lost its websocket to the controller.
juju-wait does not retry transport errors (`--retry_errors` covers only hook errors), so one
dropped connection fails the layer, even while the model is healthy and converging.

**Evidence to look for:**
- Controller crashdump (`generated/juju_maas_controller/juju-crashdump-controller-*.tar.gz`):
  grep the rotated `machine-<n>-*.log.gz` files, not only `machine-<n>.log`, which may start
  after the failure. Look for
  `error serving RPCs: ... read tcp <ctrl>:17070-><runner NAT IP>:<port>: read: connection reset by peer`
  about 1 min *before* the client error, right after an `authn_login_success:user-admin`.
- `TLS handshake error from <runner IP>: EOF` lines are constant background noise (the HA
  client dials all addresses), so do not treat them as the cause. The same applies to the
  steady `leaseexpiry ... database is locked` flood and to the `dialed mongodb` redial that
  repeats about every 31 s: compare rates before and after the failure before blaming them.
- The openstack crashdump `juju_status.txt` (taken later) shows the non-excluded apps active,
  which means the failure was a false negative.

**Recommendation:** make FCE/juju-wait retry with the remaining budget on transport errors.
Also look at the runner → controller NAT path (conntrack/firewall RSTs). Re-running the layer
usually succeeds.

**From run:** 38660190213 (UUID c0112d76, tor3-sqa-dedicated_maas dh1_j6, branch main,
2026-10-15; reset on controller 10.241.144.82 from 91.189.92.152 at 05:03:49).

---

## Notes

- The `juju_status_foundations-maas_openstack.json` / `.txt` files are captured **at bundle deploy time**, not at failure time. For the authoritative post-failure status, always use the crashdump's `juju_status.txt`.
- vault is always excluded via `-x vault` in juju-wait, but the `juju exec is-leader` probe can still target vault units.
- The juju-wait timeout is 14400 seconds (4 hours), with `--machine-error-timeout 1800` (30 min). The machine error timeout only fires if juju reports the machine in an error state — it does NOT catch machines that appear "allocating/idle" but are actually unreachable.
- `hacluster-vault` with 0 units in juju status is expected when vault deployment is not yet complete.

