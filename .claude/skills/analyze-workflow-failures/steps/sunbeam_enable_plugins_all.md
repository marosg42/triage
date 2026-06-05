# Step Knowledge: sunbeam_enable_plugins_all

## Step Overview

Enables Sunbeam feature plugins (`vault`, and others specified in `FEATURES_LIST=all`) on the
deployed Sunbeam cluster. Runs `products/sunbeam/enable_features.py`. Key operations include:
- Checking Ubuntu Pro attachment status on all nodes
- Running `sunbeam enable -m manifest.yaml vault`
- Running `sunbeam vault init` to initialise the Vault raft cluster
- Running `sunbeam vault unseal` (one call per key, threshold times) to unseal all Vault pods
- Running `sunbeam vault authorize-charm` to authorize the Vault charm

## Swift Artifacts

Objects stored under `<uuid>/generated/sunbeam/` that are useful for diagnosing failures:

| Path | Description | When to check |
|---|---|---|
| `generated/sunbeam/juju_status_openstack.txt` | Juju status of the openstack model | Always — shows vault unit states at log collection |
| `generated/sunbeam/pods_openstack_logs.tgz` | Pod logs from all openstack-namespace pods | Deep investigation — contains vault-0, vault-1, vault-2 logs |
| `generated/sunbeam/juju_debug_log_openstack.txt` | Juju controller debug log for openstack model | Charm hook errors |

## Key Log Files (inside pods_openstack_logs.tgz)

| File | What it contains | When to use |
|---|---|---|
| `generated/sunbeam/logs-openstack-vault-0.txt` | vault-0 pod log (vault + container-agent) | Primary investigation for raft join / unseal issues |
| `generated/sunbeam/logs-openstack-vault-1.txt` | vault-1 pod log (vault + container-agent) | Vault leader status; shows unseal progress |
| `generated/sunbeam/logs-openstack-vault-2.txt` | vault-2 pod log (vault + container-agent) | Raft join loop errors on the second follower |

Extract with:
```bash
mkdir -p <work_dir>/pods_openstack
tar -xzf <work_dir>/<uuid>/generated/sunbeam/pods_openstack_logs.tgz -C <work_dir>/pods_openstack
ls <work_dir>/pods_openstack/generated/sunbeam/ | grep vault
```

## Grep Patterns

```bash
# Find the vault unit states at collection time
grep -i "vault" <work_dir>/<uuid>/generated/sunbeam/juju_status_openstack.txt

# Find the raft join error on follower nodes
grep "waiting for unseal\|failed to get raft challenge\|Vault is sealed" \
    <work_dir>/pods_openstack/generated/sunbeam/logs-openstack-vault-0.txt | head -20

# Check vault-1 (leader) unseal progress — exit status 2 = partial unseal
grep "exit status" <work_dir>/pods_openstack/generated/sunbeam/logs-openstack-vault-1.txt

# Find the timeout in GH Actions log
grep "wait timed out\|CalledProcessError.*vault.*unseal" <work_dir>/run_<run_id>_failed.log
```

## Known Failure Patterns

### Pattern 1: vault unseal timeout — raft follower bootstrap/challenge not completing

**Symptom (GitHub Actions log):**
```
2026-04-02 04:15:01,875 - ERROR - [localhost] Command failed: ssh ... behaim.maas --
  sunbeam vault unseal uAO+TKsHlKDUJMac/y7GidcoKDRXwLT6/6cI8c3Kc8A1
Error: wait timed out after 599.9999986309995s

subprocess.CalledProcessError: Command '['ssh', ..., 'sunbeam', 'vault', 'unseal', '<key3>']'
  returned non-zero exit status 1.

File "products/sunbeam/enable_features.py", line 362, in enable_vault
    run_remote_command(node, cmd, terminal=True)
```

**Root cause:** `sunbeam vault unseal` successfully unseals the raft leader (vault-1) after
the threshold number of keys has been applied. However, the follower nodes (vault-0, vault-2)
cannot complete the Vault raft bootstrap/challenge handshake before the 600-second timeout
expires. vault-0 continuously receives `"waiting for unseal keys to be supplied"` from vault-1
when attempting to join via the raft bootstrap endpoint. vault-2 (attempting to join vault-0)
receives `"Vault is sealed" (503)` because vault-0 never joined. Both follower nodes remain
stuck indefinitely — their retry loops do not self-resolve.

**Evidence to look for:**
- GitHub log: `Error: wait timed out after 599.999...s` as stdout from `sunbeam vault unseal`
  on the Nth key (N = threshold)
- GitHub log: sequence of "key shares required" messages counting down to 1, then the Nth
  key call failing (not the 1st or 2nd — those succeed in seconds)
- `logs-openstack-vault-0.txt`: `[ERROR] core: failed to retry join raft cluster: retry=2s
  err="waiting for unseal keys to be supplied"` — repeating every ~2s from just after vault-1
  unseals until end of log
- `logs-openstack-vault-1.txt`: only pebble health-check `GET /v1/notices` after unsealing —
  no vault process errors (vault-1 is healthy)
- `logs-openstack-vault-2.txt`: `[ERROR] core: failed to get raft challenge ... Code: 503
  "Vault is sealed"` — vault-2 targets vault-0 as leader, which is still sealed
- `juju_status_openstack.txt`:
  - `vault/1*` blocked: "Please authorize charm" (leader: unsealed, not yet authorized)
  - `vault/0` blocked: "Please unseal Vault" (follower: still sealed)
  - `vault/2` blocked: "Please initialize Vault" (follower: never joined raft)

**First observed:** run 23878110731 (UUID fd88452f, tor3-sqa-testflinger cluster_2,
vault-k8s 1.18/stable rev 446, 2026.1/edge, 2026-04-02)

---

### Pattern 2: vault update-status hook failure — vault/0 enters error state during enable

**Symptom (GitHub Actions log):**
```
2026-04-09 11:17:04,568 - DEBUG - Enabling vault
...
2026-04-09 11:32:50,504 - ERROR - [localhost] Command failed: ssh ... -- sunbeam enable -m manifest.yaml vault
wait timed out after 899.9999945279997s

vault: error  hook failed: "update-status"  (since 09 Apr 2026 11:23:56Z)

subprocess.CalledProcessError: Command ['ssh', ..., 'sunbeam', 'enable', '-m', 'manifest.yaml', 'vault']
  returned non-zero exit status 1.

File "products/sunbeam/enable_features.py", line 343, in enable_vault
    run_remote_command(node, cmd, terminal=True)
```

**Root cause:** The `vault-k8s` charm's `update-status` hook (which runs periodically,
~every 5 minutes) failed during the vault enable sequence, placing `vault/0` into `error`
state and blocking juju-wait for the full 900 seconds. A now-confirmed sub-pattern is that the
hook tries to read TLS material from the Vault workload through Pebble before the workload
Pebble socket exists. In run `f18b4a95-c65a-45de-b484-f2965fd0702d`, the traceback showed
`vault_managers.send_ca_cert()` → `pull_tls_file_from_workload()` raising
`ops.pebble.ConnectionError: Could not connect to Pebble: socket not found at
'/charm/containers/vault/pebble.socket' (container restarted?)`. Kubernetes pod status for
`vault-0` showed the pod was created at `23:36:05Z`, but the workload container did not reach
`startedAt: 23:58:30Z` / `Ready` until well after the pipeline had already failed, so the
charm remained stuck in hook error awaiting resolution.

This is **distinct from Pattern A**: Pattern A fails during `sunbeam vault unseal` after the
raft bootstrap stalls. Pattern B fails during `sunbeam enable vault` itself before the unseal
sequence is reached; the first periodic `update-status` hook hits a missing workload Pebble
socket and the unit enters error before Vault initialization can proceed.

**Evidence to look for:**
- GitHub log: `wait timed out after 899.999...s` as output from `sunbeam enable vault`
  (not `sunbeam vault unseal`)
- GitHub log: `vault/0` / `vault` status `error` with `message='hook failed: "update-status"'`
- Swift `generated/sunbeam/pods_openstack_logs.tgz` → `logs-openstack-vault-0.txt`:
  traceback in the `update-status` hook ending in `ops.pebble.ConnectionError` for
  `/charm/containers/vault/pebble.socket`
- Swift `generated/sunbeam/kubectl_get_pod_detailed.txt`: `vault-0` workload container
  `startedAt` significantly later than the hook failure / step timeout window
- Swift `generated/sunbeam/juju_debug_log_openstack.txt`: matching `update-status` hook
  failure timing if additional charm-side context is needed

**First observed:** run 24181697345 (UUID e15bca12, tor3-sqa-virtual_maas cluster_1,
vault-k8s 1.18/stable rev 446, 2026-04-09)

---

### Pattern 3: observability enable juju-wait timeout — aodh/gnocchi payload containers not ready

**Symptom (GitHub Actions log):**
```
2026-04-09 11:17:54,875 - DEBUG - Enabling observability
...
2026-04-09 11:33:46,640 - ERROR - [localhost] Command failed: ssh ... -- sunbeam enable -m manifest.yaml observability embedded
wait timed out after 899.9999951419995s

aodh: waiting  (workload) Payload container not ready  (since 09 Apr 2026 11:22:13Z)
ceilometer: waiting  (workload) Not all relations are ready  (since 09 Apr 2026 11:19:57Z)
gnocchi: waiting  (workload) Payload container not ready  (since 09 Apr 2026 11:21:50Z)

subprocess.CalledProcessError: Command ['ssh', ..., 'sunbeam', 'enable', '-m', 'manifest.yaml', 'observability', 'embedded']
  returned non-zero exit status 1.

File "products/sunbeam/enable_features.py", line 464, in enable_plugins
    output = run_remote_command(node, cmd, terminal=True)
```

**Root cause:** `sunbeam enable observability embedded` deploys the metering/telemetry stack
(aodh, ceilometer, gnocchi) alongside observability apps. The `aodh-k8s` and `gnocchi-k8s`
pods fail to start (payload container not ready) — the K8s workload container for these
services never reaches running state. This blocks juju-wait for the full 900 seconds.
`ceilometer` is a downstream casualty: it reports `"Not all relations are ready"` because
its gnocchi storage backend is not active.

"Payload container not ready" in Juju K8s charms means the workload container (the actual
OpenStack service, as opposed to the Pebble/charm container) is not running. Possible causes:
`ImagePullBackOff`, `CrashLoopBackOff`, OOMKill, or pod scheduling failure. The specific
reason requires pod logs from Swift (`pods_openstack_logs.tgz`).

**Evidence to look for:**
- GitHub log: `wait timed out after 899.999...s` as output from `sunbeam enable observability embedded`
- GitHub log: `aodh/0` and `gnocchi/0` both showing `(workload) Payload container not ready`
  (typically set ~4–5 min after enable started)
- GitHub log: `ceilometer/0` showing `Not all relations are ready`
- Swift `pods_openstack_logs.tgz`: `logs-openstack-aodh-0.txt` and
  `logs-openstack-gnocchi-0.txt` for the container failure reason

**Grep patterns:**
```bash
# Find stuck apps in GH Actions log
grep "sunbeam_enable_plugins_all" <work_dir>/run_<run_id>_failed.log | \
  grep "Payload container not ready\|Not all relations are ready" | head -10

# Find the observability enable timeout
grep "sunbeam_enable_plugins_all" <work_dir>/run_<run_id>_failed.log | \
  grep "Enabling observability\|wait timed out\|Command failed.*observability"
```

**First observed:** run 24180158005 (UUID e8a7e71d, tor3-sqa-virtual_maas cluster_7,
aodh-k8s 2024.1/beta rev 167, gnocchi-k8s 2024.1/beta rev 171, 2026-04-09)

---

### Pattern 4: orchestration enable timeout — heat-api-cfn CrashLoopBackOff from Pebble port bind conflict

**Symptom (GitHub Actions log):**
```
2026-05-17 22:25:33,474 - DEBUG - [localhost]: ssh ... -- sunbeam enable -m manifest.yaml orchestration
...
wait timed out after 899.9999981600013s
...
'heat/0': UnitStatus(
  workload_status=StatusInfo(current='error', message='crash loop backoff: back-off 5m0s restarting failed container=heat-api-cfn ...'),
)
'heat/1': UnitStatus(workload_status=StatusInfo(current='waiting', message='(workload) Leader not ready', ...))
'heat/2': UnitStatus(workload_status=StatusInfo(current='waiting', message='(workload) Leader not ready', ...))
```

**Root cause:** `sunbeam enable orchestration` deploys the Heat stack. The leader pod (`heat-0`) never becomes healthy because its `heat-api-cfn` container enters `CrashLoopBackOff`. Pod logs show a Pebble startup failure: `cannot run daemon: cannot listen on ":38814": ... bind: address already in use`. Because the leader never comes up, follower units `heat/1` and `heat/2` stay in `Leader not ready`, and Juju waits the full 900 seconds before failing the step.

**Evidence to look for:**
- GitHub log: `sunbeam enable -m manifest.yaml orchestration` followed by `wait timed out after 899.999...s`
- GitHub log / `output.log`: `heat/0` in `error` with `crash loop backoff ... failed container=heat-api-cfn`
- GitHub log / `output.log`: `heat/1` and `heat/2` in `waiting` with `(workload) Leader not ready`
- `generated/sunbeam/kubectl_get_pod.txt`: `heat-0   3/4   CrashLoopBackOff`
- `generated/sunbeam/kubectl_get_pod_detailed.txt`: `heat-api-cfn` terminated with exit code 1 and current state `CrashLoopBackOff`
- `generated/sunbeam/pods_openstack_logs.tgz` → `logs-openstack-heat-0.txt`: `cannot run daemon: cannot listen on ":38814": listen tcp :38814: bind: address already in use`

**First observed:** run 25993877865 (UUID 51d9641b-b5aa-4fe4-b19b-e9dce3808aa1, tor3-sqa-shared_maas dh1_j8_1, branch aipoc, 2026-05-17)

---

### Pattern 5: telemetry enable timeout — openstack-exporter stuck waiting for logging despite healthy telemetry stack

**Symptom (GitHub Actions log):**
```
2026-05-17 03:34:56,928 - DEBUG - Enabling telemetry
2026-05-17 03:34:56,928 - DEBUG - [localhost]: ssh ... -- sunbeam enable -m manifest.yaml telemetry
...
wait timed out after 899.9999983639991s
...
'openstack-exporter': AppStatus(
  app_status=StatusInfo(current='waiting', message='(logging) integration incomplete', since='17 May 2026 02:56:05Z'),
)
'openstack-exporter/0': UnitStatus(
  workload_status=StatusInfo(current='waiting', message='(logging) integration incomplete', since='17 May 2026 02:56:05Z'),
)
```

<<<<<<< Updated upstream
**Root cause:** `sunbeam enable telemetry` waited for the OpenStack exporter to become active, but `openstack-exporter/0` never considered its `logging` relation ready. The exporter pod repeatedly logged `The relation 'logging' is not ready yet` and `No Loki endpoints available`. Swift relation data shows why: on the exporter's `logging` relation, `opentelemetry-collector/1` and `/2` published Loki `endpoint` and `public_address`, but `opentelemetry-collector/0` published only addresses and never exposed an endpoint. Because the exporter charm treated the logging integration as incomplete, Juju waited the full 900 seconds and `sunbeam enable telemetry` failed even though the telemetry/orchestration pods were otherwise running.
=======
**Root cause:** `sunbeam enable telemetry` waited for the OpenStack exporter to become active, but `openstack-exporter/0` never considered its `logging` relation ready. The exporter pod repeatedly logged `The relation 'logging' is not ready yet` and `No Loki endpoints available`. Swift relation data shows why: on the exporter's `logging` relation, at least one `opentelemetry-collector` unit failed to publish the full Loki endpoint data (`endpoint` and `public_address`) even though peer collector units did. Because the exporter charm treated the logging integration as incomplete, Juju waited the full 900 seconds and `sunbeam enable telemetry` failed even though the telemetry/orchestration pods were otherwise running.
>>>>>>> Stashed changes

**Evidence to look for:**
- GitHub log: first telemetry enable succeeded earlier via observability (`OpenStack telemetry application enabled.`), but the later explicit `sunbeam enable -m manifest.yaml telemetry` timed out after 900s
- GitHub log / `output.log`: `openstack-exporter` app and `openstack-exporter/0` unit are the only non-active entries, both showing `(logging) integration incomplete`
- `generated/sunbeam/pods_openstack_logs.tgz` → `logs-openstack-openstack-exporter-0.txt`: repeated `logging:269: The relation 'logging' is not ready yet.` and `No Loki endpoints available`
<<<<<<< Updated upstream
- `generated/sunbeam/show_units_openstack.txt`: relation `269` (`openstack-exporter:logging` ↔ `opentelemetry-collector:receive-loki-logs`) shows `opentelemetry-collector/0` missing `endpoint` / `public_address`, while `/1` and `/2` have both fields
=======
- `generated/sunbeam/show_units_openstack.txt`: on the `openstack-exporter:logging` ↔ `opentelemetry-collector:receive-loki-logs` relation, one collector unit is missing `endpoint` / `public_address` while the peer collector units have both fields (first observed: `/0`; later confirmation: `/1`)
>>>>>>> Stashed changes
- `generated/sunbeam/pods_openstack_logs.tgz` → `logs-openstack-opentelemetry-collector-0.txt`: receive-loki-logs hooks ran and patched the statefulset successfully (`HTTP/1.1 200 OK`), so the failure is not a crashed collector pod

**First observed:** run 25970616074 (UUID 2797dd8d-b892-43c2-9cde-391cdf3eb610, tor3-sqa-shared_maas dh1_j8_1, branch aipoc, 2026-05-17)

---

<<<<<<< Updated upstream
=======
### Pattern 6: observability enable false failure — remote command printed success, then SSH transport/DNS failed

**Symptom (GitHub Actions log):**
```
2026-05-22 11:59:11,912 - DEBUG - [localhost]: ssh ... solqa-shared-maas-server-31.maas -- sunbeam enable -m manifest.yaml observability embedded
...
2026-05-22 12:40:39,458 - ERROR - [localhost] Command failed: ssh ... observability embedded

STDOUT:
OpenStack telemetry application enabled.

STDERR:
Timeout, server solqa-shared-maas-server-31.maas not responding.

subprocess.CalledProcessError: Command ['ssh', ..., 'sunbeam', 'enable', '-m', 'manifest.yaml', 'observability', 'embedded']
  returned non-zero exit status 255.
```

**Root cause:** The failure is not a Juju workload timeout inside observability itself. The remote `sunbeam enable -m manifest.yaml observability embedded` command ran long enough to emit a success message (`OpenStack telemetry application enabled.`), then the SSH transport failed with exit 255 before the session closed cleanly. Immediately afterwards, the runner could no longer resolve several cluster hostnames (`solqa-shared-maas-server-31/32/34/35/36.maas`) while log collection for other nodes (e.g. `...-43`, `...-44`) still worked. The decisive failure is therefore runner-to-node connectivity / name-resolution loss during or immediately after command completion, producing a false negative for the layer.

**Evidence to look for:**
- GitHub log: `OpenStack telemetry application enabled.` in stdout of the failed `observability embedded` SSH command
- GitHub log: same command ends with `Timeout, server <node> not responding.` and `CalledProcessError ... exit status 255`
- Swift `generated/sunbeam/output.log`: within ~1 minute after the failure, sosreport collection fails for `solqa-shared-maas-server-31/32/34/35/36.maas` with `Could not resolve hostname ... Temporary failure in name resolution`
- Swift `generated/sunbeam/output.log`: sosreport collection still succeeds for other nodes afterwards (for example `solqa-shared-maas-server-43.maas`), showing the runner was not completely dead

**First observed:** run 26277892625 (UUID a5528ac9-ddb4-4d70-96f4-85ab4e9d7324, tor3-sqa-shared_maas dh1_j9_1, branch main, 2026-05-22)

---

### Pattern 7: telemetry enable timeout — openstack-exporter keeps waiting even after all Loki endpoints are published

**Symptom (GitHub Actions log):**
```
2026-05-23 07:18:21,048 - DEBUG - Enabling telemetry
2026-05-23 07:18:21,048 - DEBUG - [localhost]: sunbeam enable -m generated/sunbeam//manifest.yaml telemetry
...
wait timed out after 899.9999983770031s
...
'openstack-exporter': AppStatus(
  app_status=StatusInfo(current='waiting', message='(logging) integration incomplete', since='23 May 2026 06:19:02Z'),
)
'openstack-exporter/0': UnitStatus(
  workload_status=StatusInfo(current='waiting', message='(logging) integration incomplete', since='23 May 2026 06:19:02Z'),
)
```

**Root cause:** `sunbeam enable telemetry` waited for `openstack-exporter/0` to become active, but the exporter charm never cleared its `(logging) integration incomplete` status. The exporter pod repeatedly logged `The relation 'logging' is not ready yet` / `No Loki endpoints available` on relation `270` from 06:19:23 onward. However, the collector side was healthy: `opentelemetry-collector/0` successfully ran multiple `receive-loki-logs` hooks with `HTTP/1.1 200 OK`, and `show_units_openstack.txt` captured Loki `endpoint` and `public_address` data for **all three** collector units on the exporter's logging relation. The failure is therefore not missing collector relation data; it is that `openstack-exporter` remained stuck in a stale or incorrect readiness state despite the relation being populated.

This is **distinct from Pattern 5**: Pattern 5 is caused by `opentelemetry-collector/0` failing to publish Loki endpoint data. Here, all collector units published endpoints, but `openstack-exporter` still reported no endpoints and never recovered.

**Evidence to look for:**
- GitHub log: explicit `sunbeam enable -m ... telemetry` times out after 900s
- GitHub log / `output.log`: `openstack-exporter` app and `openstack-exporter/0` are the only non-active entries, both showing `(logging) integration incomplete` since `06:19:02Z`
- `generated/sunbeam/pods_openstack_logs.tgz` → `logs-openstack-openstack-exporter-0.txt`: repeated `logging:270: The relation 'logging' is not ready yet.` and `No Loki endpoints available` at 06:19:23, 06:20:46, 06:24:14, 06:32:01
- `generated/sunbeam/pods_openstack_logs.tgz` → `logs-openstack-opentelemetry-collector-0.txt`: multiple `receive-loki-logs:*` hooks return `HTTP/1.1 200 OK`
- `generated/sunbeam/show_units_openstack.txt`: relation `275` (`openstack-exporter:logging` ↔ `opentelemetry-collector:receive-loki-logs`) shows `endpoint` and `public_address` for `opentelemetry-collector/0`, `/1`, and `/2`

**First observed:** run 26320712194 (UUID 9ee2c32d-844e-4eb6-abc5-f2f6070365fb, tor3-sqa-dedicated_maas dh1_j6, branch main, 2026-05-23)

---

### Pattern 8: observability enable timeout — opentelemetry-collector/0 stayed blocked despite healthy relation hooks

**Symptom (GitHub Actions log):**
```
2026-05-20 21:00:27,053 - DEBUG - [localhost]: ssh ... -- sunbeam enable -m manifest.yaml observability embedded
...
OpenStack telemetry application enabled.
...
wait timed out after 7199.999998331999s
...
'opentelemetry-collector': AppStatus(
  app_status=StatusInfo(current='blocked', message="['cloud-config']|['grafana-dashboards-provider'] for grafana-dashboards-consumer; ['cloud-config']|['send-remote-write'] for metrics-endpoint", ...),
)
'opentelemetry-collector/0': UnitStatus(
  workload_status=StatusInfo(current='blocked', message="['cloud-config']|['grafana-dashboards-provider'] for grafana-dashboards-consumer; ['cloud-config']|['send-remote-write'] for metrics-endpoint", ...),
)
```

**Root cause:** `sunbeam enable observability embedded` progressed far enough to print `OpenStack telemetry application enabled.`, but the command still waits for all deployed applications to converge. In this run the only remaining non-active application was `opentelemetry-collector`, specifically unit `opentelemetry-collector/0`, which stayed blocked from 21:20:17 onward with a cloud-config readiness message tied to `grafana-dashboards-consumer` and `metrics-endpoint`.

Swift artifacts show this was **not** a crashed collector pod or a dead relation endpoint: the collector workload reported `Everything is ready. Begin running and processing data.`, and its `send-remote-write`, `grafana-dashboards-provider`, `grafana-dashboards-consumer`, `metrics-endpoint`, and `receive-loki-logs` hooks all ran successfully, repeatedly patching the StatefulSet with `HTTP/1.1 200 OK`. The failure is therefore a charm-side false negative / stale blocked state on `opentelemetry-collector/0`. Because Juju never cleared that blocked status, the 7200-second wait inside `sunbeam enable observability embedded` expired and the layer failed.

**Evidence to look for:**
- GitHub log: `sunbeam enable -m manifest.yaml observability embedded` followed by stdout `OpenStack telemetry application enabled.` and stderr `wait timed out after 7199.999...s`
- GitHub log / `output.log`: `opentelemetry-collector` app and `opentelemetry-collector/0` unit both `blocked` since `20 May 2026 21:20:17Z`, while `/1` and `/2` are `active`
- `generated/sunbeam/juju_status_openstack.txt`: `openstack-exporter/0` already `active`, but `opentelemetry-collector/0` still `blocked`
- `generated/sunbeam/pods_openstack_logs.tgz` → `logs-openstack-opentelemetry-collector-0.txt`: `otelcol` reaches `Everything is ready. Begin running and processing data.` and relation hooks such as `send-remote-write:260`, `grafana-dashboards-provider:259`, `grafana-dashboards-consumer:252`, and `metrics-endpoint:247/255/256` return `HTTP/1.1 200 OK`

**First observed:** run 26177987791 (UUID 18aae856-3872-4a15-bdca-73c3e4ccd2ed, tor3-sqa-shared_maas dh1_j8_2, branch main, 2026-05-20)

---

### Pattern 9: secrets enable timeout — Barbican bootstraps before the `barbican-api` Pebble socket exists

**Symptom (GitHub Actions log):**
```
2026-05-20 23:27:11,123 - DEBUG - Enabling secrets
2026-05-20 23:27:11,123 - DEBUG - [localhost]: ssh ... -- sunbeam enable -m manifest.yaml secrets
...
wait timed out after 899.9999977460011s
...
'barbican': AppStatus(
  app_status=StatusInfo(
    current='blocked',
    message="(workload) Error in charm (see logs): Could not connect to Pebble: socket not found at '/charm/containers/barbican-api/pebble.socket' (container restarted?)",
  ),
)
'octavia': AppStatus(
  app_status=StatusInfo(current='waiting', message='(barbican-service) integration incomplete', ...),
)

subprocess.CalledProcessError: Command ['ssh', ..., 'sunbeam', 'enable', '-m', 'manifest.yaml', 'secrets']
  returned non-zero exit status 1.
```

**Root cause:** `sunbeam enable secrets` deploys `barbican-k8s` (see `manifest.yaml` `secrets:` section). The Barbican charm immediately begins its bootstrap/configure path, but repeatedly tries to `exec` into the `barbican-api` workload container before that container's Pebble socket exists. Pod logs show the failure originates in `disable_barbican_config()` during multiple hook contexts (`config-changed`, `database`, `vault-kv`, ingress, etc.), each raising `ops.pebble.ConnectionError` for `/charm/containers/barbican-api/pebble.socket`. Because Barbican never becomes ready, downstream `octavia` remains stuck in `(barbican-service) integration incomplete`, and the 900-second wait inside `sunbeam enable secrets` expires. The pod later self-recovers, but only after the pipeline has already failed.

This is **distinct from Pattern 2**: Pattern 2 is a Vault `update-status` hook failure during `sunbeam enable vault`. Here the failure is in the later `secrets` plugin, specifically Barbican startup.

**Evidence to look for:**
- GitHub log / `output.log`: failed command is `sunbeam enable -m manifest.yaml secrets` (not `vault`, `telemetry`, or `observability`)
- GitHub log / `output.log`: timeout snapshot shows `barbican` and `barbican/0` `blocked` with `Could not connect to Pebble: socket not found at '/charm/containers/barbican-api/pebble.socket'`
- GitHub log / `output.log`: `octavia` / `octavia/0` `waiting` with `(barbican-service) integration incomplete`
- `generated/sunbeam/pods_openstack_logs.tgz` → `logs-openstack-barbican-0.txt`: repeated `ops.pebble.ConnectionError` traces from `disable_barbican_config()` while handling `config-changed`, `database`, `vault-kv`, `ingress-*`, `identity-service`, `receive-ca-cert`, and `amqp`
- `generated/sunbeam/juju_debug_log_openstack.txt`: same repeated `Could not connect to Pebble` failures beginning at `23:29:56Z`
- `generated/sunbeam/kubectl_get_pod_detailed.txt`: Barbican pod created at `23:28:26Z`, but workload containers `barbican-api` and `barbican-worker` only reached `startedAt` `23:52:00Z` / `23:52:04Z`, over 8 minutes after the `23:43:32Z` step timeout
- `generated/sunbeam/kubectl_get_pod.txt`: by log-collection time `barbican-0` is `3/3 Running`, confirming late self-recovery rather than a permanent crash

**First observed:** run 26178003700 (UUID e24cd89b-cc16-4b5d-b23b-53469587a63b, tor3-sqa-virtual_maas cluster_2, branch main, 2026-05-20)

---

### Pattern 10: orchestration enable false timeout — Heat pod starts far too late, then self-recovers after the 900s wait expires

**Symptom (GitHub Actions log):**
```
2026-05-21 03:42:12,726 - DEBUG - Enabling orchestration
2026-05-21 03:42:12,726 - DEBUG - [localhost]: ssh ... -- sunbeam enable -m manifest.yaml orchestration
...
wait timed out after 899.999996814s
...
'heat': AppStatus(
  app_status=StatusInfo(current='waiting', message='(workload) Payload container not ready', since='21 May 2026 03:45:19Z'),
)
'heat/0': UnitStatus(
  workload_status=StatusInfo(current='waiting', message='(workload) Payload container not ready', since='21 May 2026 03:45:19Z'),
)

subprocess.CalledProcessError: Command ['ssh', ..., 'sunbeam', 'enable', '-m', 'manifest.yaml', 'orchestration']
  returned non-zero exit status 1.
```

**Root cause:** `sunbeam enable orchestration` waited only 900 seconds for the Heat application to converge, but the `heat/0` workload containers did not actually start until long after the Heat pod had already been created. Swift artifacts show the pod was created at `03:43:25Z`, yet `heat-api`, `heat-api-cfn`, and `heat-engine` only reached `startedAt` at `04:06:37Z`, `04:06:38Z`, and `04:06:44Z` respectively. The charm spent the whole failure window reporting `Payload container not ready`; once the containers finally came up, `db-sync` ran and the unit flipped active at `04:06:56Z`, about 8 minutes after the workflow step had already failed. This is therefore a false negative caused by slow Heat workload bring-up exceeding the command's internal 900-second wait, not by a persistent Heat crash.

This is **distinct from Pattern 4**: Pattern 4 is a real `heat-api-cfn` CrashLoopBackOff caused by Pebble failing to bind `:38814`. Here there is no bind error, no crash loop, and the same pod later becomes healthy without restarts.

**Evidence to look for:**
- GitHub log: failed command is `sunbeam enable -m manifest.yaml orchestration`
- GitHub log: timeout snapshot shows only `heat` / `heat/0` stuck in `(workload) Payload container not ready` since `03:45:19Z`
- `generated/sunbeam/kubectl_get_pod_detailed.txt`: `heat-0` created at `03:43:25Z`, but pod `Ready` only at `04:06:46Z`; workload containers start at `04:06:37Z`, `04:06:38Z`, and `04:06:44Z`
- `generated/sunbeam/pods_openstack_logs.tgz` → `logs-openstack-heat-0.txt`: repeated `update-status` hooks at `03:48`, `03:53`, `03:59`, `04:04` with no crash output; first `heat-api-pebble-ready` / `heat-api-cfn-pebble-ready` only at `04:06:41Z` / `04:06:44Z`, then `Running db-sync` and `Setting active status` at `04:06:48Z` / `04:06:56Z`
- `generated/sunbeam/juju_status_openstack.txt`: by log-collection time, `heat` and `heat/0` are already `active`, confirming delayed self-recovery rather than a permanent failure
- `generated/sshtest.txt`: no SSH tunnel errors during the failure window on `tor3-sqa-virtual_maas`, ruling out virtual-lab connectivity loss as the cause

**First observed:** run 26194750522 (UUID f157df50-84b0-45ec-a247-fbd800e8cbb6, tor3-sqa-virtual_maas cluster_1, branch main, 2026-05-21)

---

### Pattern 11: telemetry enable timeout — partial Loki endpoint publication leaves several apps stuck on `(logging) integration incomplete`

**Symptom (GitHub Actions log):**
```
2026-05-21 04:42:38,359 - DEBUG - Enabling telemetry
2026-05-21 04:42:38,359 - DEBUG - [localhost]: ssh ... -- sunbeam enable -m manifest.yaml telemetry
...
wait timed out after 899.9999967229996s
...
'bind/0': UnitStatus(... message='(logging) integration incomplete', since='21 May 2026 04:04:05Z')
'ceilometer/0': UnitStatus(... message='(logging) integration incomplete', since='21 May 2026 04:04:14Z')
'designate/0': UnitStatus(... message='(logging) integration incomplete', since='21 May 2026 04:04:26Z')
'glance/0': UnitStatus(... message='(logging) integration incomplete', since='21 May 2026 04:04:25Z')
'gnocchi/0': UnitStatus(... message='(logging) integration incomplete', since='21 May 2026 04:04:25Z')
'nova/0': UnitStatus(... message='(logging) integration incomplete', since='21 May 2026 04:04:24Z')
'openstack-exporter/0': UnitStatus(... message='(logging) integration incomplete', since='21 May 2026 04:04:05Z')

subprocess.CalledProcessError: Command ['ssh', ..., 'sunbeam', 'enable', '-m', 'manifest.yaml', 'telemetry']
  returned non-zero exit status 1.
```

**Root cause:** `sunbeam enable observability embedded` had already deployed the collector and printed success, but the subsequent explicit `sunbeam enable telemetry` waited on multiple OpenStack applications whose `logging` relations never became ready. Swift relation data shows the failure was **partial/inconsistent Loki endpoint publication** from `opentelemetry-collector/0`: some relations (for example `aodh:logging`) contained `endpoint` and `public_address`, while others (`bind:logging`, `openstack-exporter:logging`, and the other stuck apps) had only addresses and no Loki endpoint fields. The waiting pods themselves were running, and the collector pod was healthy and repeatedly completed `receive-loki-logs`, `send-remote-write`, and `grafana-dashboards-provider` hooks with `HTTP/1.1 200 OK`. The failure is therefore not a crashed pod or tunnel outage; it is a charm-side relation-data fan-out defect that leaves a subset of applications permanently stuck in `(logging) integration incomplete` until the 900-second wait expires.

**Evidence to look for:**
- GitHub log / `output.log`: `sunbeam enable -m manifest.yaml observability embedded` succeeded at `04:24:07Z`, but the later explicit `sunbeam enable -m manifest.yaml telemetry` failed after 900s
- GitHub log / `output.log`: seven applications (`bind`, `ceilometer`, `designate`, `glance`, `gnocchi`, `nova`, `openstack-exporter`) all still show `(logging) integration incomplete`, with `since` timestamps clustered around `04:04Z`
- `generated/sunbeam/show_units_openstack.txt`: `aodh:logging` relation `273` contains collector `endpoint` and `public_address`, but `bind:logging` relation `261` and `openstack-exporter:logging` relation `262` do not
- `generated/sunbeam/pods_openstack_logs.tgz` → `logs-openstack-openstack-exporter-0.txt`: repeated `logging:262: The relation 'logging' is not ready yet.` / `No Loki endpoints available`
- `generated/sunbeam/pods_openstack_logs.tgz` → `logs-openstack-opentelemetry-collector-0.txt`: repeated `receive-loki-logs:*`, `send-remote-write:*`, and `grafana-dashboards-provider:*` hooks return `HTTP/1.1 200 OK`
- `generated/sunbeam/kubectl_get_pod.txt`: `bind-0`, `ceilometer-0`, `designate-0`, `glance-0`, `gnocchi-0`, `nova-0`, `openstack-exporter-0`, and `opentelemetry-collector-0` are all `Running`
- `generated/sshtest.txt`: no SSH tunnel errors during the failure window on `tor3-sqa-virtual_maas`, ruling out virtual-lab connectivity loss

**First observed:** run 26190744612 (UUID 2532ed1e-77b5-432c-9976-39c61b3946a8, tor3-sqa-virtual_maas cluster_3, branch main, 2026-05-21)

**Second observed:** run 26539853785 (UUID 6e1deb7a-c2b5-4f0f-9a63-90f0223d3397, tor3-sqa-virtual_maas cluster_7, branch main, 2026-05-27)

---

### Pattern 12: tls enable false failure — virtual_maas tunnel dropped during the SSH session

**Symptom (GitHub Actions log):**
```
2026-05-21 09:53:06,306 - DEBUG - Enabling tls
2026-05-21 09:53:06,397 - DEBUG - [localhost]: ssh ... node1.dh1-j6.tor3-sqa-dedicated-maas.solutionsqa -- sunbeam enable -m manifest.yaml tls ca --ca ...
...
2026-05-21 09:56:08,655 - ERROR - [localhost] Command failed: ssh ... -- sunbeam enable -m manifest.yaml tls ca --ca ...

STDERR:
Connection to node1.dh1-j6.tor3-sqa-dedicated-maas.solutionsqa closed by remote host.

subprocess.CalledProcessError: Command ['ssh', ..., 'sunbeam', 'enable', '-m', 'manifest.yaml', 'tls', 'ca', ...] returned non-zero exit status 255.
```

**Root cause:** This is a `virtual_maas` connectivity failure, not a confirmed TLS logic/charm failure. The runner began `sunbeam enable tls ca` at `09:53:06Z`, but `generated/sshtest.txt` shows the SSH tunnel to the virtual lab failed less than a minute later: the last successful probe returned `infra1` at `09:53:57Z`, then the next probe immediately hit `kex_exchange_identification: Connection closed by remote host`. The failing TLS command then died with SSH exit `255` / `Connection ... closed by remote host`, and post-failure collection could no longer SSH to `10.241.144.2` at all (`lsb_release -cs` exit `255`, then `ssh: connect to host 10.241.144.2 port 22: Connection timed out`). Because the transport to the virtual MAAS infra collapsed during the command, the workflow recorded a false failure for `sunbeam_enable_plugins_all`; whether the remote `sunbeam enable tls ca` operation made progress on the target is unknowable from the collected logs.

**Evidence to look for:**
- `jobs.json`: substrate is `tor3-sqa-virtual_maas` / `cluster_4`, so `generated/sshtest.txt` is authoritative for tunnel health
- GitHub log / `generated/sunbeam/output.log`: `sunbeam enable telemetry` succeeded at `09:53:06Z`, then `sunbeam enable tls ca` started immediately afterwards
- `generated/sshtest.txt`: last good probe `Thu May 21 09:53:57 UTC 2026` returned `infra1`, followed immediately by `kex_exchange_identification: Connection closed by remote host`
- GitHub log: the TLS SSH command failed at `09:56:08Z` with `Connection to node1... closed by remote host` / exit `255`
- GitHub log: later log collection also failed to reach `10.241.144.2` (`ssh ... lsb_release -cs` exit `255` at `10:05:25Z`; `ssh: connect to host 10.241.144.2 port 22: Connection timed out` at `10:07:52Z`)

**First observed:** run 26204892461 (UUID 88ff9957-d5e5-44a4-9c0d-cadb70eba5e9, tor3-sqa-virtual_maas cluster_4, branch main, 2026-05-21)

---

### Pattern 13: observability enable false failure — virtual_maas SSH tunnel dropped mid-command

**Symptom (GitHub Actions log):**
```
2026-05-21 09:49:52,915 - DEBUG - Enabling observability
2026-05-21 09:49:52,915 - DEBUG - [localhost]: ssh ... node1.dh1-j6.tor3-sqa-dedicated-maas.solutionsqa -- sunbeam enable -m manifest.yaml observability embedded
...
2026-05-21 09:55:54,411 - ERROR - [localhost] Command failed: ssh ... -- sunbeam enable -m manifest.yaml observability embedded

STDERR:
Connection to node1.dh1-j6.tor3-sqa-dedicated-maas.solutionsqa closed by remote host.

subprocess.CalledProcessError: Command ['ssh', ..., 'sunbeam', 'enable', '-m', 'manifest.yaml', 'observability', 'embedded'] returned non-zero exit status 255.
```

**Root cause:** This is a `virtual_maas` tunnel failure, not a confirmed observability / Juju workload timeout. `jobs.json` identifies the run substrate as `tor3-sqa-virtual_maas` / `cluster_3`, so `generated/sshtest.txt` is authoritative for tunnel health even though the SSH target hostname uses the `dh1-j6.tor3-sqa-dedicated-maas.solutionsqa` domain. The runner started `sunbeam enable -m manifest.yaml observability embedded` at `09:49:52Z`, but the SSH tunnel to the virtual lab later dropped: the last successful `sshtest.txt` probe returned `infra1` at `09:53:32 UTC`, then the next probe immediately failed with `kex_exchange_identification: Connection closed by remote host`. Two minutes later the still-running `sunbeam enable observability embedded` SSH session died with exit `255` / `Connection ... closed by remote host`. Post-failure collection then repeatedly failed to reach the tunnel endpoint `10.241.144.2` over SSH (`Connection timed out`). The layer therefore failed because runner-to-lab transport collapsed mid-command; whether the remote observability enable operation made any progress after the tunnel dropped is unknowable from the collected logs.

This is **distinct from Patterns 3 and 8**: those are real observability workload convergence failures inside Sunbeam. Here the decisive failure is loss of SSH transport to the virtual lab.

**Evidence to look for:**
- `jobs.json`: substrate `tor3-sqa-virtual_maas`, cluster `cluster_3`
- GitHub log / `generated/sunbeam/output.log`: `sunbeam enable -m manifest.yaml observability embedded` started at `09:49:52Z` and failed at `09:55:54Z` with SSH exit `255`
- `generated/sshtest.txt`: last good probe `Thu May 21 09:53:32 UTC 2026` returned `infra1`, followed immediately by `kex_exchange_identification: Connection closed by remote host`
- `generated/version_collector_sunbeam_enable_plugins_all.log`: repeated post-failure SSH attempts to `10.241.144.2` timed out at `09:58:27Z`, `10:00:42Z`, `10:02:58Z`, and `10:05:13Z`
- GitHub log / `generated/lastlines.txt`: no Sunbeam/Juju timeout snapshot for observability was captured before connectivity was lost

**First observed:** run 26208201711 (UUID 553ad773-f235-4643-aa06-c7bc6fe4c94b, tor3-sqa-virtual_maas cluster_3, branch main, 2026-05-21)

---

### Pattern 14: secrets enable false failure — Sunbeam CLI printed success, then aborted on a closed HTTP connection

**Symptom (GitHub Actions log):**
```
2026-05-25 11:46:34,095 - DEBUG - Enabling secrets
2026-05-25 11:46:34,095 - DEBUG - [localhost]: sunbeam enable -m generated/sunbeam//manifest.yaml secrets
...
2026-05-25 12:15:35,953 - DEBUG - OpenStack secrets application enabled.
2026-05-25 12:15:35,989 - DEBUG - An unexpected error has occurred. Please see .../inspecting-the-cluster/ for troubleshooting information.
2026-05-25 12:15:35,989 - DEBUG - Error: ('Connection aborted.', RemoteDisconnected('Remote end closed connection without response'))
...
subprocess.CalledProcessError: Command ['sunbeam', 'enable', '-m', 'generated/sunbeam//manifest.yaml', 'secrets'] returned non-zero exit status 1.
```

**Root cause:** The failure is a false negative in the Sunbeam CLI client path, not a failed secrets deployment. `products/sunbeam/enable_features.py` simply shells out to `sunbeam enable -m <manifest> secrets` and treats any non-zero exit as fatal. In this run the CLI spent ~29 minutes enabling the secrets stack, printed `OpenStack secrets application enabled.`, then immediately aborted with `('Connection aborted.', RemoteDisconnected('Remote end closed connection without response'))`. Snapshot Juju status collected ~2 minutes later showed the `openstack` model fully healthy: `vault`, `barbican`, `openstack-exporter`, `aodh`, `gnocchi`, `ceilometer`, and `heat` were all `active/idle`. The specific failure condition was therefore that the long-running client request lost its final HTTP response path after the workload had already converged, causing the CLI to exit 1 and the wrapper to raise `CalledProcessError` even though the secrets applications were successfully enabled.

This is **distinct from Pattern 9**: Pattern 9 is a real Barbican startup failure where `barbican-api` never reaches a usable Pebble socket and the application remains blocked. Here both `barbican` and `vault` were already `active/idle` at snapshot time.

**Evidence to look for:**
- GitHub log: failed command is `sunbeam enable -m ... secrets` and stdout already contains `OpenStack secrets application enabled.` immediately before the exception
- GitHub log: stderr ends with `('Connection aborted.', RemoteDisconnected('Remote end closed connection without response'))`, not a Juju timeout or charm hook error
- Workflow source `products/sunbeam/enable_features.py`: line 464/466 shells out to `sunbeam enable ... <plugin>`; line 466 passes failures through `local(...)` without post-success reconciliation
- Workflow source `products/sqa_common/helpers.py`: `run_cmd()` raises `CalledProcessError` on any non-zero return code after logging stdout/stderr
- Swift `generated/marvin-the-happy-bot/snapshot-2026-05-25T12-17-24Z/juju/external-juju-controller/openstack/status.yaml`: `vault/0..2`, `barbican/0..2`, `openstack-exporter/0`, `aodh/0..2`, `gnocchi/0..2`, `ceilometer/0..2`, and `heat/0..2` are all `active/idle`
- Swift `generated/version_collector_sunbeam_enable_plugins_all.log`: post-failure collection still reached Juju and gathered model status successfully, so this was not a controller outage

**First observed:** run 26388146493 (UUID c7593d1c-59c9-47dd-ba7a-51aac3cce2d0, tor3-sqa-dedicated_maas dh1_j6, branch aipoc, 2026-05-25)

---

### Pattern 15: telemetry enable timeout — Juju CMR event delivery failure leaves loki/0 unhookable, breaking the entire Loki URL chain

**Trigger:** `sunbeam enable -m manifest.yaml telemetry` waits 900s and exits with
`wait timed out after 899.9999...s`.

**Symptom:**
```
'openstack-exporter': AppStatus(
  status='waiting',
  message='(logging) integration incomplete',
  since='30 May 2026 06:55:11Z',
)
'openstack-exporter/0': UnitStatus(
  workload_status=StatusInfo(current='waiting', message='(logging) integration incomplete', since='...'),
  agent_status=StatusInfo(current='idle', since='30 May 2026 07:05:20Z'),
)
```

**Root cause:** The cross-model relation (CMR) between the `loki-logging` offer (in the
observability model, backed by `loki/0`) and `opentelemetry-collector` (in the openstack model,
via `send-loki-logs`) was established at the Juju level (`2/2` connected), but the Juju
controller never delivered `logging-relation-joined` or `logging-relation-changed` hook events
to `loki/0`. Because Loki never processed these hooks, it never wrote its push-API URL into the
relation databag. Consequently, `opentelemetry-collector` also never received the Loki URL via
`send-loki-logs-relation-joined/changed` (these events likewise never fired on any unit).
`openstack-exporter/0` received the `logging-relation-joined` events from the three otelcol
units but each returned "No Loki endpoints available" and it remained permanently stuck.

This is **distinct from Patterns 5/7/11**: those patterns involve `opentelemetry-collector`
failing to publish the Loki URL to `openstack-exporter`. In Pattern 15, the failure is one
step earlier: the Loki provider unit never receives the CMR events at all and never publishes
the URL into the shared databag.

**Evidence to look for:**
- `juju_debug_log_observability.txt`: `loki/0` runs only its initial lifecycle hooks (install,
  leader-elected, pebble-ready, config-changed) — **zero `logging-relation-*` hooks** for the
  entire run, even though `loki-logging` offer shows `2/2` connected
- `juju_debug_log_openstack.txt`: zero `send-loki-logs-relation-joined` or
  `send-loki-logs-relation-changed` events for any `opentelemetry-collector` unit; only
  `send-loki-logs-relation-created` hooks appear
- `juju_debug_log_openstack.txt` on `openstack-exporter/0`: repeated
  `"The relation 'logging' is not ready yet."` and `"No Loki endpoints available"` on every
  `logging-relation-changed` event; last event at ~07:05:20Z then no further hook fires
- GitHub `##[error]` line: `wait timed out after 899.9999...s`
- `juju_status_openstack.txt` (Swift): `openstack-exporter/0` `waiting: (logging) integration
  incomplete`; `opentelemetry-collector` scale=3 all `active idle`
- `juju_status_observability.txt` (Swift): `loki-logging` offer `2/2` connected; `loki/0`
  `active idle`

**First observed:** run 26672262187 (UUID 274bb7c7-8088-4c75-93e2-ea5f72281911,
tor3-sqa-testflinger cluster_1, branch main, Juju 3.6.23, 2026-05-30)

---

>>>>>>> Stashed changes
_Add more patterns below as they are discovered._

## Notes

- `sunbeam vault init` accepts `<shares> <threshold>` as positional args. In the pattern
  above: 5 shares, threshold 3. The first `threshold-1` unseal calls succeed quickly; the
  Nth call (which completes the quorum on the leader) starts the 600s wait for all pods.
- The vault raft follower bootstrap requires the follower to present unseal keys as part of
  the challenge/response; `sunbeam vault unseal` appears not to drive this protocol for
  follower nodes.
- `vault/2` sometimes targets vault-0 rather than vault-1 as the raft leader candidate —
  this is controlled by `vault.hcl` `retry_join` stanza generated by the charm.
- Substrate `tor3-sqa-testflinger` has no MAAS logs. Investigation is limited to GitHub
  Actions logs and Swift pod logs.

## Version History
- **v1.16** (2026-06-04): Updated Pattern 11 with another confirmation from run 26539853785 / UUID 6e1deb7a-c2b5-4f0f-9a63-90f0223d3397 on tor3-sqa-virtual_maas cluster_7 — `sunbeam enable telemetry` timed out because `opentelemetry-collector/0` published Loki endpoint data to `openstack-exporter:logging` (relation 229) but failed to publish it to `gnocchi` (relation 247), `horizon` (relation 250), `tempest` (relation 227), `placement`, and `ovn-relay`.

- **v1.0** (2026-04-02): Initial version — Pattern A from run 23878110731 (UUID fd88452f)
- **v1.1** (2026-04-09): Added Pattern B (vault `update-status` hook failure, run 24181697345 / UUID e15bca12) and Pattern C (observability enable timeout — aodh/gnocchi payload containers not ready, run 24180158005 / UUID e8a7e71d)
- **v1.2** (2026-05-19): Added Pattern D — `sunbeam enable orchestration` timeout caused by `heat/0` `heat-api-cfn` CrashLoopBackOff; pod log shows Pebble failed to bind `:38814` (`address already in use`) in run 25993877865 / UUID 51d9641b-b5aa-4fe4-b19b-e9dce3808aa1
- **v1.3** (2026-05-19): Added Pattern E — `sunbeam enable telemetry` timed out because `openstack-exporter/0` stayed in `(logging) integration incomplete`; `opentelemetry-collector/0` failed to publish Loki endpoint data on relation 269 while `/1` and `/2` did, in run 25970616074 / UUID 2797dd8d-b892-43c2-9cde-391cdf3eb610
<<<<<<< Updated upstream
=======
- **v1.4** (2026-05-25): Added Pattern F — `sunbeam enable observability embedded` printed `OpenStack telemetry application enabled.` but the SSH transport then died with `Timeout, server ... not responding.` / exit 255, and immediate post-failure collection showed `Temporary failure in name resolution` for multiple `.maas` hosts on the runner; from run 26277892625 / UUID a5528ac9-ddb4-4d70-96f4-85ab4e9d7324
- **v1.5** (2026-05-25): Added Pattern G — `sunbeam enable telemetry` timed out because `openstack-exporter/0` stayed in `(logging) integration incomplete` even though relation `275` already contained Loki `endpoint` / `public_address` data from all three `opentelemetry-collector` units, indicating exporter-side readiness failed to clear; from run 26320712194 / UUID 9ee2c32d-844e-4eb6-abc5-f2f6070365fb
- **v1.6** (2026-05-25): Updated Pattern E with another confirmation from run 26355836799 / UUID 3c300381-f938-4cdf-8bd9-ca14a1de9b31 on tor3-sqa-dedicated_maas dh1_j6 — this time `openstack-exporter:logging` relation `251` had Loki `endpoint` / `public_address` for `opentelemetry-collector/0` and `/2`, but not `/1`; exporter still logged `No Loki endpoints available` and blocked `sunbeam enable telemetry` for 900s.
- **v1.7** (2026-05-25): Added Pattern H — `sunbeam enable observability embedded` timed out after 7200s on tor3-sqa-shared_maas dh1_j8_2 even though stdout already printed `OpenStack telemetry application enabled.`; `opentelemetry-collector/0` stayed blocked on cloud-config readiness while pod logs showed healthy workload startup and successful relation hooks, indicating a stale/false blocked status; from run 26177987791 / UUID 18aae856-3872-4a15-bdca-73c3e4ccd2ed.
- **v1.8** (2026-05-25): Updated Pattern B with a confirmed vault `update-status` root cause from run 26178017374 / UUID f18b4a95-c65a-45de-b484-f2965fd0702d — `vault_managers.send_ca_cert()` tried to pull TLS files before the workload Pebble socket existed, raising `ops.pebble.ConnectionError`; `kubectl_get_pod_detailed.txt` showed the `vault` workload container did not start until after the pipeline timeout.
- **v1.9** (2026-05-25): Added Pattern I — `sunbeam enable secrets` timed out because `barbican-k8s` repeatedly tried to `exec` into `/charm/containers/barbican-api/pebble.socket` before the workload container started; timeout snapshot showed `barbican` blocked and downstream `octavia` waiting on `(barbican-service) integration incomplete`, while `kubectl_get_pod_detailed.txt` showed `barbican-api` / `barbican-worker` only started after the failure window; from run 26178003700 / UUID e24cd89b-cc16-4b5d-b23b-53469587a63b.
- **v1.10** (2026-05-25): Added Pattern J — `sunbeam enable orchestration` timed out after 900s because `heat/0` stayed in `(workload) Payload container not ready` for ~21 minutes after pod creation, then self-recovered and became active around 8 minutes after the workflow step had already failed; no CrashLoopBackOff or Pebble bind error was present; from run 26194750522 / UUID f157df50-84b0-45ec-a247-fbd800e8cbb6.
- **v1.11** (2026-05-25): Added Pattern K — `sunbeam enable telemetry` timed out on tor3-sqa-virtual_maas / cluster_3 because `opentelemetry-collector/0` published Loki endpoint data to some logging relations (for example `aodh`) but not others (`bind`, `openstack-exporter`, and additional OpenStack apps), leaving several applications stuck in `(logging) integration incomplete` even though all related pods were `Running`; from run 26190744612 / UUID 2532ed1e-77b5-432c-9976-39c61b3946a8.
- **v1.12** (2026-05-25): Added Pattern L — `sunbeam enable tls ca` failed with SSH exit `255` on tor3-sqa-virtual_maas / cluster_4 because the virtual-lab SSH tunnel dropped mid-command; `generated/sshtest.txt` shows the last successful probe at `09:53:57Z` followed immediately by `kex_exchange_identification`, and post-failure log collection could no longer reach `10.241.144.2`; from run 26204892461 / UUID 88ff9957-d5e5-44a4-9c0d-cadb70eba5e9.
- **v1.13** (2026-05-25): Added Pattern M — `sunbeam enable observability embedded` failed with SSH exit `255` on tor3-sqa-virtual_maas / cluster_3 because the virtual-lab SSH tunnel dropped mid-command; `generated/sshtest.txt` shows the last successful probe at `09:53:32Z` followed immediately by `kex_exchange_identification`, and post-failure version collection could no longer reach `10.241.144.2`; from run 26208201711 / UUID 553ad773-f235-4643-aa06-c7bc6fe4c94b.
- **v1.14** (2026-05-27): Added Pattern N — `sunbeam enable secrets` printed `OpenStack secrets application enabled.` but then the Sunbeam CLI exited with `('Connection aborted.', RemoteDisconnected('Remote end closed connection without response'))`; snapshot Juju status showed the openstack model already fully active/idle, so the layer was a false negative caused by a post-success client-side HTTP disconnect; from run 26388146493 / UUID c7593d1c-59c9-47dd-ba7a-51aac3cce2d0.
- **v1.15** (2026-05-30): Added Pattern 15 — `sunbeam enable telemetry` timed out because `loki/0` (observability model) never processed any `logging-relation-*` hooks despite the `loki-logging` CMR offer showing `2/2` connected; consequently `opentelemetry-collector` never received the Loki URL via `send-loki-logs-relation-joined/changed` (those events never fired) and `openstack-exporter/0` remained permanently in `(logging) integration incomplete`; Juju CMR event delivery failure on Juju 3.6.23; from run 26672262187 / UUID 274bb7c7-8088-4c75-93e2-ea5f72281911.
>>>>>>> Stashed changes
