# sunbeam_resize

## What this step does
Runs `sunbeam cluster resize` on the bootstrap node to scale the Sunbeam/OpenStack control plane to the requested HA shape, then waits for the Juju OpenStack model to converge.

## Common failure modes

### OpenStack model wait timeout with MySQL primary/router instability

**Signature**
- Runner fails in `cmd_resize -> resize_cluster -> run_remote_command(..., "sunbeam cluster resize")` with `subprocess.CalledProcessError`.
- Sunbeam output contains `Timed out waiting to deploy OpenStack control plane: TimeoutError("wait timed out after 3599...s")`.
- Timeout/final Juju status shows `nova` or another DB consumer stuck in `waiting` with `(workload) Container service not ready`, and one or more `mysql` units may show `waiting to get cluster primary from peers`.
- `*-mysql-router` pod logs contain repeated `Shell.connect_to_primary: No primaries found in an InnoDB cluster`.
- Affected workload pod logs may show DB connection failures such as `pymysql.err.OperationalError` / `ECONNREFUSED` to a `*-mysql-router-service...:6446` service.

**Likely cause**
During HA scale-out, mysql-k8s/mysql-router-k8s can spend most of the fixed one-hour Sunbeam wait without a stable reachable InnoDB primary. Joiners may loop on `Failed to fetch cluster status` and later recover via clone fallback, but dependent API charms can remain waiting long enough for `sunbeam cluster resize` to time out.

**Evidence to collect**
- `generated/sunbeam/output.log` timeout status snapshot, especially `mysql`, `*-mysql-router`, and the waiting application/unit.
- `generated/sunbeam/juju_status_openstack.txt` final MySQL/router/consumer status.
- MySQL pod logs from `generated/sunbeam/pods_openstack_logs.tgz` (`logs-openstack-mysql-*.txt`) for `Attempting to join cluster`, `Failed to fetch cluster status`, `Fallback to clone method`, and `member-role PRIMARY/SECONDARY`.
- Affected mysql-router logs for `No primaries found in an InnoDB cluster`, hook failures, and the first successful `mysql-router-pebble-ready` after timeout.
- Affected workload pod logs for DB connection errors to router services.

**Recommended follow-up**
- File/follow mysql-k8s/mysql-router-k8s issues so no-primary scale-out windows are retried cleanly and dependent charms are re-triggered once routers recover.
- Improve Sunbeam timeout diagnostics to report no-primary/router failures and consider extending the wait when MySQL recovery is actively progressing.

### OpenStack model wait timeout with mysql-k8s restart hook KeyError

**Signature**
- Runner fails in `cmd_resize -> resize_cluster -> run_remote_command(..., "sunbeam cluster resize")` with `subprocess.CalledProcessError`.
- Sunbeam output contains `Timed out waiting to deploy OpenStack control plane: TimeoutError("wait timed out after 3599...s")`.
- Timeout/final Juju status shows `mysql/0` in `error` with `hook failed: "restart-relation-changed"` while most or all other OpenStack units are active.
- `kubectl get pod -n openstack` may show `mysql-0`, `mysql-1`, and `mysql-2` all `Running`; the failure is in the charm hook, not necessarily the MySQL data plane.
- The mysql pod charm logs contain repeated tracebacks ending with:
  - `File "/var/lib/juju/agents/unit-mysql-0/charm/src/charm.py", line 546, in _restart`
  - `new_primary = self.get_unit_address(self.peers.units.pop())`
  - `KeyError: 'pop from an empty set'`

**Likely cause**
During resize scale-out, the mysql-k8s charm's rolling restart handler can be re-emitted after its peer set is empty. The handler assumes `self.peers.units` is non-empty and crashes when `pop()` is called, leaving the original mysql unit in hook error. Sunbeam's all-healthy OpenStack wait then times out even though the MySQL pods may be running.

**Evidence to collect**
- `generated/sunbeam/output.log` or `output.condensed.log` for the 3600s timeout and embedded status dump.
- `generated/sunbeam/juju_status_openstack.txt` for `mysql/0` workload error.
- `generated/sunbeam/kubectl_get_pod.txt` to distinguish pod health from charm hook state.
- Extract `generated/sunbeam/pods_openstack_logs.tgz` and inspect `logs-openstack-mysql-0.txt` for the `_restart` / `self.peers.units.pop()` traceback.
- If available from live triage, `juju show-unit mysql/0` relation data can confirm whether group replication is healthy despite the charm error.

**Recommended follow-up**
- File/follow a mysql-k8s charm bug to guard empty peer sets and make the restart relation handler idempotent during scale-out.
- For a live deployment, resolve/retry `mysql/0` only after re-arming restart relation state or otherwise ensuring the hook has valid peer information, then rerun resize.

### OpenStack model wait timeout with placement waiting

**Signature**
- Runner fails in `cmd_resize -> resize_cluster -> run_remote_command(..., "sunbeam cluster resize")` with `subprocess.CalledProcessError`.
- Sunbeam output contains `Timed out waiting to deploy OpenStack control plane: TimeoutError("wait timed out after 3599...s")`.
- Timeout/final Juju status shows `placement` or specific `placement/N` units in `waiting` with `(workload) Placement API not yet serving` while most or all other applications are active.
- Kubernetes pods for the affected placement units may still be `Running`/ready, and placement API logs may show HTTP 200s after the waiting status was set.

**Likely cause**
The placement-k8s charm can leave scale-out units with a stale waiting status after transient database/router readiness failures during resize. The API may recover, but the status does not get cleared, so Sunbeam's all-active wait times out.

**Evidence to collect**
- `generated/sunbeam/output.log` timeout status snapshot.
- `generated/sunbeam/juju_status_openstack.txt` final placement and placement-mysql-router status.
- `generated/sunbeam/kubectl_get_pod.txt` placement pod readiness.
- Placement pod logs from `generated/sunbeam/pods_openstack_logs.tgz`, especially DB connection failures, `placement-api-pebble-check-recovered`, and HTTP 200 responses.
- Placement mysql-router logs for transient `No primaries found in an InnoDB cluster`, Pebble/Kubernetes timeouts, or relation readiness issues.

**Recommended follow-up**
- File/follow a placement-k8s or ops-sunbeam bug so recovered/update-status events re-run the readiness path and clear `WaitingStatus("Placement API not yet serving")` once healthy.
- As an operational workaround, retrigger the placement charm hooks or recycle/scale placement units, then rerun resize after Juju status is active.
