# Step Knowledge: sunbeam_enable_loadbalancer-amphora

## Step Overview

Enables the Sunbeam Amphora loadbalancer feature. The step sets the local Sunbeam snap feature
gate (`feature.loadbalancer-amphora=true`), writes Amphora configuration into the manifest, and
re-runs `sunbeam enable -m manifest.yaml loadbalancer`. Failures usually appear as the
loadbalancer enable command timing out while waiting for the OpenStack Juju model to settle.

## Swift Artifacts

Objects stored under `<uuid>/generated/sunbeam/` that are useful for diagnosing failures:

| Path | Description | When to check |
|---|---|---|
| `generated/sunbeam/output.log` | Full Sunbeam orchestration log, including the failing remote command and embedded Juju timeout status | Always |
| `generated/sunbeam/juju_status_openstack.txt` | Collected Juju status of the OpenStack model | Always — look for blocked `octavia` units |
| `generated/sunbeam/pods_openstack_logs.tgz` | Per-pod logs for OpenStack applications | Deep investigation of blocked unit hook traces |
| `generated/sunbeam/kubectl_get_pod_detailed.txt` | Pod placement and node names | When failures are pinned to one Kubernetes node |

## Key Log Files (inside pods_openstack_logs.tgz)

| File | What it contains | When to use |
|---|---|---|
| `logs-openstack-glance-*.txt` | Glance API logs | When the failure is in `sunbeam loadbalancer configure` (Amphora image import) |
| `logs-openstack-octavia-*.txt` | Octavia charm/workload logs | Primary investigation for loadbalancer blocked status |
| `logs-openstack-placement-*.txt` | Placement charm logs | Check for Pebble `a2dissite` exec timeouts during relation storms |
| `logs-openstack-designate-*.txt` | Designate charm logs | Check for Pebble file read/write timeouts |
| `logs-openstack-nova-*.txt` | Nova charm/workload logs | Check for co-located blocked units and service restart symptoms |

## Grep Patterns

```bash
# Find the failed loadbalancer wait
grep -n "loadbalancer-amphora\|sunbeam enable -m manifest.yaml loadbalancer\|Timed out enabling loadbalancer" \
  <work_dir>/<uuid>/generated/sunbeam/output.log

# Find blocked OpenStack units at collection time
grep -n "blocked.*Error in charm\|octavia/[0-9].*blocked\|designate/[0-9].*blocked\|nova/[0-9].*blocked\|placement/[0-9].*blocked" \
  <work_dir>/<uuid>/generated/sunbeam/juju_status_openstack.txt

# After extracting pods_openstack_logs.tgz, find Pebble timeout signatures
grep -RIn "TimeoutError: timed out\|timeout waiting for websocket connections\|ops.pebble.ChangeError" \
  <work_dir>/pods_openstack/generated/sunbeam/logs-openstack-{octavia,placement,designate,nova}-*.txt
```

## Known Failure Patterns

### Pattern 1: Amphora loadbalancer wait times out because TLS cert distribution left Octavia blocked

**Symptom:**
```
Enabling loadbalancer-amphora
...
sunbeam enable -m manifest.yaml loadbalancer
Timed out enabling loadbalancer: TimeoutError('wait timed out after 899.999...s')
subprocess.CalledProcessError: Command '['ssh', ..., 'sunbeam', 'enable', '-m', 'manifest.yaml', 'loadbalancer']' returned non-zero exit status 1.
```

**Root cause:** A prior `sunbeam tls ca unit_certs` run can trigger a cluster-wide relation
storm. If one Kubernetes node's Pebble API stalls during the storm, several CAAS units can hit
socket-level Pebble timeouts (`container.push`/`container.pull`, or `exec` websocket
connections). The `ops_sunbeam` guard marks those units permanently blocked, and later
`sunbeam enable loadbalancer` waits on the Octavia app until the fixed timeout expires.

**Evidence to look for:**
- `output.log`: Amphora feature gate succeeds, then `sunbeam enable -m manifest.yaml
  loadbalancer` fails after ~900s.
- `juju_status_openstack.txt`: `octavia` / `octavia/N` blocked with `(workload) Error in charm
  (see logs): timed out` before the failing step started.
- Extracted pod logs: affected units show `TimeoutError: timed out` or
  `timeout waiting for websocket connections: context deadline exceeded` in guarded
  `Bootstrapping` sections.
- `kubectl_get_pod_detailed.txt`: affected units may be co-located on the same Kubernetes node,
  while sibling units on other nodes complete the same hook successfully.

### Pattern 2: `sunbeam loadbalancer configure` → `TerraformException()` because Glance web-download import is misconfigured

**Symptom:**
```
sunbeam loadbalancer configure            # runs ~1800s
Error creating Amphora OpenStack resources: TerraformException()
Error: terraform command failed: /snap/openstack/<rev>/bin/terraform apply -input=false -auto-approve -no-color -json
stderr:                                   # empty
```

**Root cause:** glance-k8s has `enabled_import_methods` (web-download) on but no import staging
directory (`node_staging_uri` None). The Amphora image (`openstack_images_image_v2.amphora`,
web-download from tarballs.opendev.org) is accepted (202) but the import task fails instantly
with `BadTaskConfiguration`; the image stays `queued` until the provider's 30-min timeout.
Glance units still show `active` in juju status. Seen with glance-k8s `2026.1/edge` rev 308.

**Evidence to look for:**
- The real Terraform log is NOT in the runner output — extract it from the bootstrap node's
  sosreport: `tar -xJf sosreport-<server-01>-*.tar.xz --wildcards '*/home/ubuntu/snap/openstack/*'`
  then `.../common/etc/<cluster-name>/loadbalancer-setup-plan/terraform-apply-<ts>.log`.
  Look for `OpenStack image status is: queued` repeated and
  `Error waiting for Image: context deadline exceeded`.
- `pods_openstack_logs.tgz` → `logs-openstack-glance-*.txt`:
  `Import methods are enabled but staging directory None does not exist; Imports will fail!`
  (repeated at worker starts, on all units) and, on the unit that served the import POST,
  a `202` access-log line followed ~25 ms later by
  `ERROR glance.async_.taskflow_executor [-] Task initialization failed: <task-uuid> of
  api_image_import not configured properly. Value of node_staging_uri must be in format
  'file://<absolute-path>': glance.common.exception.BadTaskConfiguration` (stack in
  `glance/async_/flows/api_image_import.py` around lines 520/527/952). The image never reaches
  `importing`/`active`.
- `keystonemiddleware.audit ValueError: action failed validation` on the same request is benign noise.

**Verified on run 5c058ce4-776b-455c-8f2f-56d02c3fe6de (2026-10-04, shared_maas dh1_j8_1,
glance-k8s 2026.1/edge rev 308):** step failed after exactly 1800 s; terraform log
`terraform-apply-20261004011122.log` showed 179 consecutive `OpenStack image status is: queued` polls over the 30-minute budget; the staging warning appeared 73× across glance-0/1/2 (39/22/12) from pod bring-up
(2026-10-03T22:52) onward, i.e. `node_staging_uri` was already unset long before the step.

## Notes

- Do not assume the loadbalancer command itself failed to apply configuration. In this pattern,
  the feature gate and manifest update can succeed; the failure is the subsequent readiness wait.
- Check whether blocked units predate the failed step so the original failing hook is attributed
  to the prior TLS/CA reconfiguration rather than to Amphora.
