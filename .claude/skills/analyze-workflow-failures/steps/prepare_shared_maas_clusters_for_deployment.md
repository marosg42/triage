# Step Knowledge: prepare_shared_maas_clusters_for_deployment

## Step Overview

Prepares a `shared_maas` resource pool before a deployment by logging into the shared MAAS controller, releasing any machines already allocated in the pool, and then adding the expected extra partition to storage-tagged machines.

## Swift Artifacts

Objects stored under `<uuid>/generated/` that are useful for diagnosing failures:

| Path | Description | When to check |
|---|---|---|
| `generated/github-runner/jobs.json` | GitHub job metadata, including step names and timestamps | Always — identify the failed top-level step |
| `generated/github-runner/run.log` | Full runner log collected from GitHub Actions | Always — primary evidence source |
| `generated/project_comp.log` | Rendered project configuration, including shared MAAS API URL and pool tags | Confirm target MAAS endpoint / cluster |
| `generated/foundation.log` | Minimal FCE wrapper log | Quick sanity check only |

For `shared_maas` substrates there is no `generated/maas/logs-*.tgz`; diagnosis is normally limited to runner logs and generated config artifacts.

## Key Log Files

| File | What it contains | When to use |
|---|---|---|
| `generated/github-runner/run.log` | Step commands, stderr, and exit status | Primary investigation |
| `generated/project_comp.log` | The concrete MAAS URL, pool/tag names, and rendered node/vm config | Verify environment targeting |
| `generated/github-runner/jobs.json` | Step numbering and timing | Correlate the failure window |

## Grep Patterns

Useful search strings for this step's failure modes:

```bash
# Find the failing shared-pool preparation command
grep -n "Prepare shared MAAS clusters for deployment\|machines read pool=sqa-\|maas login sqa-bot" <work_dir>/<uuid>/generated/github-runner/run.log

# Find truncated MAAS API / CLI responses or hung login termination
grep -n "IncompleteRead\|Process completed with exit code 2\|Terminated\|Process completed with exit code 143" <work_dir>/<uuid>/generated/github-runner/run.log

# Confirm the target shared MAAS controller and pool tags
grep -n "10\.239\.7\.110/MAAS\|sqa-dh1_" <work_dir>/<uuid>/generated/project_comp.log
```

## Known Failure Patterns

### Pattern 1: MAAS CLI truncated machine-list response

**Symptom:**
```text
nodes=$(maas sqa-bot machines read pool=sqa-${CLUSTER} | jq -r '.[].system_id')
...
IncompleteRead(108236 bytes read, 605910 more expected)
##[error]Process completed with exit code 2.
```

**Root cause:** The `Release Machines` substep depends on a single `maas ... machines read` call returning the full JSON description of every machine in the shared pool. In this failure mode the MAAS CLI receives only a partial HTTP body, raises `IncompleteRead`, and exits before the shell can enumerate any system IDs. Because the composite action uses `bash -e -o pipefail`, the whole top-level step fails immediately and the later `Add partitions` substep is never reached.

**Evidence to look for:**
- `generated/github-runner/run.log`: the exact `nodes=$(maas sqa-bot machines read pool=sqa-${CLUSTER} | jq -r '.[].system_id')` command in the `Release Machines` substep.
- `generated/github-runner/run.log`: `IncompleteRead(<bytes> bytes read, <bytes> more expected)` followed by exit code 2.
- `generated/project_comp.log`: shared MAAS API URL and `sqa-<cluster>` tags confirming which resource pool the action targeted.

### Pattern 2: MAAS login hangs until the runner kills it

**Symptom:**
```text
echo "Login to MAAS"
maas login sqa-bot ${maas_url} ${MAAS_CREDS}
...
Terminated
##[error]Process completed with exit code 143.
```

**Root cause:** The `MAAS Login` substep assumes the shared-MAAS authentication call will either return quickly or fail noisily. In this failure mode the `maas login` CLI never produces any further output and never exits, so the composite action remains stuck before `Release Machines` or `Add partitions` can run. Because the workflow does not wrap `maas login` in an explicit timeout or retry loop, the run idles for hours until the runner sends `SIGTERM`, which surfaces only as exit code 143.

**Evidence to look for:**
- `generated/github-runner/run.log`: the `Login to MAAS` banner followed by the exact `maas login sqa-bot ${maas_url} ${MAAS_CREDS}` command.
- `generated/github-runner/run.log`: no further MAAS CLI output after the login banner, then `Terminated` / `Process completed with exit code 143` hours later.
- `generated/project_comp.log`: the shared MAAS endpoint (`http://10.239.7.110/MAAS`) and `sqa-<cluster>` tags confirming which pool the stalled login targeted.

---

_Add more patterns below as they are discovered._

## Notes

- This step is only used for `shared_maas` substrates.
- There are no MAAS infrastructure logs for `shared_maas`; GitHub runner logs are usually the only direct failure evidence.
- The top-level workflow step wraps multiple composite-action substeps, so identify whether the failure happened in `MAAS Login`, `Release Machines`, or `Add partitions` before drawing conclusions.

## Version History

- **v1.1** (2026-05-27): Added Pattern 2 — `maas login` to shared MAAS never returns, emits no diagnostic output, and is eventually killed by the runner with exit code 143 before `Release Machines` starts; from run 26440193596 (UUID 9566a408-3d65-4f7c-9091-6e7d1ddaa52e).
- **v1.0** (2026-05-27): Initial version documenting MAAS CLI `IncompleteRead` while enumerating machines in a shared resource pool (run 26455309054, UUID ba7fec2e-a8fc-4ef3-af81-a4cddb368035).
