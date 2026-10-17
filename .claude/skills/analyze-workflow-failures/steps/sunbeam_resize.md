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

**Variant — rollingops worker (mysql-k8s rev 459+)**
- Restart runs through the `charmlibs.rollingops` async worker, so the KeyError appears as `Operation failed: restart: 'pop from an empty set'` (retry-release), **not** as a hook error. `mysql/0` may never show `hook failed`.
- Escalation, "full GR outage loop": `Failed to make instance mysql-N ... the primary` / `Changing primary failed` → `Group Replication not running (state='OFFLINE')` and mysqld `[GCS] Unable to join the group: peers not configured` → ~10-minute cycle of `Attempting reboot from complete outage` → ONLINE PRIMARY → restart again; later `Operation failed: restart: RetryError`.
- Downstream: all ~30 `*-mysql-router` units in `update-status`/`start` hook error, with ER 1290 super-read-only, `Failed to bootstrap router`, and `not an ONLINE member`; `cinder-volume-mysql-router` stuck in maintenance `Waiting for K8s service connectivity`.
- Grep `logs-openstack-mysql-0.txt` for `callback_id=restart, attempt=` and `peers not configured`.
- **Distinguish:** in the base pattern the cluster stays healthy. In this variant there is no primary.
- From run 38630178591.
- Trigger seen in run 38670103606: `config-changed` → `Configuration change requires restart` about 1 minute after resize starts, while mysql/0 is the only member. The first GR outage (05:19–05:28, `peers not configured`) comes **before** the first KeyError. Grep from the top of `logs-openstack-mysql-0.txt`, because live `kubectl logs` views can miss this early window. The cluster self-healed seconds after the 3600 s timeout, with mysql-1 joining through clone fallback.
- Ordering in run 38951877587 (testflinger cluster_3): `Configuration change requires restart` came about 50 s after resize started. The KeyError fired first, at attempt=0 and attempt=1. After `database-peers-relation-joined`, attempt=2 picked **non-member** mysql-1 → `Failed to make instance mysql-1... the primary` / `Changing primary failed`. mysql-0 was restarted anyway, then `peers not configured` → OFFLINE. This is the outage trigger. The 9-minute RetryError/complete-outage loop ran 11 times (02:18–03:50) and **did not** self-heal; mysql-1 and mysql-2 never joined. Grep for `callback_id=restart, attempt=2` followed by `Failed to make instance`.
- Order can also be reversed (run 38951859295, testflinger cluster_2): the restart is triggered by the same `config-changed` about 1 min after resize. Then:
  - The KeyError retries come **first** (attempts 0–12, 04:29–04:33, while mysql/0 is alone).
  - When peers appear, attempt 13 → `Failed to make instance mysql-1 ... the primary` (mysql-1 is still in clone fallback). mysql/0 restarts anyway, and the first `peers not configured` follows.
  - The 10-min outage loop then lasts **past** the timeout, until log collection (06:03). mysql-1 and mysql-2 never join and stay `waiting to get cluster primary from peers`, so there is **no** self-heal.
  - Final status shows `mysql/0` in maintenance `recovering unit after restart`.

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

### SSH transport loss during resize (runner→lab network outage)

**Signature**
- Runner fails in `cmd_resize -> resize_cluster -> run_remote_command(..., "sunbeam cluster resize")` with `CalledProcessError ... returned non-zero exit status 255`. `output.log` shows only `Permanently added ...`, then empty STDOUT and `Timeout, server <bootstrap>.maas not responding.` (resize prints only at the end). There is **no** sunbeam `Timed out waiting ...`, Juju status dump or charm error.
- Post-failure sosreport/scp to **all** nodes fails with `kex_exchange_identification: read: Connection reset by peer` / `Connection reset by <ip> port 22`. Some resets are immediate and some come after about 60 s, which points to a network-path outage rather than a single-node crash.
- `generated/sshuttle_metallb_range.txt` (shared_maas/testflinger) may flip to `sshuttle DOWN`, either before or after the failure. sshuttle runs through the bootstrap node and carries only the external CIDR plus metallb /32s, so DOWN is a symptom or early indicator of path blips, not the cause.

**Likely cause**
The runner lost connectivity to the lab management subnet (e.g. 10.241.2.0/24) while the nodes stayed up. This is infra/lab, not a Sunbeam defect. On testflinger, runner outbound TCP goes through `aproxy_nosni`, so connect-OK/no-data and kex resets come from the local relay when the upstream lab is unreachable. The resize result on the node is unknown, because no node logs are collected.

**Evidence to collect**
- Keepalive math: `run_remote_command` uses `ServerAliveInterval=60 ServerAliveCountMax=10`, so the node's last response was about 10 min before the timeout. Do not assume the outage began at the handshake, or that resize never ran.
- Successful SSH sessions after any earlier `sshuttle DOWN`, which show when the sustained outage actually began.
- Reset/timeout status for every node during log collection.
- Testflinger: if no job prints `Cannot cancel`, reservations were still active, which rules out reservation expiry.
- happy-bot live ICMP/banner/MAAS-API probes, if present, help place the break.

**Distinguish:** this is the same family as `sunbeam_create_cluster.md` Pattern 1. Do not confuse it with the 3600 s OpenStack-wait patterns.

**Recommended follow-up**
- Report the outage window to lab/infra. In the pipeline, run long sunbeam commands detached on the node and poll for completion, or retry once on exit 255.

**Observed in:** runs 38869707627 (shared_maas dh1_j8_1), 38869728772 (testflinger cluster_1) and 38869733991 (testflinger cluster_2), all 2026-10-16. Keepalives were lost around 10:39, failures came at about 10:49, and kex RSTs on 7/7 nodes continued until at least 11:03.

### OpenStack model wait timeout with cinder-k8s Pebble stop failure

**Signature**
- `cinder/N` blocked with `(workload) Error in charm (see logs): ... Stop service "wsgi-cinder-api" (cannot stop service: process still running after SIGTERM and SIGKILL)`.
- The cinder pod shows `2/3` Ready.
- `logs-openstack-cinder-N.txt` repeats `Check "online" failure N/3 ... apache2 is not running`.

**Likely cause**
ops_sunbeam restarts the wsgi service on every hook during relation joins. If one stop fails, no later hook restarts the service, so the unit stays blocked. This can happen together with the mysql-k8s restart wedge above.

**Evidence to collect**
- `generated/sunbeam/juju_status_openstack.txt` for the cinder unit message.
- `generated/sunbeam/kubectl_get_pod.txt` for cinder pod readiness.
- `logs-openstack-cinder-N.txt` from `pods_openstack_logs.tgz` for the `Check "online" failure` loop.

**Recommended follow-up**
- File/follow a cinder-k8s / ops_sunbeam bug so a failed Pebble stop is retried or the service is restarted on the next hook.
- From run 38630178591.

### 'Deploy Cinder Volume' 1200 s timeout — cinder-volume unit stuck on stale "Not all relations are ready"

**Signature**
- Runner `CalledProcessError ... 'sunbeam', 'cluster', 'resize' ... exit status 1` (not 255).
- `output.log`: `Application 'cinder-volume' is not ready: TimeoutError("wait timed out after 1199.9…s` with an `openstack-machines` status dump (**not** the 3600 s OpenStack control-plane wait).
- One `cinder-volume/N` is `waiting` with `(workload) Not all relations are ready`. All other cinder-volume and cinder-volume-ceph units are active, and keystone/rabbitmq/mysql in the `openstack` model are active.

**Likely cause**
While the control plane scales out (keystone 1→3), the machine charm runs an unrelated hook (for example `amqp-relation-changed`) while the CMR `identity-credentials` relation is briefly seen as incomplete. ops_sunbeam then sets waiting. `update-status` does not re-run the Bootstrapping section, so the status stays waiting unless another identity-credentials event arrives. Units that receive a later keystone-credentials join recover; a unit that already got all of its joins stays stuck. The final relation data is complete and matches the healthy units.

**Evidence to collect**
- `juju_debug_log_openstack-machines.txt` has gaps. Use the sosreport for the unit's machine (`sosreport-<host>-*.tar.xz` → `var/log/juju/unit-cinder-volume-N.log`) and grep `incomplete\|Not all relations\|identity-credentials-relation`.
- Bootstrap-node sunbeam log (`sosreport-<bootstrap>/home/ubuntu/snap/openstack/common/logs/sunbeam-*.log`): `Starting step 'Deploy Cinder Volume'` / `FAILED`, to show the unit was stuck before the wait began.
- `show_units_openstack-machines.txt`: compare the stuck unit's `identity-credentials` application-data with a healthy unit's.
- Map machine number to host with `juju_machines_openstack-machines.txt` + `hosts`.

**Recommended follow-up**
- File/follow an ops_sunbeam / cinder-volume charm bug so relation readiness is re-evaluated (for example on update-status) after a transient gap.
- From run 38924438552 (cinder-volume rev 205, keystone-k8s rev 459). `cinder-volume/1` went waiting at 17:44:09 after `Relations {'identity-credentials'} incomplete` and stayed waiting until log collection.
