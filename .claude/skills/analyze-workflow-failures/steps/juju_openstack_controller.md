# Step Knowledge: juju_openstack_controller

## Step Overview

Bootstraps a Juju controller onto an OpenStack cloud, then configures HA and
network space bindings. Runs after the `openstack` layer has deployed the
underlying OpenStack environment. Uses FCE's `juju_openstack_controller` layer,
which executes two sub-steps in sequence: `openstack_bootstrap` and `enable_ha`.

## Swift Artifacts

Objects stored under `<uuid>/generated/juju_openstack_controller/` that are
useful for diagnosing failures:

| Path | Description | When to check |
|---|---|---|
| `generated/juju_openstack_controller/log.txt` | FCE build log for this layer | Always — first stop |
| `generated/juju_openstack_controller/model_defaults.yaml` | Juju model defaults used at bootstrap time (network, proxy, etc.) | Space/network mismatch issues |
| `generated/version_collector_juju_openstack_controller.log` | Versions of snaps/packages on controller at time of failure | Version correlation |

## Key Log Files

This step runs on an OpenStack substrate — **no MAAS logs are present**.
All evidence comes from `log.txt` and the GitHub Actions run log.

## Grep Patterns

```bash
# Find the primary error in the FCE log
grep -n "ERROR\|Exception\|CalledProcessError\|returned non-zero" \
  /tmp/<uuid>/generated/juju_openstack_controller/log.txt

# Find the enable_ha sub-step start and failure
grep -n "enable_ha\|juju bind\|juju spaces\|not in space" \
  /tmp/<uuid>/generated/juju_openstack_controller/log.txt

# Show bootstrap completion and enable_ha transition
grep -n "Finished step\|Starting step" \
  /tmp/<uuid>/generated/juju_openstack_controller/log.txt

# In GitHub Actions failed log
grep -n "juju bind\|not in space\|enable_ha" /tmp/run_<run_id>_failed.log
```

## Known Failure Patterns

### Pattern: Controller unit not in expected Juju space (enable_ha bind fails)

**Symptom:**
```
ERROR unit "controller/0" not in space "alpha"

subprocess.CalledProcessError: Command '['juju', 'bind', '-m',
'foundation-openstack:controller', 'controller', 'alpha']'
returned non-zero exit status 1.
```

**Root cause:** Race condition caused by a slow-starting controller VM. The
`openstack_bootstrap` sub-step completes, but `juju bootstrap` only declares
success once the controller API is reachable — not once the unit's NIC metadata
(and therefore space mapping) is fully propagated internally. If the controller
VM is sluggish (e.g. due to OpenStack compute load), the "Contacting Juju
controller to verify accessibility" phase can take 3+ minutes. By the time
`juju bootstrap` returns, the controller API is reachable but Juju's model has
not yet registered which space `controller/0`'s NIC belongs to. The immediately
subsequent `juju bind` then fails with "not in space alpha" — even though the
VM is on the correct network.

**Comparison with a passing run on the same substrate/cluster:**

| Phase | Passing (0ffc2845) | Failed (00908688) |
|---|---|---|
| Controller IP | 10.141.135.94 | 10.141.135.177 |
| Bootstrap sub-step duration | 2m 23s | 5m 13s |
| "Contacting controller" phase | **4 seconds** | **3m 18s** |
| `juju bind` result | success | `not in space "alpha"` |

Both IPs are in the same `10.141.135.0/24` subnet mapped to the "alpha" space,
and all config (model_defaults.yaml, versions, config hash) is identical. The
only difference is the bootstrap speed — a slow boot leaves unit space metadata
unpopulated at bind time.

**Evidence to look for:**
- `generated/juju_openstack_controller/log.txt`: `Contacting Juju controller at <IP> to verify accessibility...` followed by >60s gap before `Bootstrap complete` — indicates a slow-starting VM
- `generated/juju_openstack_controller/log.txt`: `ERROR unit "controller/0" not in space "alpha"` immediately after `enable_ha` starts
- `generated/juju_openstack_controller/model_defaults.yaml`: `network:` field should still be correct (this is not a config error)
- Total `openstack_bootstrap` duration >4 minutes is a strong predictor of this failure

**This is transient** — caused by OpenStack compute resource pressure at time of
run. Retrying usually succeeds.

**Upstream bug:** https://github.com/juju/juju/issues/22096

**See also:** No dedicated pattern file yet.

---

### Pattern B: Nova MessagingTimeout — bootstrap VM stuck in BUILD

**Symptom:**
```
ERROR failed to bootstrap model: cannot start bootstrap instance: cannot run instance:
  with fault "MessagingTimeout"

subprocess.CalledProcessError: Command '['juju', 'bootstrap', ...]'
returned non-zero exit status 1.
```
Followed immediately by:
```
ERROR controller foundation-openstack not found
```
The `enable_ha` sub-step is never reached.

**Root cause:** The OpenStack Nova messaging layer (RabbitMQ/AMQP) timed out while
trying to schedule or launch the controller VM. Nova created the instance and its status
moved to `BUILD`, but the `nova-compute` agent responsible for actually running the VM
never acknowledged the request. After 6 retries (10 s each, ~60 s total), OpenStack
returned a `MessagingTimeout` fault. This is unrelated to Juju's `bootstrap-timeout`
setting — the fault originates from OpenStack's internal messaging layer.

**Evidence to look for:**
- `generated/juju_openstack_controller/log.txt`: 6× `has status BUILD, wait 10 seconds before retry` for the same instance UUID
- `log.txt`: `ERROR failed to bootstrap model: cannot start bootstrap instance: cannot run instance: with fault "MessagingTimeout"`
- Total `openstack_bootstrap` duration ~1m 45s (fast failure — the VM never advanced past BUILD)
- `model_defaults.yaml`: `network:` field correct — this is **not** a config error

**This is transient** — caused by a flaky Nova compute agent or RabbitMQ pressure at
the time of the run. Retrying usually succeeds. Investigate compute node health and
RabbitMQ consumer lag around the failure window if recurrent.

**From:** run 23748868831, UUID 1a687feb-4355-4691-964c-07f9717aecaa, tor3-sqa-sunbeam cluster_4, 2026-03-30.

---

_Add more patterns below as they are discovered._

## Notes

- This step is only used on pure-OpenStack substrates (e.g. `ext-sqa-ps6_openstack`). Not present on MAAS-based substrates.
- Bootstrap phase (`openstack_bootstrap`) can take ~5 minutes — a run that fails in `enable_ha` has already spent significant time and resources.
- The `model_defaults.yaml` `network:` field must correspond to a network that is mapped to the Juju space FCE expects (`alpha`). A misconfigured OpenStack space → network mapping in the cluster config will cause this failure on every run.
- No MAAS logs exist for this substrate type.

## Version History

- **v1.0** (2026-03-26): Initial version — controller not in space "alpha" pattern from run 23573425818 (UUID 00908688, ext-sqa-ps6_openstack cluster_1)
- **v1.2** (2026-03-30): Added Pattern B — Nova `MessagingTimeout` fault during `openstack_bootstrap`; VM stuck in BUILD for 6 retries, never reached `enable_ha`; from run 23748868831 (UUID 1a687feb, tor3-sqa-sunbeam cluster_4).
