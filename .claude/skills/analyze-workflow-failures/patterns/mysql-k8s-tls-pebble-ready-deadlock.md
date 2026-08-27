# Pattern: mysql-k8s TLS hook / `mysql-pebble-ready` starvation deadlock

**Applies to step:** `deploy_charm_mysql_k8s`
**Charm:** `mysql-k8s` rev 435, channel `8.4/candidate` (workload MySQL 8.4.8)
**Substrates observed:** `tor3-sqa-shared_maas`, `tor3-sqa-virtual_maas`
**First observed:** 2026-08-20 (6 runs on that day across 5 different clusters)

---

## One-line summary

The mysql-k8s TLS certificates relation-changed handler pebble-execs into the mysql workload
container without checking it is up; when the container is even slightly late the hook errors,
and Juju's hook-error retry loop then permanently starves the queued `mysql-pebble-ready`
event, so mysqld is never started and the unit can never recover.

## Failure chain

```
self-signed-certificates issues cert (seconds after unit start)
        │
        ▼
client-certificates-relation-changed fires
        │
        ▼
_on_client_certificate_available (src/relations/tls.py)
        └─► MySQL.get_member_state()          ← no container can_connect() guard
              └─► pebble exec mysqlsh into "mysql" container
                    └─► ops.pebble.ConnectionError: socket not found at
                        '/charm/containers/mysql/pebble.socket'
                        (or, if pebble is up but mysqld isn't:
                         MySQL Error 2003 (HY000) ... :33062 (111))
              └─► after 3 tenacity retries: raises MySQLUnableToGetMemberStateError
                  (lib/charms/mysql/v0/mysql.py ~2284)
                  ── NOT a non-ONLINE return value, so the handler's
                     `if state != InstanceState.ONLINE: event.defer()` never fires
        │
        ▼
hook exits 1 → unit enters hook-error state
        │
        ▼
uniter: "awaiting error resolution for \"relation-changed\" hook"
        └─► queued mysql-pebble-ready is NEVER dispatched
              └─► src/charm.py mysql_pebble_ready observer never runs
                    └─► pebble layer (mysql / mysqld / mysqld_exporter) never added
                          └─► mysqld never starts; datadir stays empty
        │
        ▼
automatic hook retry every ~5 min → always fails on 33062 → DEADLOCK
        │
        ▼
step's `juju wait-for model ... --timeout 60m` expires → step fails
```

The deadlock is self-sustaining: the hook needs mysqld, and only a hook that cannot run while
the unit is in error can start mysqld.

## Confirming evidence checklist

| Check | Expected when this pattern applies |
|---|---|
| `juju status` unit message | `hook failed: "client-certificates-relation-changed" for self-signed-certificates:certificates` |
| `kubectl exec -c mysql -- pebble services` | `Plan has no services` |
| `/var/lib/mysql/{data,logs,archive,temp}` | empty |
| `ps` in mysql container | no `mysqld` |
| `kubectl logs -c charm` | `mysql-pebble-ready` never appears; first error is a pebble `socket not found` |
| container start times (`kubectl get pod -o json`) | mysql container started *after* the charm container / after the relation-changed hook time |
| foundation layers | fully active (control-plane, workers, ceph, calico, etcd, vault) |

## Triggers (timing wideners — not root cause)

- Slow workload image pull from `registry.jujucharms.com`: observed 60 s, ~84 s, ~2 min, and
  once ~36 min for a 244 MB image.
- `FailedScheduling ... pod has unbound immediate PersistentVolumeClaims` from ceph-csi
  provisioning latency (~2.5 min delay).

Because the trigger is ordinary variance, the failure is **intermittent and cluster-agnostic**:
which units are hit (leader vs. followers, one vs. all three) varies per run, which is why the
resulting `juju status` snapshots differ while the underlying defect is identical.

## Variant outcomes seen

| Units in error | Result |
|---|---|
| All 3 | app status `error`, nothing works |
| Leader only | leader can't bootstrap → other units report `UNKNOWN` / "Cluster is blocked. Skipping." even though their mysqld runs |
| One follower only | app can report `active`/`Healthy` with a Primary, but the `forEach(units, ... active)` wait still times out |

## Fixes

**Charm (durable):**
- Guard `_on_client_certificate_available` with `container.can_connect()` and `event.defer()`.
- Make `get_member_state()` return a non-ONLINE state instead of raising when the instance is
  unreachable, so the existing defer guard works.
- File upstream against `canonical/mysql-k8s-operator`.

**Pipeline:**
- Pin `mysql-k8s` to a revision/channel without the race, or integrate `tls-certificates`
  only after the units reach active. **This is the preferred mitigation** — it avoids the
  race rather than trying to recover from it.
- A recovery step is possible but must not use plain `juju resolve` (see below).

## Recovery: the `juju resolve` trap

⚠️ **No triage of this pattern has ever executed a recovery command** — all live analysis was
read-only. Everything here is reasoned from Juju semantics plus observed state.

| Action | Effect | Verdict |
|---|---|---|
| `juju resolve <unit>` (plain) | Resolves the error and **re-runs the failed hook**, which still needs mysqld on 33062 → fails again → back to error | ❌ No-op, do not recommend |
| `juju resolve --no-retry <unit>` | Resolves **without** re-running the hook, so the uniter advances to a pending `mysql-pebble-ready` → layer pushed → mysqld starts → next certificates hook succeeds | ⚠️ Plausible, unverified |
| `juju remove-unit` + `juju add-unit` / delete the pod | Fresh pod, fresh `pebble-ready` cycle, image now cached so the race window is much smaller | ✅ Reliable, heavier |

### Open question: is `mysql-pebble-ready` pending or discarded?

Live triage instances disagreed on this, and it decides whether `--no-retry` works at all:

- **"Queued behind the error"** → `--no-retry` recovers.
- **"Dropped / never delivered"** → only pod recreation recovers.

The evidence offered for "dropped" is that `mysql-pebble-ready` appears nowhere in the unit
logs or `juju debug-log --replay`. **That is not discriminating** — a pending-but-never-dispatched
event logs identically to a discarded one, since only *executed* hooks are logged. Juju's CAAS
uniter derives `pebble-ready` from container remote state and tracks which containers have
already fired it, which favours "pending, not lost". Still untested.

If a future run of this pattern is caught live, **testing `juju resolve --no-retry` on one unit
is the single highest-value experiment** — it would settle this and unlock a cheap CI recovery step.

**Infrastructure:**
- Local registry mirror / pre-pull of the mysql-k8s OCI image on worker nodes.
- Reduce ceph-csi PVC binding latency.

## Known occurrences

| Solutions Run UUID | GitHub Run ID | Substrate | Cluster |
|---|---|---|---|
| f3dab46a-221b-45c5-9961-0f9ef5bdb6d0 | 32352978984 | virtual_maas | cluster_4 |
| 1868458d-6366-4437-8bb6-21851788a396 | 32352985773 | virtual_maas | cluster_5 |
| 6cd196ef-3196-4533-92f9-cd35979eed46 | 32359415514 | virtual_maas | cluster_1 |
| 9a11b0ba-9bf1-4d9d-9b53-8ef47c8e05ed | 32392541835 | virtual_maas | cluster_1 |
| 152e0d64-ded1-498a-b70b-ba4b72723637 | 32398092764 | shared_maas | dh1_j9_1 |
| 74cc28a5-f373-450d-bc66-637bfd845ab1 | 32409190626 | shared_maas | dh1_j8_2 |
