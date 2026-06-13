# Step Knowledge: deploy_charm_postgresql

## Step Overview

Deploys the PostgreSQL charm (and related charms) into a Juju model on an OpenStack controller.
It deploys `postgresql` (3 units), `self-signed-certificates`, and `data-integrator`, integrates
them, then waits up to 60 minutes for all units to reach `active` workload status using
`juju wait-for`.

## Swift Artifacts

Objects stored under `<uuid>/generated/deploy_charm_postgresql/` that are useful for diagnosing
failures:

| Path | Description | When to check |
|---|---|---|
| `generated/deploy_charm_postgresql/juju_status_foundation-openstack_postgresql.txt` | Human-readable juju status of postgresql model at failure time | Always — shows unit states |
| `generated/deploy_charm_postgresql/juju_status_foundation-openstack_postgresql.json` | JSON juju status of postgresql model at failure time | Programmatic parsing of unit/app states |
| `generated/deploy_charm_postgresql/juju_status_foundation-openstack_controller.txt` | Juju status of controller model | Check OpenStack infra health |
| `generated/juju_openstack_controller/juju-crashdump-controller-<timestamp>.tar.gz` | Full Juju controller crashdump | Deep investigation — contains logsink.log or model logs |

### Key files inside the crashdump

| File | What it contains | When to use |
|---|---|---|
| `var/log/juju/models/admin-postgresql-<id>.log` | Controller-side model worker log (provisioner, firewaller) | VM provisioning issues, MessagingTimeout |
| `var/log/juju/machine-0.log` | Controller machine worker log | Controller-side issues |

## Key Log Files

No MAAS logs for this step — substrate is `tor3-sqa-sunbeam` (OpenStack VMs, no MAAS
infrastructure logging). Investigation relies on GitHub Actions logs and Swift juju status
snapshots.

## Grep Patterns

```bash
# Find the timeout error in GitHub Actions log
grep "timed out waiting\|##\[error\]" <work_dir>/run_<run_id>_failed.log | grep -i postgresql

# Check unit states at failure time
cat <work_dir>/<uuid>/generated/deploy_charm_postgresql/juju_status_foundation-openstack_postgresql.txt

# Parse unit workload statuses from JSON
python3 -c "
import json
with open('<work_dir>/<uuid>/generated/deploy_charm_postgresql/juju_status_foundation-openstack_postgresql.json') as f:
    data = json.load(f)
for app_name, app in data.get('applications', {}).items():
    for unit_name, unit in app.get('units', {}).items():
        ws = unit.get('workload-status', {})
        print(f'{unit_name}: {ws.get(\"current\")} - {ws.get(\"message\", \"\")}')
"
```

## Known Failure Patterns

### Pattern 1: juju wait-for 60m timeout — Juju agent never connected (Nova ACTIVE, machine Juju `pending`)

**Symptom (GitHub Actions log):**
```
ERROR timed out waiting for "postgresql" to reach goal state
Process completed with exit code 1.
```

**Symptom (juju status at timeout):**
```
Unit                        Workload     Agent       Message
data-integrator/0           waiting      allocating  waiting for machine
postgresql/0                waiting      allocating  waiting for machine
self-signed-certificates/0  waiting      allocating  waiting for machine

Machine  State    Address        Inst id (Nova)    Message
0        pending  192.168.1.40   355fe3b5-...      ACTIVE
```

The Nova instance exists (has inst_id, address, Nova status `ACTIVE`) but the Juju machine
state is `pending`. Unit and its storage remain pending the entire 60 minutes.

**Root cause: OVN metadata proxy race condition**

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
it far more susceptible to this race.

**Evidence to look for in crashdump / model logs:**
- `admin-postgresql-<id>.log`: `started machine N` and `status changed to {"running" "ACTIVE"}` for
  the affected machine, but NO subsequent "moving machine N to long poll group" entry (all other
  machines get this entry within ~3–5 min of becoming ACTIVE)
- `version_collector_deploy_charm_postgresql.log`: `ERROR attempt count exceeded: retrieving SSH host keys for "postgresql/N": keys not found`

---

_Add more patterns below as they are discovered._

## Notes

- Substrate `tor3-sqa-sunbeam` is an OpenStack environment — no MAAS logs available.
- The step integrates `self-signed-certificates:certificates` → `postgresql:client-certificates` and `postgresql:peer-certificates` for TLS.
- The `juju wait-for` timeout is hardcoded at 60 minutes.
