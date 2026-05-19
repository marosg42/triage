# Step Knowledge: sunbeam_launch_vm

## Step Overview

Launches a test Ubuntu VM inside the deployed Sunbeam OpenStack cloud using
`sunbeam launch ubuntu -n test-instance`, then verifies that the VM is SSH-accessible
by connecting through the bootstrap node as a jump host. This validates end-to-end VM
lifecycle: Nova scheduling, image boot, network wiring, and cloud-init.

The script is `products/sunbeam/launch_vm.py`. For MAAS substrates the bootstrap node
is the first entry in `nodes.yaml`; for cloud substrates the runner connects directly.

## Swift Artifacts

| Path | Description | When to check |
|---|---|---|
| `generated/sunbeam/output.log` | FCE build log for the sunbeam layer (log collection phase) | Always — first stop |
| `generated/sunbeam/juju_status_openstack.txt` | Juju status of OpenStack model | Check charm health |
| `generated/sunbeam/show_units_openstack.txt` | `juju show-units` for OpenStack | Unit agent status |
| `generated/sunbeam/kubectl_get_pod.txt` | Kubernetes pod state | Pod-level failures |
| `generated/sunbeam/pods_openstack_logs.tgz` | Per-pod container logs | Nova/Neutron/OVN errors |
| `generated/version_collector_sunbeam_launch_vm.log` | Snap/apt versions at layer end | Version forensics |

## Key Log Files

| File | What it contains | When to use |
|---|---|---|
| `pods_openstack_logs.tgz` → `logs-openstack-nova-*.txt` | Nova compute/API logs | VM scheduling failures |
| `pods_openstack_logs.tgz` → `logs-openstack-neutron-*.txt` | Neutron logs | Port binding / network wiring |
| `pods_openstack_logs.tgz` → `logs-openstack-ovn-central-*.txt` | OVN north/south logs | Routing table failures |
| `pods_openstack_logs.tgz` → `logs-openstack-nova-api-*.txt` | Nova API logs | MessagingTimeout, BUILD failures |

## Grep Patterns

```bash
WDIR=<work_dir>/<uuid>

# Find the SSH error in GitHub Actions log
grep "No route to host\|Connection refused\|Could not get the proper response" \
  /tmp/run_<run_id>_failed.log

# Count total retry attempts
grep -c "No route to host\|Connection refused" /tmp/run_<run_id>_failed.log

# Find the IP returned by sunbeam launch
grep "ubuntu@" /tmp/run_<run_id>_failed.log | grep "INFO.*localhost" | head -3

# Check OpenStack charm health at time of failure
grep -E "error|blocked|waiting" $WDIR/generated/sunbeam/juju_status_openstack.txt

# Search Nova logs for the test instance
tar -xzf $WDIR/generated/sunbeam/pods_openstack_logs.tgz -C /tmp/openstack-pods/
grep -l "test-instance\|10\.243\." /tmp/openstack-pods/logs-openstack-nova-*.txt
```

## Known Failure Patterns

### Pattern 1: VM launched but SSH returns "No route to host"

**Symptom:**
```
INFO  - [localhost]: `ssh -i /home/ubuntu/snap/openstack/956/sunbeam ubuntu@<IP>`
ERROR - [localhost] Command failed: ssh ... solqa-shared-maas-server-01.maas -- \
  ssh -i .../sunbeam ubuntu@<IP> -o StrictHostKeyChecking=no -- hostname
STDERR: ssh: connect to host <IP> port 22: No route to host
...
Exception: Could not get the proper response
```

**Root cause:** `sunbeam launch` successfully creates the VM in Nova and returns an IP,
but the VM's network path is never established. The jump host (`solqa-shared-maas-server-01.maas`)
receives a hard `No route to host` (ICMP unreachable) — not a timeout or refused
connection — indicating no routing exists to the VM's IP at all. This is distinct from
the VM being up but SSH not yet ready. Possible causes: OVN logical switch port not
bound, Neutron not pushing flow rules to the compute node, or cloud-init failing to
bring up the guest network interface.

All 30 retries (10s apart) fail consistently over ~11 minutes; the VM never becomes
reachable.

**Evidence to look for:**
- GitHub Actions log: all retry SSH calls return `No route to host` (not `Connection refused`)
- `juju_status_openstack.txt`: all OpenStack charms should be `active` (control plane healthy — routing failure is below the charm layer)
- `pods_openstack_logs.tgz` → Nova logs: verify the VM reached `ACTIVE` status and was scheduled to a compute node
- `pods_openstack_logs.tgz` → Neutron/OVN logs: look for port binding errors or missing flow-table entries around the VM creation timestamp

**First observed:** run 23688090524 (UUID 6e7c84f2, tor3-sqa-shared_maas dh1_j8_1, branch main, 2026-03-28)

---

### Pattern 2: VM never reached ACTIVE (Nova BUILD timeout)

**Symptom:**
```
Instance creation request failed: Timeout waiting for
Server:<nova-server-id> to transition to ACTIVE
Error: Unable to request new instance. Please run `sunbeam configure` first.
```
The `sunbeam launch` command exits after ~6m15s with an empty stdout (`b''`).
No SSH connection string is ever printed. The `CalledProcessError` propagates
and fails the step.

**Key distinction from Pattern A:** The VM never reached `ACTIVE` at all — there
is no IP address to SSH to. Pattern A involves a VM that is `ACTIVE` but
unreachable via SSH. Pattern B is a failure earlier in the lifecycle (BUILD stage).

**Root cause:** Nova accepted the instance creation request (scheduler found a
valid host and assigned a server ID) but the nova-compute agent on the target host
failed to spawn the VM within the timeout. Likely causes:
- nova-compute agent down or hung on the scheduled hypervisor
- Libvirt/KVM failing to spawn (disk I/O, storage, or bridge unavailable)
- Glance image download stalled inside the compute node
- Resource exhaustion (RAM/disk) on the compute host

**The misleading "Please run `sunbeam configure` first" message** is a generic
catch-all error from `sunbeam launch` when the launch fails for any reason. It
does not indicate the cluster was actually unconfigured — `sunbeam_deploy`
completed successfully immediately before this step.

**Evidence to look for:**
- GitHub Actions log: `Timeout waiting for Server:<id> to transition to ACTIVE`,
  STDOUT is `b''` (grep for `ubuntu@` never matched)
- `juju_status_openstack.txt`: check if any nova-compute unit is in `error` state
- `pods_openstack_logs.tgz` → `logs-openstack-nova-compute-*.txt`: search for
  `<nova-server-id>` to identify which compute node was targeted and why spawn failed

**First observed:** run 24664528578 (UUID a44c1e26-624b-4dbc-84c9-d6f14dce8ce1,
tor3-sqa-testflinger cluster_2, branch main, 2026-04-20)

---

## Notes

- The error `No route to host` is network-level (ICMP unreachable) and differs from
  `Connection refused` (VM up, SSH not ready yet) or timeout (firewall drop). The
  distinction is important for diagnosing where the wiring failed.
- `launch_vm.py` does not check `openstack server show` — it jumps straight to SSH
  probing. A VM in `ERROR` state will also produce `No route to host` indefinitely.
  Adding `openstack server show <id>` on failure would surface the Nova fault message
  without needing Swift access.
- The jump host for MAAS substrates is always the Sunbeam bootstrap node; for
  cloud substrates the runner connects directly.
- Sunbeam snap revision at time of first observation: `956`

## Version History

- **v1.0** (2026-03-31): Initial version — Pattern A (No route to host) from run 23688090524 (UUID 6e7c84f2, tor3-sqa-shared_maas dh1_j8_1)
- **v1.1** (2026-04-21): Added Pattern B — Nova BUILD timeout (VM never reached ACTIVE); from run 24664528578 (UUID a44c1e26, tor3-sqa-testflinger cluster_2, main, 2026-04-20)
