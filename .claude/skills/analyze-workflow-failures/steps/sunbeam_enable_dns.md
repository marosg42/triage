# Step Knowledge: sunbeam_enable_dns

## Step Overview

Runs `products/sunbeam/enable_features.py <ENV> dns`, which SSHes to the primary Sunbeam
node (first entry in `generated/sunbeam/nodes.yaml`, e.g. `kulik.maas`) and runs
`sunbeam enable -m manifest.yaml dns <domain>` (e.g. `dns maas.`). This deploys `bind`,
`designate`, `designate-mysql-router` into the `openstack` k8s model and relates
`designate:dnsaas` ↔ `neutron:external-dns` (neutron pods restart → OVN SB clients reconnect).

## Swift Artifacts

| Path | Description | When to check |
|---|---|---|
| `generated/github-runner/run.log` | Runner log; the step's stdout/stderr + traceback | Always — first stop (read only up to the `##[error]`) |
| `generated/sunbeam/output.log` | Same FCE log stream for all sunbeam layers | Timeline |
| `generated/sunbeam/juju_status_openstack.txt` | Post-failure status; check `bind`/`designate` apps | Did the feature actually converge? |
| `generated/sunbeam/juju_debug_log_{controller,openstack}.txt` | Juju logs; `user-<node>` logins show sunbeam client activity | Correlate client activity vs failure time |
| `generated/sunbeam/sosreport-<node>-*.tar.xz` | Per-node sosreports (may be missing for the primary node if SSH is broken) | Node-side evidence |
| `generated/sshuttle_metallb_range.txt` | Per-minute sshuttle (via primary node) health | Was the long-lived SSH to the node still alive? |

## Grep Patterns

```bash
# SSH transport errors to nodes (pre-failure part of run.log only)
head -n <error_line> generated/github-runner/run.log | grep -n "kex_exchange\|banner exchange\|closed by remote host\|port 22"
# Same signatures during log collection (sosreport per node)
grep -n "Failed to collect files from\|banner exchange\|port 22: .*timed out" generated/github-runner/run.log
# Sunbeam client activity per minute
grep "user-<node>.maas" generated/sunbeam/juju_debug_log_controller.txt | grep -oE "^controller-0: [0-9:]{5}" | uniq -c
```

## Known Failure Patterns

### Pattern 1: SSH session to primary node dropped mid-`sunbeam enable dns` (exit 255)

**Symptom:**
```
Connection to kulik.maas closed by remote host.
subprocess.CalledProcessError: Command '['ssh', '-t', ..., 'kulik.maas', '--', 'sunbeam', 'enable', '-m', 'manifest.yaml', 'dns', 'maas.']' returned non-zero exit status 255.
```

**Root cause:** Transport-level SSH failure between runner and the primary node, not a
DNS feature bug. Seen on `tor3-sqa-testflinger` cluster_2 (run ed0529bd…): the same run
also had `kex_exchange_identification: banner line contains invalid characters` /
`banner exchange: Connection to 10.241.3.31 port 22: invalid format` before (05:01:57, in
enable_vault, retried OK) and after (05:20:48, sosreport) the failure, and
`Connection to 10.241.3.32 port 22 timed out` for mouser. designate/bind ended up
`active`, and the sshuttle SSH session via the same node stayed UP throughout.

**Evidence to look for:**
- `run.log`: exit 255 + `closed by remote host` with empty stdout, ~80 s into the command
- `run.log`: other `banner exchange ... invalid format` lines against the same node IP
- `juju_status_openstack.txt`: `bind`/`designate` active → feature itself converged
- Primary-node sosreport missing (collection hit the same SSH error)

---

_Add more patterns below as they are discovered._

## Notes

- The primary node is also the sshuttle relay and Juju client host; every feature step
  runs over a single long `ssh -t` session to it, so any SSH hiccup fails the step.
- `enable_vault` has a retry around `juju status` SSH calls; `enable_features.py`'s
  `sunbeam enable` call has none.
