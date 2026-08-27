# Step Knowledge: deploy_charm_mysql_k8s

## Step Overview

Deploys the `mysql-k8s` charm (3 units) plus `self-signed-certificates` and `data-integrator`
into the `mysql-k8s` model on the CAAS controller `foundations-kubernetes` (a Juju controller
hosted on the charmed-Kubernetes cluster built by the `kubernetes-maas` layer). It integrates
them and then waits up to 60 minutes for all units to be active:

```bash
juju wait-for model ${MODEL_NAME} \
  --query='forEach(units, unit => unit.workload-status=="active")' --timeout 60m --summary
```

A failure is therefore usually a **timeout**, not a command error — the step duration will be
almost exactly 62 minutes and the runner log ends with:

```
ERROR timed out waiting for "mysql-k8s" to reach goal state
##[error]Process completed with exit code 1.
```

Seen on both `tor3-sqa-shared_maas` and `tor3-sqa-virtual_maas` substrates.

## Swift Artifacts

| Path | Description | When to check |
|---|---|---|
| `generated/deploy_charm_mysql_k8s/juju_status_foundations-kubernetes_mysql-k8s.txt` | Human-readable juju status of the mysql-k8s model at failure time | Always — shows unit states |
| `generated/deploy_charm_mysql_k8s/juju_status_foundations-kubernetes_mysql-k8s.json` | Same, JSON | Programmatic parsing of app/unit states, charm rev/channel |
| `generated/deploy_charm_mysql_k8s/juju_status_foundations-maas_kubernetes-maas.txt` | Status of the underlying CDK cluster | Rule infra in/out (control-plane, workers, ceph, calico, vault) |
| `generated/deploy_charm_mysql_k8s/juju_status_foundations-kubernetes_controller.txt` | CAAS controller model | Controller health |
| `generated/happy-bot/report.md` | Live-environment triage performed while the deployment was still up | **Read first** — contains kubectl/pebble/debug-log evidence that is not in any other artifact |
| `generated/github-runner/run.log` | Full runner log | Confirm the wait-for timeout and env (SUBSTRATE, CLUSTER) |

> `generated/happy-bot/` also contains `findings.md`, `improvements.md` and `bug_report.txt`.

## Key Log Files

No MAAS-node-level logs are needed for this step; the failure is at the Kubernetes/charm layer.
The decisive evidence normally comes from happy-bot's live capture:
`kubectl get pods -o wide`, container start times, `kubectl exec -c mysql -- pebble services`,
`kubectl logs -c charm`, and `juju debug-log --replay -m foundations-kubernetes:mysql-k8s`.

## Grep Patterns

```bash
# Confirm the wait-for timeout
grep -n "timed out waiting for\|##\[error\]" files/<uuid>/generated/github-runner/run.log

# Substrate / cluster
grep -m1 "SUBSTRATE: \|CLUSTER: " files/<uuid>/generated/github-runner/run.log

# App/unit states + charm revision at failure time
python3 -c "
import json
d=json.load(open('files/<uuid>/generated/deploy_charm_mysql_k8s/juju_status_foundations-kubernetes_mysql-k8s.json'))
for n,a in d['applications'].items():
    print(n, a.get('charm-rev'), a.get('charm-channel'), a['application-status']['current'], a['application-status'].get('message'))
    for un,u in (a.get('units') or {}).items():
        print('  ', un, u['workload-status']['current'], u['workload-status'].get('message'))
"
```

## Known Failure Patterns

### Pattern 1: TLS relation hook races workload container → hook-error starves `mysql-pebble-ready` (deadlock)

**Signature**

- `mysql-k8s` app/units in `error` with
  `hook failed: "client-certificates-relation-changed" for self-signed-certificates:certificates`
  (sometimes `peer-certificates-relation-changed`).
- Non-errored units may sit in `maintenance` / `UNKNOWN` with "Cluster is blocked. Skipping."
- Charm container log: `ops.pebble.ConnectionError: Could not connect to Pebble: socket not
  found at '/charm/containers/mysql/pebble.socket' (container restarted?)`, then repeated
  `MySQL Error 2003 (HY000): Can't connect to MySQL server on '...:33062' (111)`.
- `kubectl exec -c mysql -- pebble services` → `Plan has no services`; `/var/lib/mysql/data` empty;
  no mysqld process; `mysql-pebble-ready` never appears in the charm log.
- Uniter logs `awaiting error resolution for "relation-changed" hook`.

**Mechanism**

1. `self-signed-certificates` issues the cert within seconds, so the certificates
   relation-changed hook fires very early.
2. `_on_client_certificate_available` (`src/relations/tls.py`, ~line 230) calls
   `MySQL.get_member_state()` unconditionally — a pebble exec of `mysqlsh` into the **mysql
   workload container**. There is no `can_connect()` guard, and `get_member_state()`
   (`lib/charms/mysql/v0/mysql.py`, ~line 2284) *raises* `MySQLUnableToGetMemberStateError`
   after 3 tenacity retries instead of returning a non-ONLINE state — so the handler's
   `if state != InstanceState.ONLINE: event.defer()` guard is bypassed.
3. If the mysql container is not up yet, the hook errors and the unit enters hook-error state.
4. Juju then only retries the failed hook and never dispatches the queued `mysql-pebble-ready`
   — the only observer that pushes the mysqld pebble layer (`src/charm.py`). mysqld therefore
   never starts, so every retry fails again on port 33062. **Permanent deadlock**; the 60 min
   wait-for expires.

**What widens the race window (the trigger, not the root cause)**

- Slow OCI image pull for the mysql workload container from `registry.jujucharms.com`
  (observed 60 s, ~84 s, ~2 min, and in one case ~36 min for a 244 MB image).
- `FailedScheduling ... pod has unbound immediate PersistentVolumeClaims` — ceph-csi
  provisioning latency delaying pod start by ~2.5 min.

Both are ordinary infra latency; a correctly-deferring charm would tolerate them.

**Affected versions observed:** `mysql-k8s` rev 435, channel `8.4/candidate`, workload 8.4.8,
with `self-signed-certificates` rev 586 (`1/stable`), Juju 3.6.27 CAAS, CDK 1.35.7.

**Do not misdiagnose as:** ceph/PVC failure, image registry outage, or CDK cluster problems.
Those only shift the timing. Verify the foundation layers in
`juju_status_foundations-maas_kubernetes-maas.txt` — they are typically fully active.

**Recovery (for reference; infra is gone by analysis time — and note this has never been
verified live, all triage of this pattern so far was read-only):**

- ❌ **Plain `juju resolve mysql-k8s/<n>` does NOT work.** It re-runs the failed hook, which
  still requires mysqld on 33062, which is still not running → straight back to error.
  Do not recommend it.
- ⚠️ `juju resolve --no-retry mysql-k8s/<n>` is the only plausible in-place fix: it resolves
  the error *without* re-running the hook, so the uniter can advance to a pending
  `mysql-pebble-ready`. Unverified — depends on that event being pending rather than discarded.
- ✅ Recreating the unit/pod (`juju remove-unit` + `juju add-unit`, or deleting the pod) is the
  reliable recovery; the image is cached by then, so the race window is much smaller.

See `patterns/mysql-k8s-tls-pebble-ready-deadlock.md` for the full deep-dive.
