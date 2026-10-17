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
  <work_dir>/<uuid>/generated/juju_openstack_controller/log.txt

# Find the enable_ha sub-step start and failure
grep -n "enable_ha\|juju bind\|juju spaces\|not in space" \
  <work_dir>/<uuid>/generated/juju_openstack_controller/log.txt

# Show bootstrap completion and enable_ha transition
grep -n "Finished step\|Starting step" \
  <work_dir>/<uuid>/generated/juju_openstack_controller/log.txt

# In GitHub Actions failed log
grep -n "juju bind\|not in space\|enable_ha" <work_dir>/run_<run_id>_failed.log
```

## Known Failure Patterns

### Pattern 1: Controller unit not in expected Juju space (enable_ha bind fails)

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

### Pattern 2: Nova MessagingTimeout — bootstrap VM stuck in BUILD

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

### Pattern 3: Controller unit stuck in pending/allocating during HA scale-up

**Symptom:**
```
Exception: Timed out waiting for juju ha to become ready.
```

The `enable_ha` sub-step completes successfully (add-unit commands return), but the
`wait_for_ready` sub-step times out (~20 minutes) because one of the newly added
controller units never reaches `active/idle`. The affected machine shows:
- `juju-status`: `pending` (never transitions to `started`)
- `unit-status`: `allocating` (never transitions to `idle`)
- `machine-status`: `running` / `ACTIVE` (VM exists in OpenStack)
- **No `hostname`, `dns-name`, `ip-addresses`, or `network-interfaces`** — the agent
  never reported any networking metadata back to the controller

**Root cause:** The OpenStack VM boots and becomes ACTIVE, but the jujud agent inside
the VM never starts, fails to initialize, or cannot reach the controller API. This is
a transient infrastructure issue — not a config or code bug.

**Evidence to look for:**
- `log.txt`: `Exception: Timed out waiting for juju ha to become ready.`
- All juju status polls during the wait window show one machine with no network data
- The affected machine has an `instance-id` and `machine-status: ACTIVE` but zero networking fields
- Other machines are fully operational with complete network metadata
- Total `wait_for_ready` duration: ~20 minutes before timeout

**This is transient** — caused by OpenStack VM agent initialization failure. Retrying
usually succeeds.

**From:** run 28384011894, UUID 66579e9a-c8de-406a-b567-fac874c9ccef, tor3-sqa-sunbeam cluster_2, 2026-06-29.

---

### Pattern 4: Simplestreams image metadata unreachable — bootstrap aborts in seconds

**Symptom:**
```
WARNING Got error requesting "https://ceph-radosgw.<cluster>.<domain>:443/swift/v1/simplestreams/data/streams/v1/index2.sjson": ... EOF   (x3)
WARNING Got error requesting ".../index2.json": ... EOF                                                (x3)
ERROR failed to bootstrap model: failed looking for image metadata in keystone catalog:
  cannot read index data, attempt count exceeded: cannot access URL "..." : EOF
```
Followed by `ERROR controller foundation-openstack not found` and FCE's misleading
`juju controller is bootstrapped but unreachable.` message. `enable_ha` never runs.

**Root cause:** The Juju client on the GitHub runner resolves the `product-streams`
(image-stream) service from the Keystone catalog and fetches simplestreams metadata from
its **public** endpoint, which is the ceph-radosgw VIP on **port 443**. If the runner
cannot complete an HTTPS connection to the cloud's public network on 443, every attempt
returns `EOF` and bootstrap aborts before any VM is created. Note that every other
OpenStack public endpoint uses a distinctive high port (keystone 5000, glance 9292,
nova 8774, …) and keeps working — so the run gets all the way to this step before failing.

**Distinguishing feature vs Patterns 1–3:** total `openstack_bootstrap` duration is
**~10 seconds**. Patterns 1–3 all take minutes. A sub-30 s bootstrap failure means Juju
never reached the point of asking Nova for an instance.

**Evidence to look for:**
- `generated/juju_openstack_controller/log.txt`: exactly 6 `Got error requesting … EOF`
  warnings (3 × `index2.sjson`, 3 × `index2.json`) within ~8 s
- `generated/openstack/juju_status_foundations-maas_openstack.json`: `ceph-radosgw`,
  `hacluster-ceph-radosgw`, `ceph-mon`, `ceph-osd` and `glance-simplestreams-sync` all
  **active** — GSS message confirms *"metadata uploaded to object store"*
- `generated/openstack/log.txt`: grep `"product-streams"` to read the exact public URL Juju used
- Crashdump `<machine>/lxd/<n>/var/log/apache2/access.log` + `error.log` + `haproxy.log`
  on the radosgw containers: **no entries at all** in the failure window → the connection
  never arrived; the cloud is not at fault
- Crashdump syslog on the radosgw containers: `IPaddr2(res_cephrg_*_vip)` shows when/where
  the public VIP was placed and whether it ever failed over (it usually did not)
- Check `no_proxy`/`https_proxy`: on this pipeline `squid.internal:3128` appears only as
  inputs to the weebl/temporal reporting actions, **not** in the `fce build` step env — do
  not blame the proxy without seeing it in the failing step's `env:` block

**This is an environment/network-path issue**, not a transient cloud fault; retrying on the
same lab network will fail the same way until runner→public-network:443 reachability is fixed.

**FCE bug exposed:** `foundationcloudengine/juju_cli.py:150-153` — when `juju bootstrap`
raises, `bootstrap()` calls `is_controller_reachable()`, whose own failure replaces the
actionable error with `controller … not found`. Always read the *first* `ERROR` in
`log.txt`, not the final traceback.

**From:** run 31611708854, UUID 0740dc42-91ce-4638-9908-8f16d18a29b4,
tor3-sqa-shared_maas cluster dh1_j8_2, 2026-08-13.

---

### Pattern 5: `availability zone "nova" not valid` — Nova API DB failures (Sunbeam substrate)

**Symptom:**
```
Launching controller instance(s) on openstack_cloud/RegionOne...
ERROR failed to bootstrap model: cannot start bootstrap instance in availability zone "nova":
  availability zone "nova" not valid
```
Followed by `ERROR controller foundation-openstack not found` and FCE's misleading
`juju controller is bootstrapped but unreachable.` `enable_ha` never runs.

**Root cause:** The cloud's Nova API is unhealthy (nova-api cannot reach its DB →
`HTTP 500 oslo_db.exception.DBConnectionError`). Juju enumerates AZs, picks `nova`, then
re-validates the zone in `StartInstance` with a fresh AZ listing; when that listing comes back
without `nova` the validator returns the *NotValid* branch ("not valid" = zone missing from the
list; "is unavailable" would mean listed but down). No VM is ever requested.

**Distinguishing features:**
- `openstack_bootstrap` fails in ~15 s; ~1 s between `Launching controller instance(s)` and the error.
- No AZ configured anywhere (`model_defaults.yaml`, bootstrap constraints) — not a config error.
- **Key evidence is in the GitHub runner log, not log.txt**: the pre-run *Clean existing
  openstack* step (passes thanks to `|| true`) shows repeated
  `Unexpected API Error ... <class 'oslo_db.exception.DBConnectionError'> (HTTP 500)` and
  servers stuck in `BUILD`/`ERROR`; the post-failure *Clean existing openstack* teardown
  typically fails outright with the same error.

```bash
grep -n "DBConnectionError\|HTTP 500\|not valid" files/run_<run_id>_failed.log
```

- The preceding `juju metadata generate-image ... -s ubuntu` → `series "ubuntu" not valid`
  error is a benign FCE quirk (downgraded to WARNING) — do not mistake it for the cause.

**Variant — `failed to get list of flavour details`:**
```
ERROR failed to get list of flavour details: ... .../openstack-nova/v2.1/flavors/detail
  returned unexpected status: 500 ... DBConnectionError
```
Same underlying nova-api ↔ DB failure, hit on the flavor listing instead of the AZ listing.
Bootstrap fails ~2 s after `Creating Juju controller`; the cleanup then fails on
`servers/detail`. Failures are intermittent (some nova calls still return 200). FCE's
`controller not found` / `bootstrapped but unreachable` (`juju_cli.py:149-153`) masks the real
error. The pre-run *Clean existing openstack* 500s may surface only as `Error: no matches found`,
so grep `DBConnectionError` in `generated/github-runner/run.log` rather than relying on the
`HTTP 500` text.

**Substrate health issue** — retrying fails until the Sunbeam control plane (nova-api ↔ MySQL)
is healthy again.

**From:** run 38568833799, UUID 7bb447b3-2b81-4ac0-ba4c-5a7ef3bfa044, tor3-sqa-sunbeam cluster_4, 2026-10-14.
**From:** run 38568821147, UUID b03d9996 (flavour-details variant).

---

### Pattern 6: Neutron 500 on security-group tag PUT — `creating tags failed` (Sunbeam substrate)

**Symptom:**
```
Launching controller instance(s) on openstack_cloud/RegionOne...
ERROR failed to bootstrap model: cannot start bootstrap instance: cannot set up groups:
  creating tags failed, rolled back security group with id: <sg-id>
caused by: request (https://<host>:443/openstack-neutron/v2.0/security-groups/<sg-id>/tags)
  returned unexpected status: 500; error info: {"NeutronError": {"type": "HTTPInternalServerError", ... "detail": ""}}
```
Followed by FCE's misleading `controller foundation-openstack not found` /
`bootstrapped but unreachable`. `enable_ha` never runs.

**Root cause:** Server-side Neutron fault on the Sunbeam control plane. The SG POST succeeds,
the follow-up tag PUT gets a single 500; Juju does not retry, rolls back the SG and aborts.

**Distinguishing features:** ~6 s from `Launching controller instance(s)` to the error; it is
the only 5xx in the bundle. Pre-run and post-failure *Clean existing openstack* steps list only
the `default` SG with no 500s/`DBConnectionError` → transient, retry should succeed (unlike
Pattern 5, where cleanup steps also hit 500s). Neutron server logs are not in the bundle.

**From:** run 38737173099, UUID 47306134-b95a-41f0-8bd3-6dac4078f0b9, tor3-sqa-sunbeam cluster_4, 2026-10-15.

---

## Notes

- This step is only used on pure-OpenStack substrates (e.g. `ext-sqa-ps6_openstack`). Not present on MAAS-based substrates.
- Bootstrap phase (`openstack_bootstrap`) can take ~5 minutes — a run that fails in `enable_ha` has already spent significant time and resources.
- The `model_defaults.yaml` `network:` field must correspond to a network that is mapped to the Juju space FCE expects (`alpha`). A misconfigured OpenStack space → network mapping in the cluster config will cause this failure on every run.
- No MAAS logs exist for this substrate type.

