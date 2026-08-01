# Step Knowledge: test_juju4

## Step Overview

`test_juju4` is a functional smoke test of Juju 4.x against data charms. It runs against an
already-bootstrapped controller (`TARGET_CONTROLLER`, typically `foundations-maas` on
`maas_cloud`) and a fresh model (`MODEL_NAME`, typically `postgresql`). The step script:

1. `juju add-model ${MODEL_NAME}` and sets model constraints
   (`tags=foundation-nodes,sqa-${CLUSTER}` on shared_maas).
2. `juju deploy postgresql --channel 16/edge --force -n 3` + `postgresql-test-app`, relate
   them, then `juju-wait -v -w -t 1800`.
3. Runs `start/show/stop-continuous-writes` actions on `postgresql-test-app/leader`.
4. **Scales out: `juju add-unit postgresql -n 2`** (3 → 5 units), then `juju-wait -v -w -t 1800`.
5. Runs the write actions again, then a deluge of `juju exec --unit postgresql/$j date`
   (50 × 5 units) to stress action/exec handling.

Because it provisions real MAAS machines for every unit, it is sensitive to node
provisioning speed and stability — especially the scale-out in step 4.

## Swift Artifacts

| Path | Description | When to check |
|---|---|---|
| `generated/github-runner/jobs.json` | Step names / conclusions / run_id | First stop for triage |
| `generated/github-runner/run.log` | Full runner log | Full step output |
| `generated/lastlines.txt` | Tail of all log streams | Quick triage |
| `generated/foundation.log` | FCE build log | Controller/model setup errors |

Note: substrate for this step is often `tor3-sqa-shared_maas` → **no MAAS infra logs**
(`generated/maas/logs-*.tgz`) are collected. Diagnosis relies on the GitHub Actions
`juju status` dumps and the `DEBUG:root:` / `ERROR:root:` wait-loop output.

## Key Log Signals (GitHub Actions log)

| Signal | What it means |
|---|---|
| `DEBUG:root:<unit> workload status is <state> since ...` | Per-unit wait-loop polling |
| `ERROR:root:<unit> failed: workload status is error` | Wait aborted — a unit hit `error` |
| `ERROR:root:Not ready in 1800s (max_wait)` / exit 44 | Wait timed out — a unit never became ready (e.g. stuck `allocating`) |
| `DEBUG:root:Machine N is in down` | Machine agent lost / node rebooting |
| `failed to acquire node: No available machine matches constraints` | MAAS pool exhaustion/contention |
| `Deploying: Rebooting` / machine `starting` in `juju status` | Node still commissioning/settling |

## Grep Patterns

```bash
L=files/run_<run_id>_failed.log
# Primary failure
grep -n "ERROR:root:.*failed: workload status is error\|Process completed with exit code" $L
# Per-unit workload transitions (dedup)
grep -h "<unit> workload status" $L | sed 's/\x1b\[[0-9;]*m//g' \
  | awk -F'workload status is ' '{print $2}' | awk '!seen[$0]++'
# Machine instability / MAAS acquisition
grep -h "Machine .* is in down\|No available machine matches constraints\|Deploying: Rebooting" $L | sort -u
# The step script itself (deploy/add-unit commands)
grep -n "juju deploy postgresql\|juju add-unit postgresql" $L
```

## Known Failure Patterns

### Pattern 1: Unit enters `error` during scale-out on unstable MAAS node

**Symptom:**
```
DEBUG:root:postgresql/3 workload status is error since <ts>
ERROR:root:postgresql/3 failed: workload status is error
##[error]Process completed with exit code 1.
```

**Root cause:** During `juju add-unit postgresql -n 2`, a newly requested MAAS node is slow
to allocate (contended `shared_maas` pool — minutes in `allocating`, and/or sibling machines
hitting `failed to acquire node: No available machine`). When the node finally boots it is
still unstable (rebooting/settling post-commission); the unit's install/start hook flips to
`error` briefly and the Juju agent is reported `lost` (`Machine N is in down`). The step's
`juju-wait` is fail-fast on any `error` workload status, so it aborts immediately — often even
though the unit recovers to `waiting`/`active` moments later.

**Evidence to look for:**
- GitHub log: `<unit>` spends far longer in `allocating` than its siblings (e.g. ~8.5 min).
- GitHub log: `error` workload status timestamp coincides with agent `lost` / `Machine N is in down`.
- Post-failure `juju status`: the offending machine shows `starting` / `Deploying: Rebooting`,
  and/or another machine shows `failed to acquire node: No available machine matches constraints`.
- Substrate is `shared_maas` (pool shared across environments → contention).

**Distinguishing from a real charm bug:** if the unit recovers to `waiting`/`active` in the
post-failure `juju status`, and the error coincides with machine reboot/agent-lost, it is an
infrastructure/provisioning-timing issue, not a PostgreSQL charm defect.

---

### Pattern 2: Unit stuck in `allocating` — Juju agent never registers (deploy timeout)

**Symptom:**
```
DEBUG:root:postgresql/0 juju agent status is allocating since <ts>   (repeats unchanged ~30 min)
ERROR:root:Not ready in 1800s (max_wait)
##[error]Process completed with exit code 44.
```

**Root cause:** On cloud substrates (e.g. `sunbeam`/OpenStack), one deployed instance boots at
the IaaS layer (nova `ACTIVE`, even gets an IP) but its in-guest Juju machine agent never
phones home to the controller. The Juju machine stays `pending` with **no address** and the
unit sits in `allocating` / "waiting for machine" until the wait's 1800s budget is exhausted →
exit code 44. Common on volume-booted instances (`root-disk-source=volume`, set for sunbeam)
where cloud-init/agent bring-up or the Cinder-backed boot is slow or fails.

**Evidence to look for:**
- GitHub log: the same `<unit> juju agent status is allocating since <ts>` line repeats
  unchanged for the whole wait window; ends with `Not ready in 1800s (max_wait)` / exit 44.
- Post-failure `juju status`: the offending machine is `pending` with an **empty Address**,
  while sibling machines are `started` with addresses. Instance `Message` may still say `ACTIVE`.
- `openstack server list` (collected during log_collection): the instance is `ACTIVE` **with**
  an IP — proving the VM booted and the gap is the Juju agent failing to register, not instance
  creation.
- Only the initial `-n 3` units appear (no scale-out units) → failure is in the first deploy wait.

**Distinguishing from a charm bug:** the instance is up at the IaaS layer but Juju never sees
the agent; other instances on the same cloud come up fine → instance-specific agent/provisioning
failure, not a PostgreSQL charm or pipeline defect.

---

### Pattern 3: `postgresql-test-app` fails `database-relation-changed` early, before the DB cluster forms (Juju 4.0/candidate)

**Symptom:**
```
ERROR:root:postgresql-test-app/0 failed: workload status is error
##[error]Process completed with exit code 1.
```
`juju status` at failure:
```
postgresql           active    3   (or 1/3)   postgresql           16/edge        1195
postgresql-test-app  error     1              postgresql-test-app  latest/stable  513   hook failed: "database-relation-changed"
postgresql-test-app/0*  error  ...  hook failed: "database-relation-changed"
postgresql/0* active ; postgresql/1,2 waiting  "awaiting for cluster to start" (or allocating)
```

**Root cause:** In the initial deploy wait, the **`postgresql-test-app`** client unit (not a
postgresql unit) fails its `database-relation-changed` hook while the 3-unit postgresql cluster
is **still forming** — typically only the leader postgresql unit is `active` and the others are
`waiting` ("awaiting for cluster to start") or `allocating`. The relation data the test-app
consumes is partial/in-flux at that moment. The step's fail-fast `juju-wait` aborts on the
first `error` workload status, so the whole step exits 1 even though Juju would normally retry
the hook.

**Distinguishing from Patterns 1 & 2:** the failing app is `postgresql-test-app`, not
`postgresql`; the machines are usually all `started`/ACTIVE (not an allocating/pool problem),
and the postgresql cluster is mid-formation. This is a relation-hook timing issue, not node
provisioning.

**Juju 4.0/candidate vs charm bug — how to tell:** these runs use `ADDON_ID: juju_4.0_candidate`
(controller `Version 4.0.13`). The exact cause (a Juju 4.0/candidate relation-data behavior
change vs a test-app charm that mishandles early/partial relation data) **cannot be determined
from the Swift bundle** because no `juju-crashdump`/unit debug-log is collected on the
openstack/sunbeam substrates. To attribute definitively you MUST capture
`juju debug-log --replay --include postgresql-test-app/0` (or a crashdump) from the live run —
the `juju status` dump alone only shows the hook name, not the traceback.

**Observed:** runs 38c79430, a8caeed6, d59f2e4e (2026-07-22) — all identical: test-app rev 513,
Juju 4.0.13, postgresql cluster still forming.

**LIVE-CAPTURED root cause (run 30120756631, 2026-07-24, Juju 4.0.13):** the hook crashes on a
**Juju secrets access denial**. The consumer's `data_platform_libs`
`_on_relation_changed_event` → `_register_secrets_to_relation(diff.added)` reads a just-added
relation secret URI (`database.<relid>.user.secret`) and Juju returns:
```
ops.model.ModelError: ERROR "postgresql-test-app/0" is not allowed to read this secret
  secret-get ... secret://<model-uuid>/<secret-id> --label database.4.user.secret  (exit 1)
```
At failure the provider postgresql cluster is **degraded** (`postgresql/2* Primary (degraded)`,
others still installing; Patroni "Unable to get primary. Cluster status unreachable"), and the
denial starts the instant the secret URI appears in the databag (`diff.added`) and is read
within seconds → a grant-propagation race. `data_platform_libs` does not tolerate the transient
denial (raises instead of deferring), so the hook errors and the fail-fast wait aborts the step.

**Juju-4 vs charm — how to close it:** the "not allowed to read this secret" verdict is Juju's,
and only reproduces under `juju_4.0_candidate`. But a healthy grant was not proven — the
provider cluster was unhealthy, so it may simply not have granted yet. To decide definitively,
capture live `juju secrets --format yaml` + per-secret grants for the `database` relation at the
moment of the consumer error (ideally with a healthy postgresql cluster): grant-present-but-
denied ⇒ Juju 4.0.13 regression; no-grant-yet ⇒ provider-readiness race + consumer-lib
intolerance. Full write-up: `outputs/30120756631-analysis.md`.

**Capturing live (what worked):** SSH to the runner; the foundation-openstack controller/model
`postgresql` is reachable with the juju client the step installs (4.0.x by test time — auto-pick
from `/snap/juju/*/bin/juju`). When `juju status -m foundation-openstack:postgresql` shows
`postgresql-test-app ... hook failed`, grab
`juju debug-log -m foundation-openstack:postgresql --replay --include postgresql-test-app/0
--no-tail -l DEBUG` (contains the traceback) plus `juju secrets`/grants and a crashdump — before
the pipeline's cleanup destroys the model.

---

_Add more patterns below as they are discovered._

## Notes

- No MAAS infra logs on `shared_maas`; trust the `juju status` dumps in the GitHub log.
- The wait loop that emits `DEBUG:root:`/`ERROR:root:` fails on the *first* unit error with no
  error-retry tolerance — a single transient hook error fails the whole step.
- Scale-out (step 4, `add-unit -n 2`) is the most fragile part: it provisions brand-new nodes
  mid-test, so it is where pool contention/instability surfaces.
