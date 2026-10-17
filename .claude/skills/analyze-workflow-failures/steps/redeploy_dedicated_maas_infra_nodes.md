# Step Knowledge: redeploy_dedicated_maas_infra_nodes

## Step Overview

This composite action releases the dedicated MAAS infra nodes for a cluster, regenerates the
Terraform plan, redeploys the infra hosts through the MAAS Terraform provider, waits for DNS to
settle, verifies SSH access, and only then pushes the expected netplan to each node.

## Swift Artifacts

Objects inside the UUID bundle that are useful for this step:

| Path | Description | When to check |
|---|---|---|
| `generated/maas/output.log` | Output from `dedicated_maas/release_with_retry.sh` | First — confirms the old infra hosts were found, released, and returned to `Ready` before redeploy starts |
| `generated/github-runner/run.log` | Full GitHub Actions runner log | Primary source for Terraform create progress, DNS checks, and the SSH wait loop |
| `generated/foundation.log` | FCE summary log from later collection steps | Useful to prove the same host stayed unreachable after the first failure |
| `generated/marvin-the-happy-bot/snapshot-*/github/run-log-failed.txt` | Snapshot copy of failed GitHub logs | Fallback if `run.log` is incomplete |
| `generated/maas/logs-*.tgz` | Full MAAS infra logs archive | Only if log collection succeeds; often absent when the dead infra node never accepts SSH |

## Key Log Files (inside tgz archive)

If `generated/maas/logs-*.tgz` exists, these are the most useful files:

| File | What it contains | When to use |
|---|---|---|
| `var/log/syslog` | Systemd, cloud-init, kernel, MAAS service messages | Primary place to confirm boot failures, networking problems, or cloud-init stalls |
| `var/log/cloud-init.log` / `var/log/cloud-init-output.log` | First-boot cloud-init activity | Check when Terraform says deployed but the node never becomes SSH reachable |
| `var/log/installer/*` or curtin logs | Deploy/install details | Use when the node appears to fail before first successful boot |

## Grep Patterns

Useful search strings for this step's failure modes:

```bash
# Terraform completed, then SSH validation failed
grep -n "Creation complete after\|Apply complete!\|DNS results are consistent\|Waiting for SSH on\|Failed to ssh to" \
  <work_dir>/<uuid>/generated/github-runner/run.log

# The same host was still unreachable during later collection
grep -n "Failed to ssh to '\|ssh: connect to host" \
  <work_dir>/<uuid>/generated/foundation.log

# Release phase really did finish successfully before redeploy started
grep -n "Getting machine with hostname\|Machine .* has status Ready\|All machines are in 'Ready' state" \
  <work_dir>/<uuid>/generated/maas/output.log

# If a MAAS tgz exists, check boot/cloud-init on the failed host
ls <work_dir>/<uuid>/generated/maas/logs-*.tgz
```

## Known Failure Patterns

### Pattern 1: Terraform deploy completes but an infra node never becomes SSH reachable

**Symptom:**
```text
maas_instance.nodes["anahuac"]: Creation complete after 12m57s [id=hqpnep]
Apply complete! Resources: 3 added, 0 changed, 0 destroyed.
...
WARNING: Failed to ssh to anahuac at 10.241.8.13 after 60 seconds
ERROR: Failed to ssh to anahuac on all available IPs: 10.241.8.13
##[error]Process completed with exit code 1.
```

Later, collection hits the same host again:

```text
ssh: connect to host 10.241.128.4 port 22: Connection timed out
root ERROR Fatal: Failed to ssh to '10.241.128.4' using user 'ubuntu'
```

**Root cause:** The redeploy itself reached MAAS/Terraform's definition of success, but the
underlying infra node never finished becoming reachable on the network afterward. The composite
action in `.github/actions/setup/redeploy_infra_nodes/action.yml` assumes that once
`maas_instance` creation completes, a short settle period plus a 60-second SSH probe is enough.
In this failure mode that assumption is wrong because the host is still dead or misconfigured at
boot time. The same timeout later during `collect-logs` proves this is not just an overly short
probe window or a runner-side false negative — the machine stayed unavailable. Without a MAAS log
archive or serial console capture, the exact node-side cause cannot be narrowed further than
post-deploy boot/network/cloud-init failure.

**Evidence to look for:**
- `generated/maas/output.log`: prior dedicated infra hosts were released and returned to `Ready`.
- `generated/github-runner/run.log`: Terraform create finished, then the sequential SSH validation loop failed on the first host.
- `generated/foundation.log`: later collection still timed out to the failed node's static infra IP.
- `generated/maas/logs-*.tgz`: often missing entirely because the same unreachable node prevents deeper collection.

### Pattern 2: Shared MAAS API hangs/resets mid-deploy (terraform + re-release both fail)

**Symptom:**
```text
maas_instance.nodes["noma"]: Still creating... [04m10s elapsed]   # one resource stops polling first
│ Error: Get "http://10.239.8.11:5240/MAAS/api/2.0/machines/7q6epf/": read tcp ...->10.239.8.11:5240: read: connection reset by peer
(x3, all machines, same second)
Release attempt 1/3 ... ConnectionResetError: [Errno 104] Connection reset by peer   (in http.client _read_status)
ERROR: Failed to release machines after 3 attempts
Deploy machines (attempt 2) ... skipped
ERROR: Failed to deploy machines after 3 attempts
```

**Root cause:** The external **shared** MAAS region/API frontend (`maas_url` in the step env,
`vars.SHARED_MAAS_URL`) hangs at the application layer. TCP still connects, but no HTTP status line comes back, and the connection is RST after 15–60 s. The canonical/maas Terraform provider does not retry polls, so it aborts. `release_with_retry.sh` only gives 3×30 s of retries. `Re-release after failed deploy number N` has no `continue-on-error`, so its failure **skips deploy attempts 2/3**, and the job dies about 4 min after the outage starts. This is a prerequisite-infra outage; the shared MAAS logs are not in the bundle.

**Evidence to look for:**
- Initial release POSTs in `generated/maas/output.log` already slow (e.g. 6 s / 15 s / 51 s instead of ~1–2 s), which is an early warning.
- In run.log, per-resource `Still creating` lines show which poll hung first, giving the real onset about 60 s before the reset.
- `output.log` re-release entries show only `Getting machine with hostname <first host>`, with nothing after.
- Concurrent `read ECONNRESET` from Vault or Temporal timeouts in the same window can appear. Do not treat them as proof of a runner network issue; the live `curl` (0 bytes while TCP connects) is what decides it.

---

## Notes

- This step only runs on `dedicated_maas` substrates.
- The action validates the hosts sequentially in the order listed by `dedicated_maas/<cluster>.txt`.
  A failure on the first host (for example `anahuac`) prevents checks for later hosts.
- The GitHub Actions composite action source is in
  `~/sqa-cloud-deployment-pipeline/.github/actions/setup/redeploy_infra_nodes/action.yml`.
- Absence of `generated/maas/logs-*.tgz` after this failure is expected: later collection also
  depends on SSH access to the redeployed infra hosts.

