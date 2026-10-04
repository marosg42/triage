# Step Knowledge: sunbeam_enable_observability

## Step Overview

Enables embedded Sunbeam observability. The step runs `sunbeam enable -m manifest.yaml observability embedded` on the Sunbeam bootstrap node, deploys telemetry/OpenTelemetry components in the OpenStack model and a COS stack in the observability model, wires cross-model relations, then waits for the collector app to become active.

## Swift Artifacts

| Path | Description | When to check |
|---|---|---|
| `generated/sunbeam/output.log` | Main FCE/Sunbeam command log, including the failed `sunbeam enable` command and embedded Juju status on timeout | Always |
| `generated/sunbeam/juju_status_openstack.txt` | Final OpenStack model status | Check blocked/waiting OpenTelemetry or telemetry apps |
| `generated/sunbeam/show_units_openstack.txt` | Per-unit relation data | Verify missing/present collector CMR endpoints |
| `generated/sunbeam/juju_debug_log_openstack.txt` | OpenStack model Juju debug log | Check hook execution and relation processing |
| `generated/sunbeam/juju_status_observability.txt` | Final COS/observability model status | Verify Loki/Prometheus/Grafana health |
| `generated/sunbeam/pods_openstack_logs.tgz` | OpenStack pod logs, including collector charm/workload logs | Verify collector pod health and hook inventory |
| `generated/sunbeam/pods_observability_logs.tgz` | COS pod logs | Check Loki/Prometheus/Grafana errors |
| `generated/lastlines.txt` | Tail of runner/Sunbeam logs | Quick failure signature |

## Key Log Files

| File | What it contains | When to use |
|---|---|---|
| `generated/sunbeam/output.log` | Command, timeout, stack trace, embedded `ModelStatus` | Primary source |
| `generated/sunbeam/juju_status_openstack.txt` | Concise final app/unit status | Confirm blocked app/unit |
| `generated/sunbeam/show_units_openstack.txt` | Unit relation inventory and remote app relation lists | Diagnose missing CMR relations |
| `pods_openstack_logs.tgz` → `logs-openstack-opentelemetry-collector-*.txt` | Collector charm and workload logs | Distinguish crashed pod from relation/status defect |

## Grep Patterns

```bash
# Primary failure
grep -n "Timed out enabling observability\|sunbeam enable -m manifest.yaml observability embedded\|wait timed out after 7199" generated/sunbeam/output.log

# Collector blocked state
grep -n "opentelemetry-collector\|cloud-config.*send-loki-logs\|send-remote-write" generated/sunbeam/juju_status_openstack.txt generated/sunbeam/output.log

# Relation inventory for send-side CMRs
grep -n "^opentelemetry-collector/\|endpoint: send-loki-logs\|endpoint: send-remote-write\|endpoint: receive-loki-logs\|endpoint: metrics-endpoint" generated/sunbeam/show_units_openstack.txt

# Collector pod health
for f in generated/sunbeam/logs-openstack-opentelemetry-collector-*.txt; do grep -n "Everything is ready\|HTTP/1.1 200 OK\|send-loki-logs\|send-remote-write" "$f"; done
```

## Known Failure Patterns

### Pattern 1: OpenTelemetry collector blocked because send-side COS CMR relations are missing

**Symptom:**
```
Timed out enabling observability.embedded: TimeoutError('wait timed out after 7199...s')
opentelemetry-collector blocked: "['cloud-config']|['send-loki-logs'] for receive-loki-logs; ['cloud-config']|['send-remote-write'] for metrics-endpoint"
```

**Root cause:** `sunbeam enable observability embedded` creates incoming OpenStack telemetry relations to `opentelemetry-collector` and outgoing CMR relations from the collector to COS offers. If the outgoing `send-loki-logs` and `send-remote-write` relations are absent or dropped while incoming `receive-loki-logs`/`metrics-endpoint` relations exist, the collector charm remains blocked and the fixed 7200-second enable wait expires. The collector pods may still be running and healthy; the key evidence is the relation inventory and SAAS relation list.

**Evidence to look for:**
- `output.log`: timeout after 7199s and embedded `ModelStatus` with `opentelemetry-collector` blocked.
- `juju_status_openstack.txt`: `opentelemetry-collector` app/units blocked with missing `cloud-config`/send-side relation message.
- `show_units_openstack.txt`: `opentelemetry-collector/*` has `receive-loki-logs`/`metrics-endpoint` but no `send-loki-logs` or `send-remote-write`; compare with `opentelemetry-collector-infra`.
- `pods_openstack_logs.tgz`: collector workload reaches `Everything is ready` and Kubernetes API PATCH/GET calls succeed, ruling out a crashed workload.

**See also:** `steps/sunbeam_enable_plugins_all.md` observability/telemetry patterns for related OpenTelemetry collector relation failures.

### Pattern 2: Telemetry wait false-negative while Gnocchi is still converging

**Symptom:**
```
Timed out enabling telemetry: TimeoutError("wait timed out after 899.999...s")
gnocchi waiting: "(container:gnocchi-api) pebble not ready"
gnocchi/1 waiting: "(container:gnocchi-api) pebble not ready"
```

**Root cause:** `sunbeam enable observability embedded` runs the telemetry enable path with a fixed ~900s readiness wait. On slower CAAS/testflinger runs, Gnocchi can be the final telemetry app to converge: its workload containers/pebble may start near the end of the 900s window and the Pebble `level=ready` check may pass shortly after the command has already timed out. If the final collected Juju status shows Gnocchi active and pod logs show no restarts or application errors, this is a false-negative convergence timeout rather than a product failure.

**Evidence to look for:**
- `output.log`: timeout after `899.999...s` with embedded status showing only `gnocchi`/`gnocchi/<n>` waiting on `(container:gnocchi-api) pebble not ready`.
- `kubectl_get_pod_detailed.txt`: the affected `gnocchi-<n>` pod was created much earlier, but the `gnocchi-api` / `gnocchi-metricd` containers only started near the timeout.
- `pods_openstack_logs.tgz` → `logs-openstack-gnocchi-<n>.txt`: Pebble daemon and `wsgi-gnocchi-api` start late; `/healthcheck` returns HTTP 200 and `GET /v1/checks?level=ready` eventually returns 200 with no crash loop.
- Final `juju_status_openstack.txt`: Gnocchi is active/idle by log collection time, confirming post-timeout convergence.

### Pattern 3: Telemetry wait timeout caused by transient Charmhub OCI image pull failures

**Symptom:**
```
Timed out enabling telemetry: TimeoutError("wait timed out after 899.999...s")
gnocchi/1 error: "OCI image pull error: ... registry.jujucharms.com/... failed to copy: read tcp ...:443: read: connection reset by peer ... insufficient_scope: authorization failed"
aodh waiting: "(workload) Payload container not ready"
```

**Root cause:** `sunbeam enable observability embedded` deploys telemetry charms with large OCI workload images. If worker nodes hit transient egress/CDN failures while pulling images from `registry.jujucharms.com`, kubelet retries can exceed Sunbeam's fixed ~900s telemetry wait. The same digest may pull successfully on sibling units/nodes or shortly after the timeout, so this is a substrate/network flake rather than a missing image or charm auth defect.

**Evidence to look for:**
- `output.log`: timeout after `899.999...s` plus `OCI image pull error` messages showing `connection reset by peer` and/or `insufficient_scope: authorization failed`.
- `kubectl_get_pod_detailed.txt`: affected pods have late container `startedAt`/`Ready` transitions after the command timeout, with zero restarts once running.
- Final `juju_status_openstack.txt`: telemetry apps later become active/idle, confirming self-recovery after the failed wait.
- Compare sibling units: the same image digest succeeds elsewhere, ruling out a genuinely missing registry object or invalid credentials.

## Notes

- `tor3-sqa-testflinger` has no MAAS infrastructure log archive; use Sunbeam/Juju/Kubernetes artifacts.
- Do not attribute this timeout to generic pod health if `kubectl_get_pod.txt` shows collector pods running and charm logs show successful statefulset patches.
