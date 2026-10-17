# Step Knowledge: juju_sunbeam_controller

## Step Overview

This step bootstraps a separate Juju controller on top of an already-deployed Sunbeam
OpenStack cloud. It first validates the cloud with the `openstack` CLI from the runner,
then runs the Terraform module in `terraform/juju_sunbeam_controller/`, which calls
`juju bootstrap openstack/RegionOne juju-sunbeam-controller` using
`generated/sunbeam/admin.openrc`.

Failures here are usually runner-to-OpenStack or bootstrap-VM-to-OpenStack connectivity
issues, rather than MAAS provisioning problems.

Entry points:
- `.github/actions/builds/juju_sunbeam_controller/action.yml`
- `terraform/juju_sunbeam_controller/main.tf`

## Swift Artifacts

| Path | Description | When to check |
|---|---|---|
| `generated/github-runner/jobs.json` | Step names, timings, conclusions | Identify the failed step |
| `generated/github-runner/run.log` | Full GitHub Actions runner log | Primary source for the failure |
| `generated/lastlines.txt` | Tail of the combined logs | Quick triage |
| `generated/sunbeam/admin.openrc` | OpenStack auth URL and interface used by the step | Verify which endpoint Juju used |
| `generated/sunbeam/kubectl_get_svc.txt` | Service and LoadBalancer IPs | Confirm the public API VIP |
| `generated/sunbeam/validation_quick_*.log` | Recent API checks against the Sunbeam cloud | Prove the cloud was reachable from the runner |
| `generated/sunbeam/validation_smoke_*.log` | Smoke validation API calls | Additional runner-side reachability evidence |
| `generated/sunbeam/validation_refstack_*.log` | Refstack validation API calls | Strongest “cloud still healthy” evidence near failure time |
| `generated/sunbeam/pods_controller-sunbeam-controller_logs.tgz` | Existing controller pod logs | Secondary evidence that the in-cluster controller stayed healthy |

## Key Log Files

| File | What it contains | When to use |
|---|---|---|
| `generated/github-runner/run.log` | Terraform output, Juju bootstrap output, serial console dump from the bootstrap VM | Always — main failure evidence |
| `generated/sunbeam/admin.openrc` | `OS_AUTH_URL`, `OS_INTERFACE`, region, CA path | Verify endpoint selection |
| `generated/sunbeam/kubectl_get_svc.txt` | `traefik-public-lb` external IP | Map the auth URL IP back to the Sunbeam public VIP |
| `generated/sunbeam/validation_refstack_*.log` | API calls from the runner shortly before failure | Distinguish runner reachability from guest reachability |
| `generated/sunbeam/logs-controller-sunbeam-controller-controller-0.txt` | Existing controller pod health | Rule out a simultaneous controller crash |

## Grep Patterns

```bash
# Primary bootstrap failure
grep -n "failed to bootstrap model\|requesting token\|dial tcp .*:443: i/o timeout" generated/github-runner/run.log

# Find the Juju bootstrap command and selected network
grep -n "juju --verbose bootstrap\|openstack network show demo-network\|allocate-public-ip=true" generated/github-runner/run.log

# Prove the runner could still reach the public API shortly before failure
grep -n "10.241.36.134/openstack-keystone" generated/sunbeam/validation_*.log

# Extract bootstrap VM networking clues from the serial console dump
grep -n "192.168.122.34\|DHCPv4 address\|Attempting to connect to 10\.243\." generated/github-runner/run.log
```

## Known Failure Patterns

### Pattern 1: Bootstrap VM cannot reach the Sunbeam public Keystone VIP

> **⚠️ Before concluding "guest routing gap", check Pattern 3 (MetalLB speaker wedge).** The
> guest→public-VIP path normally works (this step succeeds in most runs). An `i/o timeout` to
> the public VIP during bootstrap is frequently caused by the MetalLB L2 speakers wedging and
> ceasing to answer ARP for the VIP — a transient infra fault — **not** a permanent inability
> of tenant instances to route to the public API. Always inspect the MetalLB pod-log timestamps
> (Pattern 3) before attributing this to guest networking.

**Symptom:**
```
ERROR authentication failed.: authentication failed
caused by: requesting token: failed executing the request https://10.241.36.134/openstack-keystone/v3/auth/tokens
caused by: Post "https://10.241.36.134/openstack-keystone/v3/auth/tokens": dial tcp 10.241.36.134:443: i/o timeout
ERROR failed to bootstrap model: subprocess encountered error code 1
```

**Root cause:** The Terraform module sources `generated/sunbeam/admin.openrc`, which points
Juju at the Sunbeam **public** Keystone endpoint (`https://10.241.36.134/openstack-keystone/v3`).
The runner can reach that VIP and complete `openstack image list`, quota changes, and recent
Tempest validations, so the cloud itself is still healthy. The failure happens later inside the
newly-created Juju bootstrap VM: the VM boots, gets its tenant-network address (`192.168.122.34`),
Juju starts, and then `jujud` times out trying to POST to the Keystone public VIP. In other words,
the bootstrap VM lacked working network reachability to the public API endpoint that the runner was
using successfully.

**Evidence to look for:**
- `generated/github-runner/run.log`: successful runner-side `openstack` commands in the `Workarounds` step, followed by the bootstrap failure.
- `generated/sunbeam/validation_refstack_*.log`: `200 GET` responses from `https://10.241.36.134/...` within minutes of the failure.
- `generated/github-runner/run.log`: serial console / cloud-init output showing the bootstrap VM on `192.168.122.34`, plus the later `dial tcp 10.241.36.134:443: i/o timeout` from `jujud`.

**Recommendations:**
1. Add a bootstrap-VM connectivity preflight that curls or opens `OS_AUTH_URL` from inside the newly created instance before starting Juju agent installation.
2. Ensure instances on `demo-network` can route to the Sunbeam public API VIP, or use an endpoint/interface that is reachable from tenant instances instead of blindly reusing the runner’s public `admin.openrc`.
3. Improve failure logging by dumping the guest route table / `ip addr` / `curl -vk $OS_AUTH_URL` output when bootstrap authentication times out.

---

## Notes

- On `tor3-sqa-shared_maas`, there are no MAAS infrastructure logs to inspect.
- `generated/sunbeam/kubectl_get_pod.txt` showed all Keystone and Traefik pods running at collection time; this points away from a control-plane outage.
- The important distinction is **runner reachability succeeded, guest reachability failed**.
- Runner logs may also show an `sshuttle` process on the GitHub runner for the Sunbeam API VIPs; treat that as further proof that runner-side access does not guarantee identical reachability from the newly booted Juju controller VM.


### Pattern 2: sshuttle tunnel dies but daemon hangs, causing bare openstack commands to fail

**Symptom:**
```
Failed to discover available identity versions when contacting https://10.241.38.135/openstack-keystone/v3. Attempting to parse version from URL.
Could not find versioned identity endpoints when attempting to authenticate. Please check that your auth_url is correct. Unable to establish connection to https://10.241.38.135/openstack-keystone/v3: ('Connection aborted.', ConnectionResetError(104, 'Connection reset by peer'))
##[error]Process completed with exit code 1.
```
This happens immediately at the start of the `juju_sunbeam_controller` step.

**Root cause:** The `sshuttle` daemon running on the GitHub runner loses its underlying SSH connection to the MAAS node (often hours earlier). However, the Python `sshuttle` daemon process does not exit, so it keeps the `iptables` REDIRECT rules in place. When the runner tries to reach the Sunbeam VIP, the traffic is sent to the hung `sshuttle` daemon, which immediately rejects it with a TCP RST.
Because `.github/actions/builds/juju_sunbeam_controller/action.yml` contains a bare `openstack image list` command *before* its robust retry loop, `set -e` causes the script to abort instantly upon encountering the `ConnectionResetError`.

**Evidence to look for:**
- `generated/github-runner/run.log`: The step fails instantly on `openstack image list` with `ConnectionResetError`.
- `generated/sshuttle_metallb_range.txt`: Shows `sshuttle` is still `UP`.
- `var/log/auth.log` (on the node `sshuttle` was routing through, usually node 31): Look for the original `sshuttle` SSH connection (established around `18:34`) closing hours before the failure (e.g. `pam_unix(sshd:session): session closed for user ubuntu`).

**Recommendations:**
1. Update the MetalLB sshuttle monitor in `sunbeam_prepare_env/action.yml` to actively probe
   through the tunnel instead of just checking PID existence. The virtual_maas tunnel monitor
   does this correctly — it runs `ssh 10.241.144.2 "date;hostname"` every 60 seconds and
   writes the output (including SSH errors) to `sshtest.txt`, so broken connections show up
   naturally. The MetalLB monitor should do the same: replace `ps -p "$SSHUTTLE_PID"` with
   `ssh -o ConnectTimeout=5 <gateway_node> true` and record the result in
   `sshuttle_metallb_range.txt`. The gateway node is reachable from the moment sshuttle
   starts; no OpenStack dependency.

---

### Pattern 3: MetalLB speaker wedge stops serving the public Keystone VIP → bootstrap i/o timeout

**Symptom:** Identical to Pattern 1 — during `juju bootstrap`, `jujud` on the freshly-created
controller VM times out authenticating to the public Keystone VIP:
```
caused by: Post "https://<public-VIP>/openstack-keystone/v3/auth/tokens": dial tcp <public-VIP>:443: i/o timeout
ERROR failed to bootstrap model: subprocess encountered error code 1
```
Often preceded (in the serial-console dump) by
`WARNING juju.provider.openstack networking.go:370 could not resolve internal network id ... failed to get network detail`.

**Root cause:** The public VIP (`openstack/traefik-public-lb`) is announced by **MetalLB in L2
mode**. When the MetalLB `speaker` processes **wedge** (stay `Running` but their reconcile loop
dies and they stop answering ARP for the VIP), the VIP becomes unreachable after the ARP cache
expires — while Keystone itself stays healthy on its ClusterIP path. The bootstrap VM booted
fine and **cloud-init succeeded** (`Datasource DataSourceOpenStackLocal [net,ver=2]`), so this is
**not** a datasource/metadata problem and **not** a permanent guest routing gap. See
`patterns/metallb-speaker-wedge.md` for the full diagnostic.

**Evidence to look for:**
- `generated/sunbeam/pods_metallb-system_logs.tgz` → all `logs-metallb-system-metallb-speaker-*`
  stop logging at the **same time, well before the failure**, ending mid-normal `ServiceReconciler`
  with no error/panic/SIGTERM; log files small (not rotated).
- `generated/sunbeam/kubectl_get_pod.txt`: metallb pods `Running 1/1`, **0 restarts**, multi-hour
  age → hung in-process, not a crash/reschedule.
- `generated/sunbeam/pods_openstack_logs.tgz`: keystone/nova/neutron/traefik-public logs are
  **current** (collected minutes after the failure) → the anomaly is isolated to MetalLB, not a
  cluster-wide / log-pipeline outage.
- Serial console in `run.log`: `Cloud-init ... finished ... Datasource DataSourceOpenStackLocal`
  (datasource OK) followed by the jujud Keystone-VIP `i/o timeout`.

**Recommendations:**
1. Investigate why the MetalLB speakers wedged and add liveness/readiness probes so a stalled
   speaker is killed/rescheduled instead of silently staying `Running` and dropping its VIPs.
2. Make the step resilient: pre-flight `openstack token issue` (or TCP probe) against the
   credential `auth_url` from the controller's network and **retry bootstrap on transient
   `i/o timeout`**.

**Observed in:**
- Run 30214148446 / UUID `054a43a2-9a10-449a-b216-99ff66a3b8d7`: speakers silent from 02:58:48,
  failure at 03:17:40 (`dial tcp 10.241.38.132:443: i/o timeout`); openstack pods logged to
  03:52–03:55.
- Run 30196037796 / UUID `4f55d457-b9f9-4887-a8b6-31f0233d8064`: speakers silent from 20:46:03,
  failure at 21:09:35 (`dial tcp 10.241.38.134:443: i/o timeout`); openstack pods logged to
  21:45–21:48.

---

### Pattern 4: traefik-k8s upgrade leaves one internal traefik replica without a certificate → intermittent Nova HTTP 500

> **Distinct from Patterns 1 and 3.** Those produce a network-level `i/o timeout` from the
> bootstrap VM. This one produces an **HTTP 500 from the Nova API**, with
> `keystoneauth1.exceptions.connection.SSLError` named in the fault payload. If you see
> `returned unexpected status: 500` rather than `i/o timeout`, go straight here.

**Symptom:**
```
null_resource.juju_controller (local-exec): ERROR failed to bootstrap model:
  cannot start bootstrap instance: cannot run instance:
  request (https://<public-VIP>:443/openstack-nova/v2.1/servers[/<id>])
  returned unexpected status: 500; error info:
  {"computeFault": {"code": 500, "message": "Unexpected API Error. ...
   <class 'keystoneauth1.exceptions.connection.SSLError'>"}}
```
Inside nova-api:
```
ERROR nova.api.openstack Caught error: SSL exception connecting to
  https://<internal-VIP>/openstack-keystone/v3/auth/tokens: ...
  (Caused by SSLError(SSLCertVerificationError(1,
    '[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed certificate')))
```

**Root cause:** The `sunbeam_upgrade_traefik_k8s` step recreates the **internal** `traefik`
StatefulSet. Unit `traefik/0` comes back with its private-key secret gone; the charm then
discards its CSR (`Removed CSR from relation data because it did not match any certificate
request`) and **never requests a replacement**. That replica serves traefik's built-in
`TRAEFIK DEFAULT CERT` (self-signed) for the rest of the run. Because kube-proxy
round-robins the internal LB IP across all three replicas, roughly **1 in 3** in-cluster
HTTPS calls to the internal OpenStack API fail certificate verification. nova-api cannot
validate the caller's token on those connections and returns HTTP 500; `juju bootstrap`
hits one and fails.

The cloud never self-heals — errors continue until log collection. Juju reports the app
`active`, so nothing upstream flags the problem.

**Evidence to look for:**
- `generated/sunbeam/juju_status_openstack.txt` — **the decisive check**:
  ```
  traefik/0*  active idle ...  Certificate not available yet
  traefik/1   active idle ...  Serving at https://<internal-VIP>
  traefik/2   active idle ...  Serving at https://<internal-VIP>
  ```
- `pods_openstack_logs.tgz` → `logs-openstack-traefik-0.txt`: `Private key secret not found,
  nothing to remove` and `Removed CSR from relation data because it did not match any
  certificate request`, timestamped at the pod restart inside the upgrade window. These
  appear on **unit 0 only**.
- Blast radius: ~30 pods across ceilometer, cinder, designate, glance, gnocchi, heat,
  neutron, nova, octavia, placement all log `CERTIFICATE_VERIFY_FAILED`.
- **Timeline discriminator** — bucket the errors by minute:
  ```bash
  grep -ah "CERTIFICATE_VERIFY_FAILED" logs-openstack-*.txt \
    | grep -aoP '^\d{4}-\d\d-\d\dT\d\d:\d' | sort | uniq -c
  ```
  Expect a transient burst during `sunbeam_enable_tls`, then a **clean gap** (Tempest
  validation passes here), then an unbroken run of errors starting at the traefik pod
  restart. The gap is what proves the upgrade — not enable-TLS — is the trigger.

> **⚠️ Red herring:** `server_cert is None; sending traces over INSECURE connection` appears
> ~25–29 times on **all three** traefik units and refers to the charm's *tracing* endpoint,
> not the ingress certificate. It is not a discriminator. Use `Removed CSR` /
> `Private key secret not found` and the `Certificate not available yet` unit status instead.

**Recommendations:**
1. Upstream `traefik-k8s` bug: re-request a certificate when the private-key secret is
   missing after StatefulSet recreation, instead of dropping the CSR and stalling forever.
2. A unit at `Certificate not available yet` should be `blocked`/`waiting`, not `active`.
3. Add a post-`sunbeam_upgrade_traefik_k8s` gate asserting every `traefik*` unit message
   starts with `Serving at`, and/or `openssl s_client` each traefik pod IP and verify the
   leaf against the cloud CA.

**Observed in:**
- Run 33207592598 / UUID `14701e8e-b465-487a-9107-88454dc5d1d1`: testflinger cluster_3,
  branch `main`. traefik/0 restarted 04:45:44; clean error gap 03:44–04:45; errors 04:46 →
  06:09; failure 05:16:11 on `GET /servers/<id>` (VM created then deleted by Juju).
- Run 33170527393 / UUID `329df9ba-51f7-49f1-afe5-0b486c0cf28c`: testflinger cluster_2,
  branch `sunbeam_charm_upgrades`. Upgrade 22:53:40–23:11:19; clean gap 22:2x–22:4x; errors
  22:5x → 00:0x; failure 23:16:52 on `POST /servers` (no VM ever created).

Same defect on two clusters and two branches — reproducible, not branch-specific.

---

### Pattern 5: sshuttle SSH link loses return traffic mid-step → one openstack call hangs 120 s → `RemoteDisconnected`

> **Distinct from Pattern 2** (hung daemon, stale REDIRECT → *instant* `ConnectionResetError` on
> the first command). Here earlier openstack commands in the same step succeed, then one call
> hangs ~2 min and fails with `RemoteDisconnected`; the sshuttle daemon actually exits.

**Symptom:**
```
<previous openstack command output, e.g. `quota show` table>   (T)
Unable to establish connection to https://<public-VIP>/openstack-keystone/v3/auth/tokens:
  ('Connection aborted.', RemoteDisconnected('Remote end closed connection without response'))   (T + ~120 s)
```

**Root cause:** The runner reaches the MetalLB VIPs only through `sshuttle ... -r <first node>`
(`ServerAliveInterval=30 ServerAliveCountMax=3`). If traffic from that node back to the runner
stops, the request still reaches Keystone (answered 201) but the reply is lost; ssh gives up after
4×30 s = **120 s**, sshuttle exits and closes the redirected socket → `RemoteDisconnected`. Bare
openstack commands in the step have no retry, so the step fails. Cloud and the node stay healthy.

**Evidence to look for:**
- `generated/sshuttle_metallb_range.txt`: `UP (pid N)` → `DOWN (pid unknown)` in the minute of the failure, DOWN thereafter.
- `pods_openstack_logs.tgz` → `logs-openstack-keystone-*.txt`: last `openstacksdk/... POST /openstack-keystone/v3/auth/tokens ... 201` ≈ 120 s before the failure, none after.
- First node's sosreport `var/log/auth.log`: the sshuttle session (`Accepted publickey ... from <runner IP>` at sshuttle start) shows `session closed` within ~100 ms of the runner error. **Grep the sosreport auth.log/journal, not `journalctl -u ssh`** — the latter can miss it.
- No OOM / link / NIC errors on that node (`sar -n EDEV` on `var/log/sysstat/saDD`).

**Recommendations:** supervise/restart sshuttle and probe through it; retry the pre-bootstrap
openstack commands; or drop the tunnel where the runner has a direct route to the MetalLB range.

**Observed in:**
- Run 38664081501 / UUID `9e27e367-644e-41e3-bcc4-e4d5c1d08252`: testflinger cluster_1, tunnel via
  shuckle (10.241.2.29). Keystone 201 at 06:37:36 → error 06:39:36.227 → shuckle session close 06:39:36.283.
