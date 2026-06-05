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

