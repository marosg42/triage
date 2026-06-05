# Step Knowledge: setup_virtual_lab

## Step Overview

Bootstraps the `virtual_maas` test environment: provisions a Testflinger relay VM, prepares the remote KVM/LXD host, starts the shared virtual MAAS infra VMs, and validates runner connectivity to the virtual MAAS subnet through an `sshuttle` tunnel.

## Swift Artifacts

Objects stored under `<uuid>/generated/` that are useful for diagnosing failures:

| Path | Description | When to check |
|---|---|---|
| `generated/github-runner/run.log` | Full GitHub Actions runner log, including `setup-virtual-lab`, `setup-tunnel`, and retries | Always — primary evidence source |
| `generated/foundation.log` | FCE log collected during build/log collection phases | Check for post-failure SSH/log-collection cascades |
| `generated/maas/maas-api` | MAAS API key copied from the shared virtual MAAS seed host | Useful to confirm setup progressed through shared-virtual-maas staging |
| `generated/sshtest.txt` | Periodic SSH health checks through the tunnel | Check if log collection succeeded far enough to create it |

If `setup_virtual_lab` fails before `collect-logs` can SSH to the infra nodes, `generated/sshtest.txt`, `generated/maas/log.txt`, and `generated/maas/logs-*.tgz` may be missing entirely.

## Key Log Files (inside tgz archive)

This step often fails before a MAAS infrastructure tgz is collected. When `generated/maas/logs-*.tgz` is absent, rely on runner logs instead.

| File | What it contains | When to use |
|---|---|---|
| `generated/github-runner/run.log` | Action-level shell output, relay host IP, virsh start results, tunnel checks | Primary investigation |
| `generated/foundation.log` | Follow-on `collect-logs` SSH failures | Confirm whether reachability was still broken after the main failure |

## Grep Patterns

Useful search strings for this step's failure modes:

```bash
# Find the tunnel preflight failure
grep -nE 'Attempt [0-9]+: Scanning|All attempts failed for|sshuttle' <work_dir>/<uuid>/generated/github-runner/run.log

# Confirm relay host and infra startup progression
grep -nE 'REMOTE_HOST=|virsh start infra|Domain .* started|maas-api' <work_dir>/<uuid>/generated/github-runner/run.log

# Check whether post-failure log collection hit the same SSH problem
grep -nE 'Failed to ssh to|Connection timed out' <work_dir>/<uuid>/generated/foundation.log <work_dir>/<uuid>/generated/github-runner/run.log
```

## Known Failure Patterns

### Pattern 1: Virtual MAAS infra never reachable after tunnel setup

**Symptom:**
```text
Attempt 1: Scanning 10.241.144.2...
Scan failed. Retrying in 60 seconds...
...
All attempts failed for 10.241.144.2
##[error]Process completed with exit code 1.
```

**Root cause:** The relay VM used for `virtual_maas` came up and the remote host reported `virsh start` success for `infra1`/`infra2`/`infra3`, but the runner never established SSH reachability to the virtual MAAS subnet through the relay. In this state, `setup-tunnel` exits before MAAS login or later layers begin. The direct failure is loss of reachability to `10.241.144.2`; the precise guest-side reason (guest boot failure vs. tunnel/network failure) cannot be determined if no MAAS/log-collection artifacts were captured.

**Evidence to look for:**
- `generated/github-runner/run.log`: `ssh-keyscan` works for the relay host, `virsh start infra1/2/3` succeeds, then repeated scan failures begin for `10.241.144.2`
- `generated/foundation.log`: later `collect-logs` also reports `Failed to ssh to '10.241.144.2'`
- Artifact tree: `generated/sshtest.txt` and `generated/maas/logs-*.tgz` are absent because the tunnel never became usable enough for later collection

### Pattern 2: Tunnel reaches infra briefly, then drops before MAAS login completes

**Symptom:**
```text
Attempt 3: Scanning 10.241.144.2...
# 10.241.144.2:22 SSH-2.0-OpenSSH_9.6p1 Ubuntu-3ubuntu13.15
Attempt 1: Scanning 10.241.144.3...
# 10.241.144.3:22 SSH-2.0-OpenSSH_9.6p1 Ubuntu-3ubuntu13.15
Attempt 1: Scanning 10.241.144.4...
# 10.241.144.4:22 SSH-2.0-OpenSSH_9.6p1 Ubuntu-3ubuntu13.15
IncompleteRead(380435 bytes read, 158769 more expected)
[Errno 113] No route to host
Failed to login after 10 attempts
```

**Root cause:** `setup-tunnel` eventually established SSH reachability to the three infra nodes, but the tunnel or underlying virtual MAAS infra failed almost immediately afterwards. The background SSH health probe in `generated/sshtest.txt` captured one successful `infra1` response and then `Connection closed by 10.241.144.2 port 22`. The subsequent `maas login` loop can surface either a transient HTTP 502 or an `IncompleteRead` while MAAS is still partially answering, then repeated `No route to host` errors once connectivity to `10.241.144.0/21` is gone. This means the direct failure is loss of tunnel/reachability after initial success, not a persistent MAAS API application error.

**Evidence to look for:**
- `generated/github-runner/run.log`: SSH scans for `10.241.144.2/.3/.4` succeed, then `maas login root http://10.241.144.5:80/MAAS ...` fails with `IncompleteRead` (optionally preceded by `502`) and repeated `[Errno 113] No route to host`
- `generated/sshtest.txt`: exactly one successful probe (`infra1`) followed immediately by an SSH disconnect such as `kex_exchange_identification: Connection closed by remote host` or `Connection closed by 10.241.144.2 port 22`
- `generated/foundation.log`: later `collect-logs` still cannot SSH to `10.241.144.2`, confirming connectivity never recovered
- Artifact tree: `generated/maas/log.txt` and `generated/maas/logs-*.tgz` are absent because the run never regained enough access to collect MAAS-side logs

### Pattern 3: One virtual MAAS infra node never completes SSH validation

**Symptom:**
```text
Attempt 3: Scanning 10.241.144.2...
# 10.241.144.2:22 SSH-2.0-OpenSSH_9.6p1 Ubuntu-3ubuntu13.15
Attempt 1: Scanning 10.241.144.3...
# 10.241.144.3:22 SSH-2.0-OpenSSH_9.6p1 Ubuntu-3ubuntu13.15
Attempt 1: Scanning 10.241.144.4...
10.241.144.4: Connection closed by remote host
...
All attempts failed for 10.241.144.4
##[error]Process completed with exit code 1.
```

**Root cause:** `setup-tunnel` successfully reached the relay host and validated SSH to `infra1` and `infra2`, but `infra3` (`10.241.144.4`) never completed a usable SSH handshake. The action's `for i in 2 3 4; do run_ssh_keyscan ... || exit 1; done` logic treats any per-node failure as fatal, so the step exits before starting the background `sshtest.txt` probe or attempting `maas login`. The direct failure is therefore a single virtual MAAS infra node that booted far enough to accept TCP connections but immediately closed the SSH handshake, not a MAAS API error.

**Evidence to look for:**
- `generated/github-runner/run.log`: relay host is reachable, `virsh start infra1/2/3` succeeds, `10.241.144.2` and `.3` return SSH banners, but `.4` repeatedly returns `Connection closed by remote host` and never passes `ssh-keyscan`
- `.github/actions/setup/setup-tunnel/action.yml`: after `sshuttle` starts, the action exits immediately if any of `10.241.144.2/.3/.4` fails validation
- Artifact tree: `generated/sshtest.txt` is absent because the failure happened before the background probe was launched; `generated/maas/logs-*.tgz` is also absent, leaving no per-node guest logs for deeper diagnosis

---

_Add more patterns below as they are discovered._

## Notes

- This step is specific to `tor3-sqa-virtual_maas`; no MAAS tgz may exist when failure happens before or during tunnel validation.
- `setup-tunnel` validates the virtual MAAS network by probing `10.241.144.2`, `10.241.144.3`, and `10.241.144.4` after launching `sshuttle` through the Testflinger relay.
- `virsh start` success alone is not enough to prove the infra VMs booted correctly; the decisive signal is SSH reachability through the tunnel.

