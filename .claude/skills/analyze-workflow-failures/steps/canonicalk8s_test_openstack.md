# Step Knowledge: canonicalk8s_test_openstack

## Step Overview

Runs k8s-operator `tests/integration/test_openstack.py`
(`tox -e integration-k8s -- -k test_openstack.py --no-deploy --model canonicalk8s-openstack`)
against the **same model** that `canonicalk8s_test` (which runs `test_k8s.py`, including
destructive unit-removal tests) has just used. Any damage that `canonicalk8s_test` leaves
behind shows up here.

Tests: `test_cloud_provider`, `test_provider_ids`, `test_cinder_pv`,
`test_k8s_api_load_balancer`, `test_extra_sans`. Only `test_extra_sans` uses the
whole-model, error-aware `wait_active()` (`juju.wait(error=any_error)` with no app filter).

The substrate is usually `ext-sqa-ps6_openstack`, which has no MAAS logs.

## Swift Artifacts

| Path | Use |
|---|---|
| `generated/canonicalk8s_test_openstack/log/3-commands[0].log` | pytest log + WaitError status dump |
| `generated/canonicalk8s_test/log/3-commands[0].log` | Previous step; find when units went to error |
| `generated/canonicalk8s-openstack/juju-crashdump-*.tar.gz` | `debug_log.txt` (unit tracebacks), `juju_status.txt` |

## Grep Patterns

The crashdump filename contains a `:`. Use `tar --force-local -xzf ./juju-crashdump-*.tar.gz -C <dir>`, otherwise tar treats it as a remote host.

```bash
T1='generated/canonicalk8s_test_openstack/log/3-commands[0].log'
T0='generated/canonicalk8s_test/log/3-commands[0].log'
grep -n "FAILED\|WaitError\|current='error'\|RemoteDisconnected\|ProtocolError" "$T1"
grep -n "Remove unit\|leader = True\|openstack-cloud-controller/0'\].workload_status" "$T0"
grep -n "Uncaught exception\|connect_tcp.started\|hook \"update-status\".*failed\|validation errors for Data" debug_log.txt
```

## Known Failure Patterns

### Pattern 1: openstack-cloud-controller/0 stuck in `hook failed: "update-status"` after leader removal

**Symptom:**
```
FAILED tests/integration/test_openstack.py::test_extra_sans
  - jubilant._juju.WaitError: error function wait_active.<locals>.<lambda> returned true
'openstack-cloud-controller/0': current='error', message='hook failed: "update-status"', since=<time of previous step's leader removal>
```
The test fails about 1 s after `wait_active` starts, and the other openstack tests pass.

**Root cause:**
- `test_remove_leader_control_plane` in the previous step (`canonicalk8s_test`) removes the k8s leader.
- The k8s charm had published that leader's own node IP as the `kube-control` `api-endpoints`, not the Octavia LB VIP.
- During the relation rotation, OCC reports `9 validation errors for Data`. Its `update-status` hook then runs `_check_node_provider_ids` → `client.list(Node)`, which hits `httpx.ConnectError [Errno 111]` against the removed IP. The exception is not caught.
- The unit stays in error, and Juju runs no further hooks while it waits for error resolution. This happens even after the new leader publishes a valid endpoint.

Removing a non-leader does not trigger this.

**Variants and extra evidence:**
- The errno can be `[Errno 113] No route to host` instead of `[Errno 111]`. You see 113 once the removed leader's VM is gone. Check that every OCC `connect_tcp.started host=` points at the removed leader's `public_address` from the `canonicalk8s_test` log.
- In the previous step's log, the runner status diff shows OCC changing `active → error` within about 1–2 minutes of `Remove unit k8s/<leader>`. `test_remove_leader_control_plane` still PASSES because its wait covers only the k8s apps.
- OCC unit lines in `debug_log.txt` can be time-shifted. They may appear just after a `"log-sender" manifold worker stopped: restart immediately` line, a few minutes after the controller-side status `since`. Treat the `since` value and the runner's status diff as the authoritative failure time.
- `cinder-csi/0` can keep hitting the same removed IP (`Failed to load in cluster CRDs`, `[Errno 113]`) while still reporting `active`.
- `automatically-retry-hooks: true` does not clear the error. Retries hit the same stale endpoint.
- The crashdump machine directories (e.g. `6/`) are often empty because `juju ssh --proxy` addon commands time out. The OCC kubeconfig cannot be inspected from the bundle.
- Also seen on the `tor3-sqa-sunbeam` and `tor3-sqa-shared_maas` substrates, not only `ext-sqa-ps6_openstack`.
- The dead endpoint can be the removed leader's **internal/ens3 IP** (`192.168.x` / `172.16.0.x`) rather than its `public_address`. Map it via `.machines['N'].ip_addresses` / `network_interfaces` in the status dump, the cert SANs log line, or the integrator's `Creating lb member <ip>:6443`. Find the LB VIP/FIP via `Created FIP for <fip> -> <vip>`.
- The OCC error can appear up to ~2m45s after `Remove unit`. On `tor3-sqa-sunbeam`, one run saw ~5m39s, with an intermediate `blocked "Missing required certificates"` before the `error`.
- The logged `ManifestClientError('Failed to load in cluster CRDs', ConnectError(...))` and the `Cannot connect to the api endpoint, marking ... as missing` line are handled and do not fail the hook. The uncaught exception that fails the hook is always `charm.py _update_status → _check_node_provider_ids → client.list(Node)`.
- **Trigger attribution:** the removals come from the previous step's tests (`grep "Remove unit" generated/canonicalk8s_test/log/3-commands[0].log`), not from units being "replaced during deployment". A dying k8sd socket on the removed node is a symptom, not the cause.
- `juju-crashdump` teardown can hang 35m–1h45m; this is cosmetic.
- **"45-min wait timeout" / "removed during the deploy" misreadings (happy-bot):** pytest's total time, for example `0:45:03`, is mostly crashdump teardown. `test_extra_sans` fails within seconds because the error callback fires immediately. Check the `juju-crashdump started/finished` timestamps. happy-bot may also say the stale IP belonged to units "replaced during the deploy". Always cite the `Remove unit k8s/<leader>` line in `canonicalk8s_test`. From live data, the first failure is `ConnectionRefusedError(111)` and changes to `113` about 2–3 min later. The stale file is `/srv/openstack-cloud-controller/0/kubeconfig`, which `cinder-csi` also has.
- **"Transient cluster formation" misattribution (happy-bot):** the replacement leader's `leader-elected` (~3–4 min later) and the test's own `juju add-unit k8s` (~8 min later) can make the event look like a cluster that was still forming. To show the endpoint is stale rather than transiently down, grep the hourly auto-retries (`hook "update-status".*failed` at about +1h, +2h, ...). They still log `connect_tcp.started host='<removed-leader IP>'` → `OSError(113, 'No route to host')`.
- **IP reuse red herring:** the test's re-added k8s unit (e.g. `k8s/4`) can get the removed leader's *public* address (e.g. `10.243.39.132`). Its internal ens3 IP is new, though, so the stale internal endpoint (e.g. `192.168.1.226`) stays dead. Always map the OCC `connect_tcp` host to a machine through `.machines['N'].network_interfaces['ens3']`.
- **"Old units k8s/0, k8s/1 replaced during deploy" (happy-bot):** happy-bot can lump the non-leader and leader removals together. Check each removed unit's ens3 IP: only the leader's matches the OCC host, and OCC stays `active` after the earlier non-leader removal.
- **Archived debug_log can have zero OCC lines for the failure window.** In one case nothing was logged from 10:36:25 to 11:39, so the first visible traceback is the +1h auto-retry. happy-bot's live `juju show-unit` showed that the new leader advertises **its own** node IP (`api-endpoints ["https://<k8s/2 ens3>:6443"]`), not the LB VIP. This supports the warning that `juju resolve` alone may not be a durable fix. happy-bot may also say `wait_active` "timed out after ~38 min". That time is crashdump teardown, so check `juju-crashdump started` right after the test's first `wait: status changed`.
- **Proving the pin predates the removal:** the earliest OCC `connect_tcp.started host=` lines, from about 1–2 min after kube-control is first joined (well before `canonicalk8s_test` starts), already target the future-removed leader's ens3 IP and never the LB VIP. Use `grep -a "unit-openstack-cloud-controller-0.*connect_tcp.started" debug_log.txt | head`. This shows the endpoint was fixed at deploy time and did not drift during the removal tests. On `tor3-sqa-sunbeam` (Juju 3.6.29, k8s 1.36/candidate), one run reached `blocked "Missing required certificates"` at about +4m, then `error` at about +5m after `Remove unit`. Crashdump took 1h29m.
- **Endpoint was stale-by-design from deploy time (tor3-sqa-sunbeam):** every OCC `connect_tcp.started` from its *first* hook, about 70 min before the removal, already targeted the leader's ens3 IP (`192.168.2.231`), not the LB VIP. So the bug is in the leader's endpoint publishing, not in a rotation race. Prove it with `grep -a "unit-openstack-cloud-controller-0.*connect_tcp.started" debug_log.txt | head`. In that run the error appeared at +3m15s, and the first auto-retry came at about +43m (10:39), not +1h. Crashdump teardown took about 1h31m.

**Verdict:** charm robustness (OCC and the k8s endpoint publishing) plus pipeline test isolation. This is not a substrate problem.

**Remediation:** `juju resolve openstack-cloud-controller/0` may not be enough, because the stale endpoint persists — the new leader also publishes its own node IP as `api-endpoints` rather than the LB VIP. Re-integrate `kube-control` or redeploy OCC. Pipeline: resolve or check for error units after `canonicalk8s_test`.

**Masking note:** pytest runs with "stop after 1 failure". If an earlier test such as `test_cinder_pv` fails first, `test_extra_sans` never runs, but Pattern 1 can still be present. Always grep the status dump for `current='error'` before you conclude the visible failure is the only problem.

---

### Pattern 2: `test_cinder_pv` → `ProtocolError(RemoteDisconnected)` on the reader-pod POST

**Symptom:**
```
FAILED tests/integration/test_openstack.py::test_cinder_pv - urllib3.exceptions.ProtocolError:
  ('Connection aborted.', RemoteDisconnected('Remote end closed connection without response'))
  ... storage.py line 78, in exec_storage_class: create_from_yaml(api_client, PV_READER_POD)
```
The writer pod reaches `Succeeded`, so Cinder provisioning and attach work. Writer polling runs through `juju exec ... kubectl` and leaves the Python `api_client` idle for about 2 min. The next `api_client` POST then gets EOF with no response.

**What is known:**
- The test kubeconfig (`get-kubeconfig` with no `server`) points at the Octavia API LB VIP (`Requesting kubeconfig for server=https://<vip>:443` in `debug_log.txt`).
- POST is non-idempotent. The default urllib3 `Retry` does not retry it, so one dropped connection fails the test.
- The bundle can't show which LB member or apiserver closed the socket. Crashdump machine dirs are empty, there are no amphora logs, and `openstack-integrator` logs often have `log-sender` gaps that hide member add/remove after the `canonicalk8s_test` unit churn. Check `grep -a "Added member\|Creating lb member" debug_log.txt`. If no member changes appear after the control-plane removals, pool reconciliation is unverifiable.
- Seen together with Pattern 1 (OCC in error since the leader removal). Even if this flake hadn't happened, `test_extra_sans` would likely have failed.

**Verdict:** transient API-path connection drop combined with a lack of retries in the test. Confidence about the exact dropping element is low unless LB/apiserver logs are available.

**Remediation:** retry pod creation on `ProtocolError` in `storage.py`, or use a fresh `ApiClient` or `Retry(allowed_methods=None)`. In the pipeline, verify Octavia pool members match the live `k8s` units after `canonicalk8s_test`.

---

_Add more patterns below as they are discovered._
