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
grep "timed out waiting\|##\[error\]" /tmp/run_<run_id>_failed.log | grep -i mysql

# Check unit states at failure time
cat /tmp/<uuid>/generated/deploy_charm_mysql/juju_status_foundation-openstack_mysql.txt

# Parse unit workload statuses from JSON
python3 -c "
import json
with open('/tmp/<uuid>/generated/deploy_charm_mysql/juju_status_foundation-openstack_mysql.json') as f:
    data = json.load(f)
for app_name, app in data.get('applications', {}).items():
    for unit_name, unit in app.get('units', {}).items():
        ws = unit.get('workload-status', {})
        print(f'{unit_name}: {ws.get(\"current\")} - {ws.get(\"message\", \"\")}')
"
```

## Known Failure Patterns

### Pattern: juju wait-for 60m timeout — mysql/N stuck due to mysqld socket unavailable on certificates-relation-joined

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
ls /tmp/crashdump/*/0/baremetal/var/log/juju/models/
# Look for admin-mysql-XXXXXX.log — the XXXXXX is a short prefix of the model UUID
grep "423a8d" /tmp/crashdump/.../logsink.log  # replace with actual prefix
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

### Pattern: juju wait-for 60m timeout — mysql/2 stuck "waiting for machine" (Juju agent never connected)

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
state is `pending`. Unit mysql/2 and its filesystem storage remain pending the entire 60 minutes.

**Distinguishing from the mysqld socket race pattern:**
- **This pattern**: unit in `waiting / allocating`, machine in Juju `pending` — agent never connected
- **Socket race pattern**: unit in `maintenance / Setting up cluster node` — agent *did* connect but charm hook failed

**Root cause:** The Nova VM for `mysql/2` was provisioned successfully, but the Juju agent
inside the VM never connected to the controller. The cloud-init bootstrap (which installs and
starts the juju agent) failed silently on that particular VM. All other machines connect within
~5 minutes of becoming Nova `ACTIVE`; a machine that never enters the instancepoller "long poll
group" is a reliable indicator that its agent never came up.

**Evidence to look for:**
- `admin-mysql-<id>.log`: `started machine N` and `status changed to {"running" "ACTIVE"}` for
  the affected machine, but NO subsequent "moving machine N to long poll group" entry (all other
  machines get this entry within ~5 minutes of becoming ACTIVE)
- `logsink.log`: No entries with `machine-N` as the source for the mysql model UUID — the agent
  never forwarded any logs to the controller
- `juju status`: affected machine state is `pending` despite having a Nova inst_id and IP;
  corresponding unit shows `waiting / allocating`; storage shows `pending`

**Why further diagnosis is impossible from available logs:**
The VM's cloud-init / agent startup logs live inside the VM itself; no nova console-log or
in-VM logs are collected by the pipeline for this substrate (`tor3-sqa-sunbeam`).

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

---

_Add more patterns below as they are discovered._

## Notes

- Substrate `tor3-sqa-sunbeam` is an OpenStack environment — no MAAS logs available.
- The step integrates `self-signed-certificates:certificates` → `mysql:certificates` for TLS.
- For `postgresql` 16.x channel, different certificate relations are used (client-certificates
  and peer-certificates instead of certificates).
- The `juju wait-for` timeout is hardcoded at 60 minutes; slow VMs may legitimately need more.

## Version History

- **v1.0** (2026-03-26): Initial version — 60m timeout on cluster node join (run 23343431862)
- **v1.1** (2026-03-26): Added Juju agent never connected pattern — Nova VM ACTIVE but agent never registers, machine stays Juju `pending` (run 23343435826, UUID 57cc163c)
- **v1.2** (2026-03-30): Linked bug report canonical/mysql-operators#189 for mysqld socket race pattern
