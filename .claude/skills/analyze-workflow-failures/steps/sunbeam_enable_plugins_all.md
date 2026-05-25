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
mkdir -p /tmp/pods_openstack
tar -xzf <work_dir>/<uuid>/generated/sunbeam/pods_openstack_logs.tgz -C /tmp/pods_openstack
ls /tmp/pods_openstack/generated/sunbeam/ | grep vault
```

## Grep Patterns

```bash
# Find the vault unit states at collection time
grep -i "vault" <work_dir>/<uuid>/generated/sunbeam/juju_status_openstack.txt

# Find the raft join error on follower nodes
grep "waiting for unseal\|failed to get raft challenge\|Vault is sealed" \
    /tmp/pods_openstack/generated/sunbeam/logs-openstack-vault-0.txt | head -20

# Check vault-1 (leader) unseal progress — exit status 2 = partial unseal
grep "exit status" /tmp/pods_openstack/generated/sunbeam/logs-openstack-vault-1.txt

# Find the timeout in GH Actions log
grep "wait timed out\|CalledProcessError.*vault.*unseal" /tmp/run_<run_id>_failed.log
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
state. This blocked juju-wait for the full 900 seconds. The hook failure occurred ~6 minutes
after enable was invoked (consistent with the first periodic `update-status` call). The exact
exception is only visible in the Juju debug log (Swift artifact) — in this run, Swift upload
also failed, so the root cause within the hook is unknown.

This is **distinct from Pattern A**: Pattern A fails during `sunbeam vault unseal` after the
raft bootstrap stalls. Pattern B fails during `sunbeam enable vault` itself before the unseal
sequence is reached; the vault pod is running (PVC mounts are active) but the hook throws.

**Evidence to look for:**
- GitHub log: `wait timed out after 899.999...s` as output from `sunbeam enable vault`
  (not `sunbeam vault unseal`)
- GitHub log: `vault/0` status `error` with `message='hook failed: "update-status"'`
- Swift `generated/sunbeam/juju_debug_log_openstack.txt`: exception/traceback in the
  `update-status` hook around the timestamp in the error message
- Swift `generated/sunbeam/pods_openstack_logs.tgz`: `logs-openstack-vault-0.txt` for
  vault process errors at or before the hook failure time

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
grep "sunbeam_enable_plugins_all" /tmp/run_<run_id>_failed.log | \
  grep "Payload container not ready\|Not all relations are ready" | head -10

# Find the observability enable timeout
grep "sunbeam_enable_plugins_all" /tmp/run_<run_id>_failed.log | \
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

**Root cause:** `sunbeam enable telemetry` waited for the OpenStack exporter to become active, but `openstack-exporter/0` never considered its `logging` relation ready. The exporter pod repeatedly logged `The relation 'logging' is not ready yet` and `No Loki endpoints available`. Swift relation data shows why: on the exporter's `logging` relation, `opentelemetry-collector/1` and `/2` published Loki `endpoint` and `public_address`, but `opentelemetry-collector/0` published only addresses and never exposed an endpoint. Because the exporter charm treated the logging integration as incomplete, Juju waited the full 900 seconds and `sunbeam enable telemetry` failed even though the telemetry/orchestration pods were otherwise running.

**Evidence to look for:**
- GitHub log: first telemetry enable succeeded earlier via observability (`OpenStack telemetry application enabled.`), but the later explicit `sunbeam enable -m manifest.yaml telemetry` timed out after 900s
- GitHub log / `output.log`: `openstack-exporter` app and `openstack-exporter/0` unit are the only non-active entries, both showing `(logging) integration incomplete`
- `generated/sunbeam/pods_openstack_logs.tgz` → `logs-openstack-openstack-exporter-0.txt`: repeated `logging:269: The relation 'logging' is not ready yet.` and `No Loki endpoints available`
- `generated/sunbeam/show_units_openstack.txt`: relation `269` (`openstack-exporter:logging` ↔ `opentelemetry-collector:receive-loki-logs`) shows `opentelemetry-collector/0` missing `endpoint` / `public_address`, while `/1` and `/2` have both fields
- `generated/sunbeam/pods_openstack_logs.tgz` → `logs-openstack-opentelemetry-collector-0.txt`: receive-loki-logs hooks ran and patched the statefulset successfully (`HTTP/1.1 200 OK`), so the failure is not a crashed collector pod

**First observed:** run 25970616074 (UUID 2797dd8d-b892-43c2-9cde-391cdf3eb610, tor3-sqa-shared_maas dh1_j8_1, branch aipoc, 2026-05-17)

---

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

- **v1.0** (2026-04-02): Initial version — Pattern A from run 23878110731 (UUID fd88452f)
- **v1.1** (2026-04-09): Added Pattern B (vault `update-status` hook failure, run 24181697345 / UUID e15bca12) and Pattern C (observability enable timeout — aodh/gnocchi payload containers not ready, run 24180158005 / UUID e8a7e71d)
- **v1.2** (2026-05-19): Added Pattern D — `sunbeam enable orchestration` timeout caused by `heat/0` `heat-api-cfn` CrashLoopBackOff; pod log shows Pebble failed to bind `:38814` (`address already in use`) in run 25993877865 / UUID 51d9641b-b5aa-4fe4-b19b-e9dce3808aa1
- **v1.3** (2026-05-19): Added Pattern E — `sunbeam enable telemetry` timed out because `openstack-exporter/0` stayed in `(logging) integration incomplete`; `opentelemetry-collector/0` failed to publish Loki endpoint data on relation 269 while `/1` and `/2` did, in run 25970616074 / UUID 2797dd8d-b892-43c2-9cde-391cdf3eb610
