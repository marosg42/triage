# Step Knowledge: spark_terraform

## Step Overview

Deploys the **Charmed Spark 3.5** bundle onto a Kubernetes Juju controller using the
project's Terraform module (`terraform/products/charmed-spark-3.5` from
`canonical/spark-k8s-bundle.git`). After `terraform apply`, FCE polls the target Juju model
(typically `spark`) with a "wait for goal state" loop until every application is
`active/idle`, then hands off to `test_spark`.

Runs on K8s substrates — commonly `ext-sqa-aks` (Azure AKS) — so there are **no MAAS logs**.
The relevant evidence is the GitHub Actions log (the goal-state wait loop + final `juju
status` dump) and the Swift `generated/` layer logs.

## Swift Artifacts

| Path | Description | When to check |
|---|---|---|
| `generated/version_collector_spark_terraform.log` | Post-run version/status collector for this layer | Context on charms/revisions; note its `git describe`/`kubernetes-worker not found` errors are benign |
| `generated/lastlines.txt` | Tail of all log streams | Quick triage |
| `generated/foundation.log` | FCE driver log | Always |
| `generated/github-runner/run.log` | Full runner log | Deep investigation |

There is no `logs-*.tgz` infra archive for K8s/public-cloud substrates.

## Key Log Evidence (GitHub Actions log)

The decisive evidence is at the end of the step: the `ERROR timed out waiting for "spark" to
reach goal state` line immediately followed by a full `juju status` of the model. Read the
per-app / per-unit **Message** column — that names the app(s) blocking goal state.

## Grep Patterns

```bash
# Confirm the goal-state timeout
grep -n 'timed out waiting for .* to reach goal state' files/run_<run_id>_failed.log

# Jump to the juju status dump printed at timeout (blocking apps/units)
grep -n 'Missing\|blocked\|waiting\|awaiting\|error\|Message' files/run_<run_id>_failed.log

# Charm channels/revisions deployed
grep -n 'postgresql-k8s\|kyuubi-k8s\|spark-integration-hub\|zookeeper-k8s' files/run_<run_id>_failed.log
```

## Known Failure Patterns

### Pattern 1: Goal-state timeout — postgresql-k8s stuck "awaiting for primary endpoint to be ready"

**Symptom:**
```
ERROR timed out waiting for "spark" to reach goal state
...
kyuubi                blocked  Missing authentication database relation
kyuubi-users/0*       waiting  awaiting for primary endpoint to be ready
metastore/0*          waiting  awaiting for primary endpoint to be ready
```

**Root cause:** The `metastore` and `kyuubi-users` `postgresql-k8s` units never expose a
ready primary endpoint (Patroni/primary-election never settles), even though the
application-level status may read `active`. Because no primary endpoint is available, the
`kyuubi` charm's metastore/auth-database relations never complete, so all `kyuubi` units stay
`blocked` on "Missing authentication database relation". The model never reaches goal state
and the wait loop times out (~50+ min).

**Evidence to look for:**
- GitHub log `juju status` at timeout: postgresql units `waiting — awaiting for primary
  endpoint to be ready`; `kyuubi` units `blocked — Missing authentication database relation`.
- `data-integrator blocked — Please relate the data-integrator with the desired product` is
  the charm's normal idle-without-relation state, **not** the blocker — do not misattribute.
- Check the deployed `postgresql-k8s` channel/revision (e.g. `14/stable` rev 925) for a
  possible charm regression.

---

_Add more patterns below as they are discovered._

## Notes

- Substrate is K8s/public cloud (e.g. AKS eastus) — skip all MAAS log steps.
- The wait loop only prints `model "spark" found, waiting...` with no per-app progress, so
  the only actionable signal is the final `juju status` dump at timeout.
- The failure is often reported twice in `--log-failed`: once from the `Run the pipeline`
  job and again echoed by the log-collection job — same event, not two failures.
