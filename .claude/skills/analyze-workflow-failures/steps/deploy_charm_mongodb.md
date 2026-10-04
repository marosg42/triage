# Step Knowledge: deploy_charm_mongodb

## Step Overview

Deploys the `mongodb` machine charm (3 units, `6/stable`) into the `mongodb` model on the
`foundations-maas` controller, together with `self-signed-certificates` (`1/stable`) and
`data-integrator` (`latest/edge`), relates them, then blocks on:

```
juju wait-for model ${MODEL_NAME} \
  --query='forEach(units, unit => unit.workload-status=="active")' \
  --timeout 60m --summary
```

Units land on **bare-metal MAAS machines**, so most failures here are provisioning failures,
not charm failures. Note this step uses `juju wait-for` (not `juju-wait`), which has **no
machine-error detection** — it will always burn the full 60 minutes.

## Swift Artifacts

| Path | Description | When to check |
|---|---|---|
| `generated/deploy_charm_mongodb/juju_status_foundations-maas_mongodb.json` | Full model status incl. per-machine `machine-status` | **Always — first stop** |
| `generated/deploy_charm_mongodb/juju_status_foundations-maas_mongodb.txt` | Human-readable same | Quick scan |
| `generated/deploy_charm_mongodb/juju_status_foundations-maas_controller.{json,txt}` | Controller model health | Rule out controller issues |
| `generated/version_collector_deploy_charm_mongodb.log` | `juju ssh` probes per unit | Confirms which units were reachable |
| `generated/maas/add_partitions.log` | Pre-deployment disk prep against the MAAS nodes | Node-specific storage oddities |
| `generated/maas/logs-*.tgz` | Full MAAS logs — **only on `virtual_maas`/`dedicated_maas`** | Absent on `shared_maas` |

## Grep Patterns

```bash
# The fatal line in the runner log
grep -an 'timed out waiting for "mongodb" to reach goal state' files/run_<run_id>_failed.log

# Any machine that failed to provision, with timestamps
python3 -c "
import json;d=json.load(open('files/<UUID>/generated/deploy_charm_mongodb/juju_status_foundations-maas_mongodb.json'))
for k,m in d['machines'].items():
    print(k, m.get('display-name'), m['machine-status'], m['juju-status'])
"

# Acquisition retries vs. hard deployment failures
grep -an "failed to acquire node\|Failed deployment\|provisioning error" files/run_<run_id>_failed.log
```

> The step emits thousands of `model "mongodb" found, waiting...` lines. Jump straight to the
> last ~200 lines of the step instead of reading them.

## Known Failure Patterns

### Pattern 1: MAAS node fails to deploy → `wait-for` burns the full 60m timeout

**Symptom (GitHub Actions log):**
```
ERROR timed out waiting for "mongodb" to reach goal state
##[error]Process completed with exit code 1.
```

**Symptom (`juju status`):**
```
mongodb                   6.0.24   active    2/3  mongodb  6/stable  265
mongodb/2   waiting  allocating  2  10.241.37.107   waiting for machine
2  down  10.241.37.107  solqa-shared-maas-server-01  ubuntu@22.04  zone1
      Failed deployment: Loading ephemeral
```

**Root cause:** One of the three allocated MAAS machines failed bare-metal deployment. The
`Loading ephemeral` stage is where the node PXE-boots and pulls the curtin ephemeral image; an
abort there is an infrastructure fault on that specific node (BMC/power, PXE/DHCP/TFTP, or
image download), unrelated to the mongodb charm. Juju retries *acquisition* failures
(`failed to acquire node: No available machine matches constraints` → retries in a new AZ) but
**never retries a machine MAAS reports as `Failed deployment`** — it stays in
`provisioning error` forever. `juju wait-for` has no machine-error awareness, so the step idles
until its 60m timeout even though the outcome was decided minutes in.

**Key timing check — distinguishes node fault from deploy timeout:**
compare `machine-status.since` against `modification-status.since`.
A gap of only a few minutes (e.g. 15:06:11Z → 15:12:43Z) means the node aborted early and is
faulty; a gap near MAAS's deploy timeout means a slow/hung install instead.

**Evidence to look for:**
- `juju_status_..._mongodb.json`: machine with `machine-status.current == "provisioning error"`
  and `juju-status.current == "down"` / `agent is not communicating with the server`.
- Surviving units are genuinely healthy (`mongodb/N*` shows `Primary.`), app status `active 2/3`
  — proves the charm was fine.
- The failed unit's storage stays `pending` while the others are `attached`.
- `grep` the other layers' `juju_status_*.txt` for the node name: if it appears nowhere else,
  the node was idle until this step and had no prior corroborating errors in this run.

**Substrate note:** on `tor3-sqa-shared_maas` there is **no `generated/maas/logs-*.tgz`**, so
the PXE/BMC evidence is unavailable post-mortem — the node-level cause can only be confirmed
live via the MAAS event log (`GET /MAAS/api/2.0/events/?id=<system_id>`).

**Observed in:**
- Run 36412188317 (UUID: a1c34442-10e6-4b0d-8579-c3e288499c3b, cluster `dh1_j8_1`,
  `tor3-sqa-shared_maas`; `solqa-shared-maas-server-01` / `psc3mt` failed at 15:12:43Z,
  step timed out at 16:06:23Z)

---

_Add more patterns below as they are discovered._

## Notes

- 5 machines total: 3 × mongodb (ubuntu@22.04) + 1 × self-signed-certificates + 1 × data-integrator
  (both ubuntu@24.04).
- The 60m `juju wait-for` timeout is the *only* failure mechanism in this step — the error text
  is identical whether the cause is a dead machine, a hook error, or a genuinely slow deploy.
  Always go to the per-machine and per-unit status to tell them apart.
- On `shared_maas` the machine pool is tight; transient
  `failed to acquire node: No available machine matches constraints` messages at deploy start
  are normal and self-heal via AZ retry — do not mistake them for the root cause.
