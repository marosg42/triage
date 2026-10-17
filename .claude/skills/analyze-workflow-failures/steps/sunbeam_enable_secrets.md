# Step Knowledge: sunbeam_enable_secrets

## Step Overview

Runs `../products/sunbeam/enable_features.py ${ENVIRONMENT} secrets` through
`.github/actions/builds/sunbeam_enable_feature`. The script SSHes from the runner to the
primary Sunbeam node and runs `sunbeam enable -m manifest.yaml secrets` (Barbican, which
requires vault to be enabled first). On `tor3-sqa-virtual_maas`, all runner→node traffic goes
through the runner's sshuttle tunnel (`10.241.144.0/21` via the relay).

## Swift Artifacts

| Path | Description | When to check |
|---|---|---|
| `generated/github-runner/run.log` | Runner log; the step's SSH command, stderr and traceback | Always, first stop (read only up to the step's `##[error]`) |
| `generated/sunbeam/output.log` | Sunbeam layer output (original, not `.condensed`) | Feature-level errors / juju status dumps |
| `generated/version_collector_sunbeam_enable_secrets.log` | Post-step SSH probes to `10.241.144.2` | Confirms whether connectivity survived the step |
| `generated/sshtest.txt` | Per-minute tunnel probe (virtual_maas only) | Always on virtual_maas |
| `generated/sunbeam/sosreport-<host>-*.tar.xz` | Node sosreports | The hostname shows whether collection reached the real `node1` |

## Grep Patterns

```bash
# The step's SSH call and result
head -n <error_line> generated/github-runner/run.log | grep -n "Enabling secrets\|Command failed\|kex_exchange\|Connection reset\|closed by remote host"
# Tunnel health (virtual_maas)
grep -n -B2 "kex\|Connection\|IDENTIFICATION" generated/sshtest.txt
# Runner egress problems in the same window (Temporal retries)
head -n <error_line> generated/github-runner/run.log | grep -n "Failed to report to Temporal\|tls handshake eof"
```

## Known Failure Patterns

### Pattern 1: virtual_maas sshuttle tunnel died before the step; SSH reaches the colliding physical host

**Symptom:**
```
DEBUG - [localhost]: ssh -t ... node1.dh1-j6.tor3-sqa-dedicated-maas.solutionsqa -- sunbeam enable -m manifest.yaml secrets
ERROR - [localhost] Command failed: ...   (about 60 s later, STDOUT b'')
kex_exchange_identification: read: Connection reset by peer
Connection reset by 10.241.144.81 port 22
subprocess.CalledProcessError: ... returned non-zero exit status 255.
```

**Root cause:** The runner→relay sshuttle tunnel died (journald: `sshuttle: Timeout, server
<relay> not responding` / `c : fatal`) after the previous feature (pro) finished. With the
tunnel gone, 10.241.144.0/21 routes to the physical dedicated_maas cluster that reuses the same
IPs. The SSH to 10.241.144.81 reached a physical Juju controller (`juju-1`), which reset the
handshake. `sunbeam enable secrets` never ran, and the virtual deployment itself was healthy.
This is lab infra, not a Barbican/Sunbeam bug.

**Evidence to look for:**
- `sshtest.txt`: the last `infra1` probe comes before the step start, followed by
  `kex_exchange_identification: Connection closed by remote host` and no further entries
- `run.log`: Temporal reporting for the previous layer fails on all 5 attempts (`tls handshake
  eof` / `Timeout expired`) in the same window. This stretches the gap before the step
  (about 14 minutes)
- `version_collector_sunbeam_enable_secrets.log`: SSH to `10.241.144.2` gets `Connection reset
  by peer` on every retry
- `generated/sunbeam/sosreport-juju-1-*`: host `juju-1` with only `juju-db` snap, i.e. the
  collision host rather than the virtual `node1`. MAAS `logs-*.tgz` contains Pokémon hosts
- Matches `steps/sunbeam_enable_plugins_all.md` Patterns 12/13 (same tunnel-drop mechanism)

**First observed:** run 38695576543 (UUID 93a6ce2c-44e7-4081-97d1-49414fe3c8a4, tor3-sqa-virtual_maas cluster_6, branch main, 2026-10-15)

---

## Notes

- On virtual_maas, always check `sshtest.txt` first. An exit-255 SSH failure with empty stdout
  almost always means a transport failure, not a feature failure.
- `kex_exchange_identification: read: Connection reset by peer` (as opposed to `closed by
  remote host` mid-session) means the connection never established. The tunnel was already
  gone before the step started.
