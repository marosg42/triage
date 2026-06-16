---
name: analyze-workflow-failures
description: Given a Solutions Run UUID, analyze the run to identify the first failed step, retrieve its logs, and determine the root cause of failures.
---

# Skill: Analyze GitHub Actions Workflow Run Failures

## Purpose
Given a **Solutions Run UUID**, analyze the run to identify the first failed step, retrieve
its logs (including from Swift object storage), and determine the root cause of failures.

> **Important:** By the time this skill is invoked, the failing job's infrastructure is already gone.
> The goal is purely **diagnosis** — understanding *why* it failed — not remediation.
> All evidence must come from GitHub Actions logs and Swift-stored artifacts.

## Knowledge Base Structure

This skill is split across files to keep context lean. Only load what you need:

```
.claude/skills/analyze-workflow-failures/
├── SKILL.md                        ← you are here (core process)
├── steps/
│   ├── _template.md                                    ← template for new step files
│   ├── existing_juju_maas_controller_microk8s.md       ← per-step logs, patterns, grep hints
│   ├── juju_k8s_controller.md                          ← per-step logs, patterns, grep hints
│   ├── juju_kubernetes_controller.md                   ← per-step logs, patterns, grep hints
│   ├── juju_maas_controller.md                         ← per-step logs, patterns, grep hints
│   ├── k8s_cloud.md                                    ← per-step logs, patterns, grep hints
│   ├── juju_openstack_controller.md                    ← per-step logs, patterns, grep hints                         ← per-step logs, patterns, grep hints
│   ├── maas.md                                         ← per-step logs, patterns, grep hints
│   ├── magpie.md                                       ← per-step logs, patterns, grep hints
│   ├── metallb_microk8s.md                             ← per-step logs, patterns, grep hints
│   ├── microk8s.md                                     ← per-step logs, patterns, grep hints
│   ├── sunbeam_deploy.md                               ← per-step logs, patterns, grep hints
│   ├── sunbeam_maas_deploy.md                          ← per-step logs, patterns, grep hints
│   ├── sunbeam_prepare_env.md                          ← per-step logs, patterns, grep hints
│   ├── sunbeam_test_with_validation_plugin.md               ← per-step logs, patterns, grep hints
│   ├── sunbeam_test_with_validation_plugin_no_features.md  ← per-step logs, patterns, grep hints (log-finding guidance applies to ALL sunbeam_test_with_validation_plugin_* variants)
│   ├── sunbeam_launch_vm.md                               ← per-step logs, patterns, grep hints
│   ├── juju_sunbeam_controller.md                         ← per-step logs, patterns, grep hints
│   ├── landscape.md                                    ← per-step logs, patterns, grep hints
│   ├── deploy_charm_mysql.md                           ← per-step logs, patterns, grep hints
│   ├── deploy_charm_postgresql.md                      ← per-step logs, patterns, grep hints
│   ├── openstack.md                                    ← per-step logs, patterns, grep hints
│   ├── prepare_shared_maas_clusters_for_deployment.md ← per-step logs, patterns, grep hints
│   ├── redeploy_dedicated_maas_infra_nodes.md        ← per-step logs, patterns, grep hints
│   ├── setup_virtual_lab.md                           ← per-step logs, patterns, grep hints
│   ├── test_kubeflow.md                               ← per-step logs, patterns, grep hints
│   ├── kubernetes-maas.md                             ← per-step logs, patterns, grep hints
│   ├── kubernetes_aws.md                              ← per-step logs, patterns, grep hints
│   ├── kubeflow_terraform.md                          ← per-step logs, patterns, grep hints
│   ├── sunbeam_enable_plugins_all.md                  ← per-step logs, patterns, grep hints
│   └── sunbeam_test_plugins.md                        ← per-step logs, patterns, grep hints
└── patterns/
    └── maas-snap-apparmor-dbupgrade.md  ← deep-dive on specific recurring bug
```

After identifying the failed step (Step 2), load the matching step file:
```
view: .claude/skills/analyze-workflow-failures/steps/<failed_step_name>.md
```
If no file exists yet, use `steps/_template.md` as a guide and create one after analysis.

## Terminology

| Term | Description |
|---|---|
| **GitHub Run ID** | The numeric ID of a GitHub Actions workflow run (e.g., `23388633087`). Obtained from `jobs.json` in Swift; used with `gh run view`. |
| **Solutions Run UUID** | A UUID (e.g., `63cd57c4-cba2-48a7-95e9-fb01ae0600cb`) generated per pipeline execution, used to identify the run in Weebl and as the Swift object storage prefix for collected logs. |

## Source Repositories

| Codebase | Local path | Purpose |
|---|---|---|
| `sqa-cloud-deployment-pipeline` | `~/sqa-cloud-deployment-pipeline/` | GitHub Actions workflow definitions, runner scripts |
| `fce` (Foundation Cloud Engine) | `~/cpe/foundation/` | Python library used in most pipeline steps |

When a stack trace or error references a source file, look it up in the appropriate local repo
before drawing conclusions. Do **not** fetch code from GitHub or any remote — use only the
local checkouts above.

## Inputs
- `uuid`: Solutions Run UUID (e.g., `63cd57c4-cba2-48a7-95e9-fb01ae0600cb`) — **required**
- `ignore_patterns`: Steps to ignore (optional, e.g., "log collection", "cleanup")

## Steps

> **⚠️ GitHub access: use `gh` CLI only.**
> Do NOT use GitHub MCP server tools (`github-mcp-server-*`) for any step in this skill —
> they will not work for this repository. All GitHub Actions log retrieval must go through
> the `gh` CLI (Steps 1–3).

### On Entry: Check for Existing Analysis First

Check whether this run has already been analyzed before launching any subagents:

```bash
ls outputs/<UUID>-analysis.md 2>/dev/null && head -5 outputs/<UUID>-analysis.md
```

If the file exists, show the user the first few lines as a reminder of what was found, then
**use `ask_user`** to ask whether to proceed with a fresh analysis or stop. Do not launch any
subagents or download anything until the user confirms. If no file exists, continue immediately.

---

### On Entry: Start Parallel Work

- **First**, run Step A (artifact download) — it downloads all files including `jobs.json` and benefits from maximum head-start. Launch it as a background subagent so GitHub log analysis can proceed in parallel.

---

### Step A. Download Artifacts (Background Subagent)

> **Launch this as a background subagent the instant UUID is known.** Do not wait for it to
> complete before continuing with GitHub log analysis — it runs in parallel.
> **Use model `claude-haiku-4.5`** — this is pure mechanical work (script execution),
> no reasoning required.

> **⚠️ If the download script fails:**
> Stop immediately. Report the error to the user. Do **not** attempt partial analysis based
> only on GitHub logs.

The work directory is `files/` in the current workspace (`/home/ubuntu/triage/files/`).

The subagent must run the download script from the workspace root:

```bash
cd /home/ubuntu/triage
UUID=<UUID> bash download_uuid.sh
```

This will:
1. Fetch the file index from the object storage for the given UUID
2. Download all artifact files into `files/<UUID>/` preserving the full directory tree
3. Print `"Done. Files saved to files/<UUID>/"` on success

> **Do NOT delete `files/<UUID>/` after analysis.** Leave it in place — the user is
> responsible for cleanup.

By the time you reach Step 4 (Log Analysis), `files/<UUID>/` should already be populated.
If the subagent is still running, wait for it before proceeding to Step 4.

---

### 1. Obtain the GitHub Run ID from Downloaded Artifacts

Once Step A completes, parse `jobs.json` from the downloaded artifacts:

```bash
python3 -c "
import json, sys
jobs = json.load(open('files/<UUID>/generated/github-runner/jobs.json'))
for j in jobs['jobs']:
    print(j['name'], j['conclusion'], j['run_id'])
"
```

`jobs.json` contains the full GitHub API job response, including `run_id`, `run_url`,
`html_url`, branch, conclusion, and per-step details. It also makes a useful quick triage
source — step names and conclusions without needing to call `gh`.

Example:
```json
{
  "jobs": [{
    "run_id": 23388633087,
    "run_url": "https://api.github.com/repos/canonical/sqa-cloud-deployment-pipeline/actions/runs/23388633087",
    "html_url": "https://github.com/canonical/sqa-cloud-deployment-pipeline/actions/runs/23388633087/job/68047963001",
    "head_branch": "betterssh",
    "conclusion": "failure",
    ...
  }]
}
```

### 2. Get Run Overview and Identify Failed Steps
```bash
gh run view <run_id>
```

Parse output to identify all jobs and their statuses (✓ = success, X = failure, - = skipped),
find the first failed step, and note the substrate, cluster, and branch from context.

```bash
gh run view <run_id> --log-failed 2>&1 > files/run_<run_id>_failed.log
wc -l files/run_<run_id>_failed.log
```

> **Prefer `--log-failed`** over `--log` — it's much smaller and focused on failures.

Then find the actual error:
```bash
grep -n "##\[error\]\|Process completed with exit code" files/run_<run_id>_failed.log
```

Extract context around the first exit code error:
```bash
sed -n '<start>,<end>p' files/run_<run_id>_failed.log | grep -v "^\(Run the pipeline.*\*\*\*\)$" | cat
```

### 3. Know Your Substrate

The **substrate** determines which logs are available. Check `SUBSTRATE` in the GitHub Actions
environment variables early — it shapes the entire investigation strategy.

| Substrate | MAAS logs available? | Notes |
|---|---|---|
| `tor3-sqa-virtual_maas` | ✅ Yes | KVM VMs on libvirt hosts; MAAS logs in `generated/maas/logs-*.tgz` |
| `tor3-sqa-dedicated_maas` | ✅ Yes | Physical servers; MAAS logs in `generated/maas/logs-*.tgz` |
| `tor3-sqa-shared_maas` | ❌ No | Shared MAAS infrastructure; no MAAS logs collected |
| `tor3-sqa-testflinger` | ❌ No | Testflinger-managed; no MAAS logs |
| `aws` / `azure` | ❌ No | Public cloud; no MAAS |

**MAAS logs are only present for `virtual_maas` and `dedicated_maas` substrates.** For all
other substrates, skip Steps 4b and 5 entirely — there will be no `generated/maas/logs-*.tgz`.

#### Distinguishing virtual_maas from dedicated_maas

`virtual_maas` and `dedicated_maas-dh1_j6` share the same DNS domain, which can cause
confusion. **Always trust the substrate name reported in `jobs.json`** — do not try to
infer substrate type from network addresses or domain names.

If you need a quick sanity-check, look at the machine names in the MAAS nodes:

| Substrate | MAAS nodes (infra) | Other nodes |
|---|---|---|
| `virtual_maas` | `infra1`, `infra2`, `infra3` | `node1` … `node6` |
| `dedicated_maas-dh1_j6` | Pokémon names (e.g. `leafeon`) | Also Pokémon names |

If the node names follow the `infra[1-3]` / `node[1-6]` pattern → `virtual_maas`.
If node names are Pokémon names → `dedicated_maas`.

#### Important: MAAS log timestamps

The MAAS log archive (`logs-*.tgz`) and `generated/foundation.log` accumulate across runs on
the same cluster. The **first portion of these logs may be months old** — from previous
pipeline executions on the same infra. This is expected and harmless.

**When analysing, only look at the log section that aligns with the timestamp of the job
being investigated.** Locate the job's start time from `jobs.json` (`started_at` field) and
filter to that window:

```bash
# Get the job start time from jobs.json
python3 -c "import json; j=json.load(open('files/<UUID>/generated/github-runner/jobs.json')); [print(x['started_at'], x['name']) for x in j['jobs']]"

# Filter MAAS syslog to the relevant time window (e.g., 2026-03-19T18:)
grep "2026-03-19T18:" files/maas-logs/10.241.144.2/var/log/syslog | head -20
```

### 4. Use Logs from Downloaded Artifacts

If Step A (background subagent) has not yet reported completion, wait for it before proceeding.
All artifacts are already available under `files/<UUID>/` — no further download is needed.

> `files/<UUID>/` is **not** cleaned up after analysis. Leave it as-is for the user.

Key layout:
```
files/<UUID>/
├── FCB.md
├── config/          — nodes.yaml, networks.yaml, overlays, etc.
├── generated/
│   ├── lastlines.txt              — tail of all log streams (quick triage)
│   ├── foundation.log
│   ├── project_comp.log
│   ├── features.yaml / project_features.yaml
│   ├── version_collector_<layer>.log
│   ├── sshtest.txt                — SSH tunnel health log (virtual_maas only; see 4a)
│   ├── github-runner/
│   │   ├── jobs.json              — GitHub API job metadata (run_id, steps, conclusions)
│   │   └── run.log                — full runner log
│   ├── maas/
│   │   ├── log.txt                — short FCE build log for the MAAS layer
│   │   ├── logs-<timestamp>.tgz   — full MAAS infrastructure logs archive
│   │   └── maas-api               — MAAS API URL
│   └── sunbeam/                   — Sunbeam layer logs
└── ...
```

#### 4a. Quick triage from local files

```bash
# Tail of all log streams:
cat files/<UUID>/generated/lastlines.txt

# GitHub runner metadata (step names, conclusions):
python3 -m json.tool files/<UUID>/generated/github-runner/jobs.json | head -60

# FCE MAAS build log:
cat files/<UUID>/generated/maas/log.txt
```

#### 4a-i. SSH tunnel health check (virtual_maas only)

> **For `virtual_maas` substrates, always check `sshtest.txt` before diving into logs.**

`generated/sshtest.txt` records a periodic SSH connectivity test (roughly once per minute)
through the tunnel to the `virtual_maas` infra. Each entry is a timestamp followed by the
hostname returned (`infra1`). When the tunnel fails, the SSH error appears instead:

```
Wed Apr  8 21:22:20 UTC 2026
infra1
kex_exchange_identification: Connection closed by remote host
Connection closed by 10.241.144.2 port 22
```

**How to use it:**

```bash
cat files/<UUID>/generated/sshtest.txt
```

- **If no errors appear**: the tunnel held throughout the run — MAAS logs are fully trustworthy.
- **If SSH errors appear**: note the timestamp of the last successful entry. **Any MAAS or
  infra logs collected after that timestamp are from the underlying dedicated_maas hosts that
  share the same IP range — not from this run's virtual_maas cluster. Treat all such logs as
  irrelevant to this run's failure.** The tunnel failure itself is likely the direct cause of
  any step failure that occurred at or after that time (the runner lost connectivity to the
  virtual cluster).

> **Important:** The `generated/maas/logs-*.tgz` for a `virtual_maas` run is collected by
> SSHing through the tunnel. If the tunnel was down when log collection ran, the archive may
> contain logs from the wrong hosts (dedicated_maas Pokémon-named nodes) or be empty. Do not
> treat Pokémon-named hostnames in MAAS logs as evidence that the substrate was dedicated_maas
> — always check `sshtest.txt` and trust `jobs.json` for the substrate name.

#### 4b. Extract the MAAS infrastructure logs archive

> **Only applicable for `virtual_maas` and `dedicated_maas` substrates.** Skip for all others.

The full per-node MAAS logs are inside a nested tgz within the bundle:
```bash
MAAS_TGZ=$(ls files/<UUID>/generated/maas/logs-*.tgz 2>/dev/null | head -1)
mkdir -p files/maas-logs && tar -xzf ${MAAS_TGZ} -C files/maas-logs

# Map IPs to hostnames:
# virtual_maas: hostnames are infra1/infra2/infra3; dedicated_maas: Pokémon names (e.g. leafeon)
for d in files/maas-logs/*/; do
  ip=$(basename $d)
  echo -n "$ip: "
  head -1 $d/var/log/syslog 2>/dev/null | grep -oP '\S+ kernel' | grep -oP '^\S+' || echo "(no syslog)"
done
```

### 5. Analyze MAAS Logs

> **Only applicable for `virtual_maas` and `dedicated_maas` substrates.**

The archive contains per-node directories (e.g., `10.241.144.2/`, `10.241.144.3/`, `10.241.144.4/`).

#### Most useful log files

| File | What it contains | When to use |
|---|---|---|
| `var/log/syslog` | Everything: systemd, snapd, MAAS services, AppArmor | Primary investigation file |
| `var/log/maas/var.lib.maas.log` | MAAS service log (captured from syslog) | Quick MAAS-specific view |
| `var/log/postgresql/postgresql-16-main.log` | PostgreSQL main instance | DB errors, migration failures |
| `var/log/postgresql/postgresql-16-ha.log` | HA standby PostgreSQL | Replication issues |
| `var/log/haproxy.log` | HAProxy health checks for MAAS API backends | Service availability |
| `var/snap/maas/common/log/dump.dmp` | PostgreSQL DB dump (pg_dump format) | Schema inspection, migration state |
| `var/snap/maas/common/log/db-stat.txt` | DB statistics | DB health overview |
| `var/snap/maas/common/log/snap-perms.txt` | Snap file permissions | Snap version/revision info |
| `var/log/libvirt/qemu/*.log` | VM console/QEMU logs | VM boot issues |

#### Search patterns for common issues

```bash
# Find all instances of a specific error across all nodes
grep -rh "error_string" files/maas-logs/*/var/log/syslog 2>/dev/null | sort

# Get clean timeline (exclude noise)
grep -h "2026-03-22T01:0" files/maas-logs/10.x.x.x/var/log/syslog 2>/dev/null \
  | grep -v "kernel\|audit\|apparmor\|named\|maas-machine\|#011\|twisted\|django\|maasserver\|provisioning" \
  | sort

# Count errors by process
grep -rh "error_string" files/maas-logs/*/var/log/syslog 2>/dev/null \
  | grep -oP 'infra\d+ maas-regiond\[\d+\]' | sort | uniq -c | sort -rn
```

### 6. Extract Error Details

From the logs, extract:
- **Exception type and message**: The specific error that occurred
- **Stack trace**: Full trace showing the call chain
- **File paths and line numbers**: Where the error occurred
- **Failed command**: The exact command that failed
- **Preceding context**: Environment variables, configs, prior steps

**Pattern Recognition:**
- `CalledProcessError`: Command failed — check exit code and stderr
- `CalledProcessError` **exit code 255**: SSH connection failure — the remote command may have actually succeeded; check timing gap for idle timeout
- `FileNotFoundError`: Missing file — check if deployment type expects it
- `KeyError`: Missing dictionary key — check data structure assumptions
- `ConnectionError` / `TimeoutError`: Network/service issues
- `django.db.utils.ProgrammingError`: DB schema mismatch — check migrations
- `ServerError: 400 Bad Request (Failed to render preseed: ...)`: MAAS API error — check DB schema

### 7. Perform Root Cause Analysis

> **Do not attribute failures to leftover state from a previous run** unless you have direct,
> specific evidence — for example: a named resource whose creation timestamp clearly pre-dates
> this run's start time, an "already exists" error referencing a UUID from a different run, or
> a log message explicitly confirming a stale lease, IP, or record was reused. There is
> substantial automated cleanup between runs; assume the environment was clean at the start of
> this run unless the evidence unambiguously says otherwise.

1. **Locate source code** mentioned in stack traces
2. **Check snap versions**: MAAS snap auto-refreshes during runs can cause mid-run failures
3. **Check snap refresh events** in syslog:
   ```bash
   grep -h "41404\|auto-refresh\|post-refresh\|taskrunner" files/maas-logs/*/var/log/syslog 2>/dev/null | grep "2026-" | sort
   ```
4. **Correlate timing**: Did the failure happen *during* or *after* the refresh?
5. **Check DB migration state**: Use dump.dmp to inspect schema
6. **Check AppArmor denials**: `DENIED` entries in syslog can indicate hook failures, but **beware of false positives**.
   - *Note: AppArmor `DENIED` messages (e.g., for `/etc/gss/mech.d/`) are often benign probes by underlying libraries (like `psycopg2` checking for Kerberos auth) that gracefully fall back. Do not assume an AppArmor denial is the root cause without corroborating evidence. Always look for underlying exceptions, database concurrency issues, or serialization failures first.*

## Output Format

**Scope rules — follow strictly:**
- Analyse **only the run provided**. Do NOT reference, compare, or draw connections to other runs unless the user explicitly asks.
- Each run is treated as independent. Do not mention that a "similar" issue occurred elsewhere, or suggest substrate-wide patterns based on multiple runs in the same session.
- The "Notable Context" section is **not part of the output template** — omit it entirely.

```markdown
# Workflow Run Analysis Report

## Run: <github_run_id>
- **Repository:** canonical/sqa-cloud-deployment-pipeline
- **Branch:** <branch>
- **Status:** ❌ Failed
- **Duration:** <total_time>
- **Solutions Run UUID:** <uuid>

---

## Failed Step: <step_name>

### Executed Steps (relevant excerpt)
- Step 1 ✅
- Step N ❌ **← FAILED HERE**
- Step N+1 ✅ (post-failure cleanup)

---

## Failure Analysis

### Error
```
<exception_type>: <message>

Stack Trace:
  File "<file>", line <line>, in <function>
    <code>
  <error_type>: <detailed_message>
```

### Environment Context
- **Substrate:** <substrate>
- **Cluster:** <cluster>
- **Environment:** <environment>

---

## Root Cause

**Summary:** <one-sentence description>

**Detailed Explanation:**
1. What the code was trying to do
2. What assumptions it made
3. Why those assumptions failed
4. The specific condition that triggered the error

**Evidence:**
- Finding 1: <observation from GitHub logs>
- Finding 2: <observation from Swift/MAAS logs>
- Finding 3: <timeline correlation>

---

## Recommendations

1. <Infrastructure/config change that would prevent recurrence>
2. <Code change that would improve resilience or error messaging>
```

---

## Save Report to File

After displaying the report to the user, write it to disk:

```bash
mkdir -p outputs
# Write the report to outputs/<UUID>-analysis.md
```

The filename must be `outputs/<UUID>-analysis.md` where `<UUID>` is the Solutions Run UUID.
Use the current working directory (repository root) as the base path.

If the UUID is not known (analysis was only possible from a GitHub Run ID with no UUID found),
use `outputs/<run_id>-analysis.md` as a fallback.

---

## Key Techniques

### Log Analysis Patterns
- **Save logs first**: Always download full logs to a file for manipulation
- **Use `--log-failed`**: Much smaller than `--log`, focused on what matters
- **Use line numbers**: `grep -n` to get line numbers for extraction
- **Extract sections**: `sed -n '<start>,<end>p'` for targeted extraction
- **Filter noise**: Exclude `***` (masked secrets), `#011` (continuation lines), `twisted`/`django` framework internals
- **Follow timestamps**: Logs are chronological, correlate events by time
- **Check all nodes**: The tgz archive contains logs from all infra nodes — the failing node may not be obvious

### Snap Refresh Investigation
- Always check if a snap auto-refresh occurred during or just before the failure window
- Correlate: when did the new snap mount? When did the hook run? How long did it take?
- AppArmor denials during the hook window are a red flag
- A hook that completes in <30s is suspicious for complex operations like DB migrations
- A hook that takes exactly 10m0s was killed by snapd

### Context Gathering
- **Environment variables**: Always check `SUBSTRATE`, `CLUSTER`, `ENVIRONMENT` in GitHub logs
- **Previous steps**: Look at what ran successfully before failure
- **Deployment markers**: Identify deployment type (virtual_maas vs dedicated_maas vs AWS/Azure)

## Limitations

- **Requires authentication**: `gh` CLI must be authenticated
- **Swift access**: Required — artifacts are downloaded directly from object storage via `download_uuid.sh`. If the download fails, stop and report; do not attempt workarounds
- **Bundle size**: The full UUID bundle can be 50–100 MB — `download_uuid.sh` (launched as background subagent) handles this efficiently
- **Domain knowledge**: Some errors require specific MAAS/Juju/Sunbeam product knowledge
- **Diagnosis only**: This skill identifies root causes. The failing infrastructure is gone — no fix can be applied retroactively. Findings should be used to inform future preventive action.

## Related Skills

- **Debug Python Applications**: For deeper Python-specific debugging
- **Analyze Juju Deployments**: For Juju-specific issues

## Updating the Knowledge Base

After completing an analysis, if you discovered a new root cause or new evidence for an
existing pattern, update the knowledge base so future analyses benefit:

### If a step file already exists (`steps/<step_name>.md`)
1. Add the new pattern under "Known Failure Patterns" following the existing format
2. If the pattern is complex or reusable, create `patterns/<descriptive-name>.md` and reference it

### If no step file exists
1. Copy `steps/_template.md` to `steps/<step_name>.md`
2. Fill in the Swift artifacts, log file table, grep patterns, and the new pattern
3. Update the Knowledge Base Structure section in this file to list the new step file

The user will review with `git diff` and commit when satisfied.

