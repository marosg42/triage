# Step Knowledge: deploy_charm_mysql

## Step Overview

Deploys the MySQL charm (and related charms) into a Juju model on an OpenStack controller.
It deploys `mysql` (3 units), `self-signed-certificates`, and `data-integrator`, integrates
them, then waits up to 60 minutes for all units to reach `active` workload status using
`juju wait-for`.

## Swift Artifacts

Objects stored under `<uuid>/generated/deploy_charm_mysql/` that are useful for diagnosing
failures:

| Path | Description | When to check |
|---|---|---|
| `generated/deploy_charm_mysql/juju_status_foundation-openstack_mysql.txt` | Human-readable juju status of mysql model at failure time | Always — shows unit states |
| `generated/deploy_charm_mysql/juju_status_foundation-openstack_mysql.json` | JSON juju status of mysql model at failure time | Programmatic parsing of unit/app states |
| `generated/deploy_charm_mysql/juju_status_foundation-openstack_controller.txt` | Juju status of controller model | Check OpenStack infra health |
| `generated/juju_openstack_controller/juju-crashdump-controller-<timestamp>.tar.gz` | Full Juju controller crashdump | Deep investigation — contains logsink.log with all unit charm logs |

### Key files inside the crashdump

| File | What it contains | When to use |
|---|---|---|
| `var/log/juju/logsink.log` | All unit charm logs forwarded to the controller — filter by model UUID | Primary investigation for charm-level errors |
| `var/log/juju/models/admin-mysql-<id>.log` | Controller-side model worker log (provisioner, firewaller) | VM provisioning issues, MessagingTimeout |
| `var/log/juju/machine-0.log` | Controller machine worker log | Controller-side issues |

## Key Log Files

No MAAS logs for this step — substrate is `tor3-sqa-sunbeam` (OpenStack VMs, no MAAS
infrastructure logging). Investigation relies on GitHub Actions logs and Swift juju status
snapshots.

## Grep Patterns

```bash
# Find the timeout error in GitHub Actions log
grep "timed out waiting\|##\[error\]" <work_dir>/run_<run_id>_failed.log | grep -i mysql

# Check unit states at failure time
cat <work_dir>/<uuid>/generated/deploy_charm_mysql/juju_status_foundation-openstack_mysql.txt

# Parse unit workload statuses from JSON
python3 -c "
import json
with open('<work_dir>/<uuid>/generated/deploy_charm_mysql/juju_status_foundation-openstack_mysql.json') as f:
    data = json.load(f)
for app_name, app in data.get('applications', {}).items():
    for unit_name, unit in app.get('units', {}).items():
        ws = unit.get('workload-status', {})
        print(f'{unit_name}: {ws.get(\"current\")} - {ws.get(\"message\", \"\")}')
"
```

## Known Failure Patterns

### Pattern 1: juju wait-for 60m timeout — mysql/N stuck due to mysqld socket unavailable on certificates-relation-joined

**Symptom (GitHub Actions log):**
```
ERROR timed out waiting for "mysql" to reach goal state
Process completed with exit code 1.
```

The `juju wait-for model mysql --query='forEach(units, unit => unit.workload-status=="active")' --timeout 60m`
command prints `model "mysql" found, waiting...` repeatedly for 60 minutes then exits.

**Symptom (juju controller logsink.log / crashdump):**
```
unit-mysql-1 ERROR  certificates:3: ERROR 2002 (HY000): Can't connect to local MySQL server through socket '/var/snap/charmed-mysql/common/var/run/mysqld/mysqld.sock' (2)
unit-mysql-1 ERROR  certificates:3: Failed to list roles
unit-mysql-1 ERROR  certificates:3: Uncaught exception while in charm code:
unit-mysql-1 ERROR  hook "certificates-relation-joined" failed: exit status 1
unit-mysql-1 WARNING certificates:3: mysqld is not running, skipping flush host cache
unit-mysql-1 WARNING Failed to check if local cluster metadata exists  ← repeats every ~5s
unit-mysql-1 DEBUG  Deferring <StartEvent via MySQLOperatorCharm/on/start[37]>
```

**Root cause:** A bootstrapping race condition in the mysql charm. The `certificates-relation-joined`
hook fires on `mysql/N` before `mysqld` has fully started on that VM. The hook attempts to call
`mysqlsh` / list roles over the Unix socket, fails with `ERROR 2002 (HY000)`, and the charm
enters a retry/defer loop. The charm repeatedly warns "Failed to check if local cluster metadata
exists" every ~5 seconds for the duration of the 60-minute timeout. The unit stays in
`maintenance / Setting up cluster node` the entire time. Even after the juju wait-for timeout,
the charm continues its retry loop indefinitely.

This is unrelated to VM performance — it is a charm-level race between mysqld startup and the
`certificates-relation-joined` hook dispatch, triggered by fast VM provisioning (machines start
within ~2 minutes, leaving insufficient mysqld startup time).

**Bug report:** https://github.com/canonical/mysql-operators/issues/189

**Evidence to look for:**
- `juju_status_foundation-openstack_mysql.txt`: one mysql unit showing `maintenance` /
  `Setting up cluster node` while other units are `active`
- Crashdump `var/log/juju/logsink.log` (filter by mysql model UUID):
  - First ERROR at ~13:14:54 on `certificates-relation-joined` with socket error
  - Repeating WARNING `"Failed to check if local cluster metadata exists"` starting ~13:15:00
  - `LOGS_TYPE=ERROR ... flush_mysql_logs` run-commands entries every 2 minutes
- Crashdump `var/log/juju/models/admin-mysql-<id>.log`:
  - `MessagingTimeout` retries for machine 4 (data-integrator VM) at provisioning time indicate
    general Nova API pressure during the deployment window

**How to find the mysql model UUID for log filtering:**
```bash
ls <work_dir>/crashdump/*/0/baremetal/var/log/juju/models/
# Look for admin-mysql-XXXXXX.log — the XXXXXX is a short prefix of the model UUID
grep "423a8d" <work_dir>/crashdump/.../logsink.log  # replace with actual prefix
```

**Example (run 23343431862, UUID d4685121):**
```
# juju status at timeout (14:05:29Z, 60m after deploy start at 13:05:28Z):
Unit      Workload     Agent      Message
mysql/0*  active       idle       Primary
mysql/1   maintenance  executing  Setting up cluster node   ← never recovered
mysql/2   active       idle

# First error in logsink at 13:14:48 (only ~9 minutes after deploy):
unit-mysql-1 WARNING  mysqld is not running, skipping flush host cache
unit-mysql-1 ERROR    certificates:3: Can't connect to MySQL socket (2)
unit-mysql-1 ERROR    hook "certificates-relation-joined" failed: exit status 1
# Still retrying at 14:19 (14 minutes after timeout expired)
```

---

### Pattern 2: juju wait-for 60m timeout — Juju agent never connected (Nova ACTIVE, machine Juju `pending`)

**Symptom (GitHub Actions log):**
```
ERROR timed out waiting for "mysql" to reach goal state
Process completed with exit code 1.
```

**Symptom (juju status at timeout):**
```
Unit     Workload  Agent       Message
mysql/0  active    idle        Primary
mysql/1* active    idle
mysql/2  waiting   allocating  waiting for machine

Machine  State    Address        Inst id (Nova)    Message
2        pending  192.168.1.197  5c270182-...      ACTIVE
```

The Nova instance exists (has inst_id, address, Nova status `ACTIVE`) but the Juju machine
state is `pending`. Unit and its storage remain pending the entire 60 minutes.

This pattern has also been observed on auxiliary machines (`self-signed-certificates` and
`data-integrator`, both `ubuntu@24.04`) in run ac4e7f36, while the three mysql machines
(`ubuntu@22.04`) all provisioned successfully.

**Distinguishing from the mysqld socket race pattern:**
- **This pattern**: unit in `waiting / allocating`, machine in Juju `pending` — agent never connected
- **Socket race pattern**: unit in `maintenance / Setting up cluster node` — agent *did* connect but charm hook failed

**⚠️ Key diagnostic signal — check console log immediately:**
```bash
openstack console log show <inst_id>
```

Look for these specific cloud-init messages:
```
Used fallback datasource         ← ROOT CAUSE: cloud-init fell back to NoCloud
ci-info: no authorized SSH keys  ← user-data (juju bootstrap) was never executed
```

If `Used fallback datasource` appears in the console log, **stop and attribute the bug to
`cloud-init` or the Ubuntu image**, NOT to Juju. This is the definitive root cause signal.
Marvin agents have historically mistaken this message as benign — it is not.

**Root cause: OVN metadata proxy race condition (confirmed by live investigation)**

This Sunbeam OpenStack environment:
- Does **not** set `force_config_drive` — VMs rely entirely on the IMDS (Instance Metadata
  Service) at `169.254.169.254` to receive user-data (the Juju bootstrap script)
- Uses `neutron-ovn-metadata-agent` running on each hypervisor with a per-network HAProxy
  namespace that proxies metadata requests to `nova-api-metadata`

Race mechanism:
1. Nova marks the instance `ACTIVE` and signals the hypervisor
2. **Asynchronously**: OVN notifies the metadata agent to create the HAProxy namespace for
   the instance's network (takes seconds to complete)
3. The VM boots and cloud-init begins datasource detection, querying 169.254.169.254
4. **If cloud-init queries BEFORE the HAProxy namespace is ready**: connection is refused
5. cloud-init falls back to the NoCloud/fallback datasource
6. user-data (Juju bootstrap script) is never executed; network not configured; Juju agent
   never starts

Ubuntu 24.04 has a shorter/stricter datasource detection window than Ubuntu 22.04, making
it far more susceptible to this race. This explains why:
- All three `ubuntu@22.04` mysql machines succeed (tolerant of the timing window)
- Both `ubuntu@24.04` auxiliary machines fail (strict detection window, fall through faster)

**Measured timing from live system:**
- Port binding → HAProxy namespace ready: a few seconds (usually before VM's cloud-init)
- First `user_data` response latency: up to 2.2 seconds (cold nova-api-metadata cache)
- OVN SB connection drops (`ssl:<ovn-relay>:6642: receive error`) observed on hypervisors —
  can delay the metadata agent's port binding notification, extending the race window

**Intermittent nature:**
- On a lightly loaded or fast OpenStack the race window closes before cloud-init detects it
- On a slow/loaded OpenStack (e.g., during concurrent heavy deployments) the OVN plumbing
  is slower, and the race window is wider → 24.04 falls through, 22.04 tolerates it

**Evidence to look for in crashdump:**
- `admin-mysql-<id>.log`: `started machine N` and `status changed to {"running" "ACTIVE"}` for
  the affected machine, but NO subsequent "moving machine N to long poll group" entry (all other
  machines get this entry within ~3–5 min of becoming ACTIVE)
- `logsink.log`: No entries with `machine-N` as the source for the affected model UUID — the
  agent never forwarded any logs to the controller; audit log: zero API connections from that machine

**Evidence to look for in console log (run `openstack console log show <inst_id>`):**
- `Used fallback datasource` (confirms IMDS was unavailable/timing out at cloud-init detection time)
- `ci-info: no authorized SSH keys` (user-data never ran → no SSH keys injected)
- `Cloud-init finished` without any WARNING or ERROR lines at Python log level
  (cloud-init considers NoCloud/fallback a non-error completion, masking the actual failure)
- SSH to the instance's IP from the runner or a peer VM in the same subnet → timeout
  (sshd never started because network config from user-data never ran)

**Example (run 23343435826, UUID 57cc163c):**
```
# Machine 2 provisioned and ACTIVE at 13:07:15Z
# All other machines moved to long poll by 13:12:47Z
# Machine 2 never moved to long poll
# juju wait-for timed out at 14:04:35Z (60m after deploy)

# juju status at timeout:
mysql/2  waiting   allocating  2  192.168.1.197  waiting for machine
Machine 2: pending, ACTIVE (Nova)
```

**Example (run 25109744547, UUID 2edfd3e6):**
```
# Machine 1 (inst 4fce3bdf-...) provisioned and ACTIVE at 13:07:01Z
# Machines 0, 2, 3, 4 all moved to long poll by 13:12:14Z (~3-5 min after ACTIVE)
# Machine 1 last appears in model log at 13:07:01Z — never moved to long poll
# logsink.log: only 2 controller-side entries for machine-1 (host key + password setup
#   at 13:03:21Z); no agent-originated log lines ever appeared
# juju wait-for timed out at 14:03:17Z (60m after deploy)

# juju status at timeout:
mysql/1  waiting   allocating  1  192.168.1.29   waiting for machine
Machine 1: pending, ACTIVE (Nova), inst 4fce3bdf-8173-4cc9-80b5-2c7b4ce72f42
```

**Example (run 26339137164, UUID ac4e7f36, tor3-sqa-sunbeam cluster_3, 2026-05-23):**
```
# Machines 3 (self-signed-certificates, ubuntu@24.04, inst b9873a00-...)
#          4 (data-integrator,          ubuntu@24.04, inst 9e99e84c-...)
# Both provisioned ACTIVE at ~18:13–18:14Z
# Machines 0/1/2 (mysql, ubuntu@22.04) all moved to long poll within ~3 min of ACTIVE
# Machines 3/4: zero log entries in logsink.log; zero API connections in audit log
# Nova console logs (checked by live Marvin agent): "Used fallback datasource" + "ci-info: no authorized SSH keys"
# Both instances: SSH to port 22 timed out from runner AND from peer VM in same subnet
# juju wait-for timed out at 19:11:38Z (60m after deploy start at ~18:11Z)
#
# Confirmed (live investigation): force_config_drive NOT set; OVN metadata proxy architecture
# (neutron-ovn-metadata-agent + per-network HAProxy) confirmed; OVN SB drops observed on hypervisors
# Bug attribution: cloud-init / ubuntu-noble image, NOT juju
```

---

### Pattern 3: juju wait-for 60m timeout — Nova DB connection failure blocks auxiliary VM provisioning

**Symptom (GitHub Actions log):**
```
ERROR timed out waiting for "mysql" to reach goal state
Process completed with exit code 1.
```

**Symptom (juju status at timeout):**
```
Unit                        Workload     Agent       Message
mysql/0                     waiting      idle        Waiting to join the cluster
mysql/1*                    maintenance  executing   Setting up cluster node
mysql/2                     waiting      allocating  waiting for machine
data-integrator/0           waiting      allocating  waiting for machine
self-signed-certificates/0  waiting      allocating  waiting for machine

Machine  State  Address   Inst id   Base          Message
2        pending  ...     <UUID>    ubuntu@22.04  ACTIVE        ← agent never connected
3        down             pending   ubuntu@24.04  cannot run instance: request (...nova...) 500
4        down             pending   ubuntu@24.04  cannot run instance: request (...nova...) 500
```

**Distinguishing from other patterns:**
- Machines 3 and 4 show `down` / `instance-id: pending` — Nova API rejected the request entirely
- The juju status truncates the error; the full message is in `admin-mysql-<id>.log` (crashdump)
- mysql units are stuck for TLS reasons (no `self-signed-certificates`), not the mysqld socket race

**Root cause:** The Nova API returned HTTP 500 with `oslo_db.exception.DBConnectionError` for
all provisioning requests during a ~5-minute window. Juju's provisioner exhausted its 11 retries
during this window and permanently marked both machines `down`. Nova also reported
`availability zone "nova" not valid` midway through retries, indicating the nova-compute service
crashed or restarted. Once machines 3 (`self-signed-certificates`) and 4 (`data-integrator`)
failed to provision, mysql units couldn't get TLS certificates and the cluster couldn't form.

**Evidence to look for:**
- `admin-mysql-<id>.log` (crashdump): `machine N failed to start ... DBConnectionError` (HTTP 500)
  followed by `availability zone "nova" not valid` then terminal `ERROR cannot start instance`
- Multiple distinct Nova instance UUIDs in the errors (each retry creates a new server request)
- `juju status`: machines 3 and 4 `down`/`pending`; mysql units stuck waiting for certs

**Grep patterns:**
```bash
# Find Nova DB errors
grep "DBConnection\|cannot run instance\|availability zone.*not valid" \
  $LOGBASE/models/admin-mysql-*.log | head -20

# Count retries per machine
grep "retrying in 10s" $LOGBASE/models/admin-mysql-*.log | grep -oP "machine \d+" | sort | uniq -c
```

**Example (run 25166260474, UUID aa136dd9, cluster_1, 2026-04-30):**
```
# Nova DB errors began at 13:08:08Z (machine 4) and 13:08:53Z (machine 3)
# Error window: ~13:08–13:13Z (~5 minutes)
# 11 retries per machine, each returning HTTP 500 DBConnectionError
# AZ "nova" became "not valid" at 13:09:27Z (nova-compute restart signal)
# Terminal ERROR for machine 3 at 13:13:46Z, machine 4 at 13:13:51Z
# juju wait-for timed out at 14:04:26Z (60m after deploy)
```

---

### Pattern 4: juju wait-for 60m timeout — ops-framework defer deadlock (mysql/1) + blocking join_innodb_cluster (mysql/2)

**Symptom (GitHub Actions log):**
```
ERROR timed out waiting for "mysql" to reach goal state
Process completed with exit code 1.
```

**Symptom (juju status at timeout):**
```
Unit      Workload     Agent      Message
mysql/0*  active       idle       Primary
mysql/1   waiting      idle       waiting to join the cluster.   ← deferred event loop
mysql/2   maintenance  executing  joining the cluster            ← blocking join call
```

All machines `started` (ACTIVE); all Juju agents connected (all in long-poll group). No hook failures in the mysql model (contrast with Pattern 1 where `hook "certificates-relation-joined" failed: exit status 1`).

**Distinguishing from other patterns:**
- **Pattern 1**: hook fails with `exit status 1` and `ERROR 2002 (HY000): Can't connect to MySQL socket`; unit shows `maintenance / Setting up cluster node`
- **This pattern**: hooks complete successfully (exit 0), charm uses `event.defer()` internally; units show `waiting / idle` and `maintenance / executing`
- **Pattern 2**: unit in `waiting / allocating`, machine `pending` — agent never connected
- **Pattern 3**: machines `down`, Nova HTTP 500

**Root cause:** Two interrelated charm-level race conditions in mysql rev 444 (`8.0/stable`), both triggered by fast VM provisioning:

1. **mysql/1 — circular dependency deadlock**: The `certificates_relation_joined` handler in the charm checks for local InnoDB cluster metadata before configuring TLS. On a secondary node that hasn't joined yet, this check always fails. The charm defers the event. But TLS is needed to join, and joining is needed to have local metadata. The defer loop is perpetual: at 14:40:11Z the event was first deferred; at 15:46:02Z (over an hour later) it was still being deferred every ~1 minute via `flush_mysql_logs` run-commands.

2. **mysql/2 — blocking join_innodb_cluster call**: mysql/2 progressed through its full initial hook queue (~22 hooks in 25 seconds). At 14:40:27Z the `database-peers-relation-changed for mysql/0` hook fired (triggered by mysql/0 achieving Primary status at 14:40:23Z). Inside this handler the charm called `join_innodb_cluster()`. This blocking call never returned, locking the juju uniter for the full 60-minute timeout.

**Logsink evidence to look for:**
```bash
# mysql/1 defer loop (repeating every ~60s for the entire timeout)
grep "unit-mysql-1" logsink.log | grep "Deferring.*certificates_relation_joined\|Failed to check if local cluster metadata"

# mysql/2 blocked hook (last real entry; only update-status timers after)
grep "unit-mysql-2" logsink.log | grep "AGENT-STATUS\|database-peers-relation-changed" | tail -5
# Expect: last executing entry is database-peers-relation-changed for mysql/0; no completion logged

# Confirm all agents connected (no Pattern 2)
grep "moving machine.*to long poll group" admin-mysql-*.log
```

**Key timing pattern:**
- mysql/0 became Primary at `t+8m` after step start
- `certificates-relation-joined` fired on mysql/1 BEFORE the primary was established
- mysql/0 was the founding member; mysql/1/2 were secondaries — only the primary escapes the circular dependency

**Bug report:** https://github.com/canonical/mysql-operators/issues/189

**Example (run 25964063826, UUID 6424e6d7, tor3-sqa-sunbeam cluster_1, 2026-05-16):**
```
# All 5 machines ACTIVE by 14:36:08Z; all in long-poll by 14:37:57Z
# mysql/0 Primary at 14:40:23Z
# mysql/1 certificates_relation_joined first deferred at 14:40:12Z
# mysql/2 last hook (database-peers-relation-changed for mysql/0) at 14:40:27Z — never completed
# juju wait-for timed out at 15:32:21Z (60m after step start at 14:32:04Z)
```

---

_Add more patterns below as they are discovered._

## Notes

- Substrate `tor3-sqa-sunbeam` is an OpenStack environment — no MAAS logs available.
- The step integrates `self-signed-certificates:certificates` → `mysql:certificates` for TLS.
- For `postgresql` 16.x channel, different certificate relations are used (client-certificates
  and peer-certificates instead of certificates).
- The `juju wait-for` timeout is hardcoded at 60 minutes; slow VMs may legitimately need more.

