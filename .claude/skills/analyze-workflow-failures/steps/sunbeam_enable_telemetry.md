# Step Knowledge: sunbeam_enable_telemetry

## Step Overview

Enables the Sunbeam telemetry plugin with `sunbeam enable -m manifest.yaml telemetry`. The step deploys and wires OpenStack telemetry/observability components in the Sunbeam OpenStack Juju model, then waits for model readiness.

## Swift Artifacts

Objects stored under `<uuid>/generated/sunbeam/` that are useful for diagnosing failures:

| Path | Description | When to check |
|---|---|---|
| `generated/sunbeam/output.log` | Full FCE/Sunbeam layer log containing the `sunbeam enable telemetry` command, timeout, and embedded Juju status dump | Always — first stop |
| `generated/sunbeam/output.condensed.log` | Smaller layer log for quick failure context | Always |
| `generated/sunbeam/juju_status_openstack.txt` | Final OpenStack model status | Check for waiting/blocked telemetry apps |
| `generated/sunbeam/juju_status_observability.txt` | Final observability model status | Check Loki/Prometheus/Grafana health |
| `generated/sunbeam/show_units_openstack.txt` | Relation data for OpenStack units | Check relation data gaps |
| `generated/sunbeam/juju_debug_log_openstack.txt` | Juju unit/controller log for the OpenStack model | Check hooks and relation events |
| `generated/sunbeam/pods_openstack_logs.tgz` | Collected pod logs from OpenStack namespace | Inspect openstack-exporter/opentelemetry logs |

## Key Log Files

| File | What it contains | When to use |
|---|---|---|
| `output.log` | Failed command, `TimeoutError`, full status object from Sunbeam wait | Confirm the timeout and non-active apps |
| `juju_status_openstack.txt` | Human-readable status of telemetry apps | Identify the app blocking readiness |
| `show_units_openstack.txt` | Relation data keys by related unit | Confirm missing `endpoint` data |
| `pods_openstack_logs.tgz:logs-openstack-openstack-exporter-0.txt` | openstack-exporter charm/workload logs | Confirm logging relation readiness errors |

## Grep Patterns

```bash
# Failure and blocker in Sunbeam output
grep -n "Timed out enabling telemetry\|wait timed out after\|openstack-exporter" files/<uuid>/generated/sunbeam/output.log

# Final model blocker
grep -n "openstack-exporter\|integration incomplete\|waiting" files/<uuid>/generated/sunbeam/juju_status_openstack.txt

# Relation data: compare opentelemetry-collector/0, /1, /2 for endpoint keys
grep -n "relation-id: .*\|endpoint: logging\|opentelemetry-collector/[0-2]\|endpoint: '{\"url\"" files/<uuid>/generated/sunbeam/show_units_openstack.txt

# Exporter charm logging relation symptoms
tar -xOzf files/<uuid>/generated/sunbeam/pods_openstack_logs.tgz generated/sunbeam/logs-openstack-openstack-exporter-0.txt \
  | grep -E "relation 'logging' is not ready|No Loki endpoints|logging-relation"
```

## Known Failure Patterns

### Pattern 1: opentelemetry-collector missing Loki endpoint wedges openstack-exporter

**Symptom:**
```
Timed out enabling telemetry: TimeoutError("wait timed out after 899.999...")
openstack-exporter  waiting  (logging) integration incomplete
The relation 'logging' is not ready yet.
No Loki endpoints available
```

**Root cause:** One `opentelemetry-collector` unit fails to publish the `endpoint` key (Loki push URL) on the `receive-loki-logs` relation. `openstack-exporter` requires complete logging relation data and remains in `waiting: (logging) integration incomplete`, causing Sunbeam's fixed 900s telemetry readiness wait to time out. This can originate during the earlier observability enable phase and only surface when telemetry waits for OpenStack model health.

**Evidence to look for:**
- `generated/sunbeam/output.log`: `sunbeam enable ... telemetry` runs for ~900s and raises `TimeoutError`.
- `generated/sunbeam/juju_status_openstack.txt`: `openstack-exporter/0` is `waiting/idle` with `(logging) integration incomplete`; other apps are active.
- `generated/sunbeam/show_units_openstack.txt`: on `openstack-exporter/0` relation `endpoint: logging` / `related-endpoint: receive-loki-logs`, one `opentelemetry-collector/N` has only address keys while sibling units have `endpoint: '{"url": .../loki/api/v1/push'}'`.
- `generated/sunbeam/pods_openstack_logs.tgz` exporter log: repeated `The relation 'logging' is not ready yet.` and `No Loki endpoints available`, followed by no further successful logging relation evaluation.

## Notes

- This is a Juju charm relation-data/readiness failure, not a MAAS substrate failure.
- On non-MAAS substrates such as `tor3-sqa-testflinger`, no MAAS syslog analysis is expected.
