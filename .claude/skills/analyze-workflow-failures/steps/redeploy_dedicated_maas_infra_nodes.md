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

---

## Notes

- This step only runs on `dedicated_maas` substrates.
- The action validates the hosts sequentially in the order listed by `dedicated_maas/<cluster>.txt`.
  A failure on the first host (for example `anahuac`) prevents checks for later hosts.
- The GitHub Actions composite action source is in
  `~/sqa-cloud-deployment-pipeline/.github/actions/setup/redeploy_infra_nodes/action.yml`.
- Absence of `generated/maas/logs-*.tgz` after this failure is expected: later collection also
  depends on SSH access to the redeployed infra hosts.

