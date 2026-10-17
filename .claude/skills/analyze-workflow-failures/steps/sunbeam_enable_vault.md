# Step Knowledge: sunbeam_enable_vault

## Step Overview

This step runs `sunbeam enable -m manifest.yaml vault` on the primary Sunbeam node over SSH from
the GitHub runner. The code path is `enable_features.py:enable_vault()` → `run_remote_command(...,
terminal=True)`, with `ssh -t -o ServerAliveInterval=60 -o ServerAliveCountMax=10`. When the
command finishes, it parses `juju status` YAML to initialize and unseal Vault.

## Swift Artifacts

| Path | Description | When to check |
|---|---|---|
| `generated/sunbeam/output.log` | Full step log: SSH commands, timestamps, stderr | Always — first stop (do not rely on `output.condensed.log`) |
| `generated/github-runner/run.log` | Runner log (read only up to the failed step's `##[error]`) | Stack trace / exit code |
| `generated/sshuttle_metallb_range.txt` | Per-minute sshuttle tunnel UP/DOWN status via the primary node | Connectivity timeline (shared_maas / testflinger) |
| `generated/sunbeam/juju_status_openstack.txt`, `kubectl_get_pod.txt` | Post-failure state (only if collect-logs could reach the nodes) | vault-k8s unit / pod state |

## Key Log Files

`shared_maas` bundles contain no node-side logs (no MAAS archive). sosreports are only present if
collect-logs could SSH to the nodes.

## Grep Patterns

```bash
# Vault enable command and its outcome
grep -an "enable -m manifest.yaml vault\|not responding\|Command failed" files/<UUID>/generated/sunbeam/output.log

# Post-failure reachability of nodes (collect-logs)
grep -an "kex_exchange_identification\|Connection reset by\|Failed to copy Juju" files/<UUID>/generated/sunbeam/output.log

# Tunnel status transition
grep -n "DOWN" files/<UUID>/generated/sshuttle_metallb_range.txt | head -3
```

## Known Failure Patterns

### Pattern 1: Truncated `juju status` YAML (ScannerError)

**Symptom:** `enable_features.py:387` raises a YAML `ScannerError` while parsing the status output.

**See also:** `patterns/vault-status-yaml-truncation.md`.

---

### Pattern 2: SSH keepalive timeout to the primary node (transport loss)

**Symptom:**
```text
Timeout, server solqa-shared-maas-server-NN.maas not responding.
subprocess.CalledProcessError: ... 'sunbeam', 'enable', '-m', 'manifest.yaml', 'vault' ... returned non-zero exit status 255.
```

**Root cause:** The runner's SSH session to the primary node stopped receiving keepalive
replies, and the client aborted after 10 × 60 s. Sunbeam or Vault reported no error: stdout is
`b''` and there is no traceback.

To estimate when the connection stalled, subtract 600 s from the failure time. A silent but
healthy session is **not** killed: for example, `sunbeam enable validation` can run 600 s with
no output and complete. So this signature means the transport died, not that the command was
slow.

**Evidence to look for:**
- `output.log`: the vault command starts and fails about 600–660 s later with exit 255. Earlier
  steps on the same node, including validation runs, succeeded.
- Collect-logs: `kex_exchange_identification: read: Connection reset by peer` on **all** nodes
  points to a fault on the runner↔lab network path rather than one hung node.
- `sshuttle_metallb_range.txt`: an UP→DOWN transition around the failure. Its `UP` may only
  mean the sshuttle process exists, because the wrapper can outlive the daemon.

**Verdict:** infrastructure or network flake, NO_BUG. Vault may be left deployed but uninitialized or
sealed.

**Fix candidates:** retry or reconnect on exit 255, then check the remote state; raise
`ServerAliveCountMax` for long enable steps.

## Notes

- `gh run view` from this triage repo needs `-R canonical/sqa-cloud-deployment-pipeline`.
