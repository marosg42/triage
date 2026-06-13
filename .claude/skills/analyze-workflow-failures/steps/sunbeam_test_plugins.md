# Step Knowledge: sunbeam_test_plugins

## Step Overview

Executes integration and validation tests for Sunbeam OpenStack features and plugins (such as observability, telemetry, and load balancing) in a deployed environment using pytest.

## Swift Artifacts

Objects stored under `<uuid>/` that are useful for diagnosing failures:

| Path | Description | When to check |
|---|---|---|
| `generated/github-runner/run.log` | Full GitHub actions runner log containing pytest session results | Always — first stop to find the failed test and stack trace |
| `generated/sunbeam/sosreport-node1-*.tar.xz` | Node-level sosreport containing system logs and Juju unit status | When a unit is in "lost" or "stopped" state to inspect local agent logs |
| `generated/sunbeam/juju_status_openstack-machines.txt` | Juju status output for the openstack-machines model | To check if unit agents or subordinates are inactive/lost |
| `generated/sunbeam/juju_debug_log_controller.txt` | Target Juju controller logs | To inspect model migrations, agent logins, and credential handshakes |

## Key Log Files (inside sosreport)

| File | What it contains | When to use |
|---|---|---|
| `var/log/juju/unit-<charm>-<unit_id>.log` | Local Juju agent unit log | When the agent is lost or stopped during or after model migration |
| `var/log/syslog` | System-level syslog containing service start/stops, apparmor denials, and snap installation logs | To check system-wide health and snap refresh/install details |

## Grep Patterns

Useful search strings for this step's failure modes:

```bash
# Find the failing pytest tests and corresponding AssertionError
grep -n -C 5 "AssertionError" <work_dir>/generated/github-runner/run.log

# Find Juju agent connection status and migration status
grep -rn "migration failed" <work_dir>/sosreport-node1-extracted/var/log/juju/
```

## Known Failure Patterns

### Pattern 1: k8s/0 Juju Agent Lost due to Model Migration Failure (REAP phase)

**Symptom:**
```
AssertionError: Unit k8s/0 is missing opentelemetry-collector subordinate. Subordinates: []
```

**Root cause:** 
During model migration, Juju migrates machine and unit agents from the source controller to the target controller. If the target controller's model is still initializing or under network convergence, transient connection timeouts can occur. While other unit agents retried and successfully connected a few seconds later, the `unit-k8s-0` migration worker aborted the migration. Its agent config was left unchanged pointing to the source controller, which rejected subsequent connections as unauthorized once migration finalized. This left the unit agent permanently in a "stopped"/"lost" state.

**Evidence to look for:**
- `unit-k8s-0.log`: `CRITICAL juju.worker.migrationminion worker.go:430 migration failed for unit-k8s-0: ... failed to open API to target controller`
- `unit-k8s-0.log`: `ERROR juju.worker.apicaller connect.go:209 Failed to connect to controller: invalid entity name or password (unauthorized access)`
- `juju_status_openstack-machines.txt`: `k8s/0  unknown  lost  0  ...  agent lost, see 'juju show-status-log k8s/0'`

---

_Add more patterns below as they are discovered._

## Notes

- Since Juju controllers are hosted on the Kubernetes cluster managed by the k8s charm, Juju model migrations can suffer from cyclic dependency or transient DNS resolution failures (`cannot resolve "controller-service..."`).
