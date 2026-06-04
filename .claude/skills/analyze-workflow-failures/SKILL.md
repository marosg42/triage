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
│   ├── deploy_charm_mysql.md                           ← per-step logs, patterns, grep hints
│   ├── openstack.md                                    ← per-step logs, patterns, grep hints
│   ├── prepare_shared_maas_clusters_for_deployment.md ← per-step logs, patterns, grep hints
│   ├── redeploy_dedicated_maas_infra_nodes.md        ← per-step logs, patterns, grep hints
│   ├── setup_virtual_lab.md                           ← per-step logs, patterns, grep hints
│   ├── test_kubeflow.md                               ← per-step logs, patterns, grep hints
│   ├── kubernetes-maas.md                             ← per-step logs, patterns, grep hints
│   ├── kubernetes_aws.md                              ← per-step logs, patterns, grep hints
│   └── sunbeam_enable_plugins_all.md                  ← per-step logs, patterns, grep hints
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

- **First**, launch the `jobs.json` fetch as a background Haiku subagent — it is small (~50 KB) and returns quickly with the `run_id` and step list you need to drive the rest of the analysis
- **Immediately after**, launch Step A (Swift bundle download) as a second background Haiku subagent — it is large and benefits from maximum head-start

---

### Step A. Download Swift Artifacts (Background Subagent)

> **Launch this as a background subagent the instant UUID is known.** Do not wait for it to
> complete before continuing with GitHub log analysis — it runs in parallel.
> **Use model `cgoogle/gemini-3.5-flash`** — this is pure mechanical work (MCP call + curl + tar),
> no reasoning required.

> **⚠️ If the Swift MCP server is unavailable or returns an error:**
> Stop immediately. Report to the user that Swift is unavailable and that analysis cannot
> proceed without the artifacts. Do **not** attempt workarounds, alternative download methods,
> or partial analysis based only on GitHub logs.

**Before launching the subagent**, determine the work directory from your `<session_context>`:
it is the `files/` subdirectory of the session folder listed there
(e.g. `/home/ubuntu/.copilot/session-state/<session_id>/files`).
Pass this path explicitly in the subagent prompt as `<work_dir>`.

The subagent must:

1. Call `swift-mcp-stage_uuid_bundle` with `uuid = "<UUID>"` — this returns a download URL
2. Download the bundle to the session files directory (passed as `<work_dir>` in the prompt):
   ```bash
   curl -fsSL <url_from_tool> -o <work_dir>/<UUID>.tgz
   ```
3. Extract it into the same directory:
   ```bash
   mkdir -p <work_dir>/<UUID>
   tar -xzf <work_dir>/<UUID>.tgz -C <work_dir>/<UUID> --strip-components=1
   ```
   This produces `<work_dir>/<UUID>/` preserving the full directory tree.
4. Report success: `"Done — <work_dir>/<UUID>/ is ready"` (or report any error)

> **Do NOT delete `<work_dir>/<UUID>/` after analysis.** Leave it in place — the user is
> responsible for cleanup. Having all logs available persistently is more important than
> disk tidiness.

By the time you reach Step 4 (Log Analysis), `<work_dir>/<UUID>/` should already be populated.
If the subagent is still running, wait for it before proceeding to Step 4.

---

### 1. Obtain the GitHub Run ID from Swift

The `jobs.json` fetch subagent (launched on entry) provides the `run_id`. Parse it:

```bash
python3 -c "
import json, sys
jobs = json.load(open('<work_dir>/<uuid>-jobs.json'))
for j in jobs['jobs']:
    print(j['name'], j['conclusion'], j['run_id'])
"
```

> **Why `stage_object` not `get_object`?** `jobs.json` is ~50 KB — `get_object` returns it
> inline and the output gets truncated to a temp file, requiring extra parsing steps.
> `stage_object` returns a download URL so you can `curl` it straight to disk.

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
gh run view <run_id> --log-failed 2>&1 > /tmp/run_<run_id>_failed.log
wc -l /tmp/run_<run_id>_failed.log
```

> **Prefer `--log-failed`** over `--log` — it's much smaller and focused on failures.

Then find the actual error:
```bash
grep -n "##\[error\]\|Process completed with exit code" /tmp/run_<run_id>_failed.log
```

Extract context around the first exit code error:
```bash
sed -n '<start>,<end>p' /tmp/run_<run_id>_failed.log | grep -v "^\(Run the pipeline.*\*\*\*\)$" | cat
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
python3 -c "import json; j=json.load(open('<work_dir>/<uuid>-jobs.json')); [print(x['started_at'], x['name']) for x in j['jobs']]"

# Filter MAAS syslog to the relevant time window (e.g., 2026-03-19T18:)
grep "2026-03-19T18:" <work_dir>/maas-logs/10.241.144.2/var/log/syslog | head -20
```

### 4. Use Logs from Swift

If Step A (background subagent) has not yet reported completion, wait for it before proceeding.
All artifacts are already available under `<work_dir>/<uuid>/` — no further download is needed.

> `<work_dir>/<uuid>/` is **not** cleaned up after analysis. Leave it as-is for the user.

Key layout:
```
<work_dir>/<uuid>/
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
cat <work_dir>/<uuid>/generated/lastlines.txt

# GitHub runner metadata (step names, conclusions):
python3 -m json.tool <work_dir>/<uuid>/generated/github-runner/jobs.json | head -60

# FCE MAAS build log:
cat <work_dir>/<uuid>/generated/maas/log.txt
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
cat <work_dir>/<uuid>/generated/sshtest.txt
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
MAAS_TGZ=$(ls <work_dir>/<uuid>/generated/maas/logs-*.tgz 2>/dev/null | head -1)
mkdir -p <work_dir>/maas-logs && tar -xzf ${MAAS_TGZ} -C <work_dir>/maas-logs

# Map IPs to hostnames:
# virtual_maas: hostnames are infra1/infra2/infra3; dedicated_maas: Pokémon names (e.g. leafeon)
for d in <work_dir>/maas-logs/*/; do
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
grep -rh "error_string" <work_dir>/maas-logs/*/var/log/syslog 2>/dev/null | sort

# Get clean timeline (exclude noise)
grep -h "2026-03-22T01:0" <work_dir>/maas-logs/10.x.x.x/var/log/syslog 2>/dev/null \
  | grep -v "kernel\|audit\|apparmor\|named\|maas-machine\|#011\|twisted\|django\|maasserver\|provisioning" \
  | sort

# Count errors by process
grep -rh "error_string" <work_dir>/maas-logs/*/var/log/syslog 2>/dev/null \
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
   grep -h "41404\|auto-refresh\|post-refresh\|taskrunner" /tmp/maas-logs/*/var/log/syslog 2>/dev/null | grep "2026-" | sort
   ```
4. **Correlate timing**: Did the failure happen *during* or *after* the refresh?
5. **Check AppArmor denials**: `DENIED` entries in syslog can indicate hook failures
6. **Check DB migration state**: Use dump.dmp to inspect schema

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
- **Swift access**: Required — if the Swift MCP server is unavailable, stop and report; do not attempt workarounds
- **Bundle size**: The full UUID bundle can be 50–100 MB — `stage_uuid_bundle` + `curl` (launched as background subagent) handles this efficiently; the MAAS logs tgz inside is an additional nested archive to extract separately
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

### Always
- Bump the version history at the bottom of this file
- Bump the version history at the bottom of the step file

The user will review with `git diff` and commit when satisfied.

## Version History
- **v3.5** (2026-06-04): Updated `steps/sunbeam_maas_deploy.md` with another confirmation of the dedicated MAAS deploy-time `suicune` curtin late-command failure from run 26687730876 / UUID 99a74612-fa41-4cd0-8013-a63535b1db5b: machine `5` / instance `suicune` failed `driver_51_osd_zap` on host `noma` at `17:47:26Z`, where `ceph-bluestore-tool zap-device` returned `Operation not permitted`, leaving the node in `Failed deployment` and causing `sunbeam cluster deploy` to time out waiting for machines.
- **v3.4** (2026-06-04): Updated `steps/sunbeam_maas_deploy.md` with Pattern 7 from run 26710851237 / UUID 0a948444-837e-4dd6-8d31-4642581e907e: `sunbeam cluster bootstrap` hit a 5-minute `aiohttp.streams.readany` timeout during the `Add machines` step because it called `maas_client.list_machines` without pool filtering on a large `dedicated_maas` region.

- **v3.3** (2026-06-02): Updated `steps/sunbeam_maas_deploy.md` with another confirmation of the dedicated MAAS deploy-time `suicune` curtin late-command failure from run 26644898503 / UUID 96f56fa7-cce5-48b5-9ec2-28a41cfbbbdf: machine `5` / instance `akxawm` failed `driver_51_osd_zap` on host `anahuac` at `17:23:19Z`, where `ceph-bluestore-tool zap-device` returned `Operation not permitted`, leaving the node in `Failed deployment` and causing `sunbeam cluster deploy` to time out waiting for machines.
- **v3.2** (2026-06-02): Updated `steps/sunbeam_maas_deploy.md` with another confirmation of the dedicated MAAS deploy-time `suicune` curtin late-command failure from run 26666874596 / UUID e2855722-2c0c-4c23-8872-6a3fc72ed2ae: machine `5` / instance `brd87a` failed `driver_51_osd_zap` on host `anahuac`, where `ceph-bluestore-tool zap-device` returned `Operation not permitted`, leaving the node in `Failed deployment` and causing `sunbeam cluster deploy` to time out waiting for machines.
- **v3.1** (2026-06-02): Updated `steps/sunbeam_maas_deploy.md` with a new dedicated MAAS deploy-time pattern from run 26721324961 / UUID 6d4c2445-ec78-44d9-9fbc-6ad73fb19004: storage node `suicune` failed its host-specific curtin late command `driver_51_osd_zap` because `ceph-bluestore-tool zap-device` on `/dev/disk/by-id/wwn-0x600508b1001c2289bcab3857b9bd9e4c` returned `Operation not permitted`, leaving machine `5` / instance `dtpqgp` in `Failed deployment` and causing `sunbeam cluster deploy` to time out waiting for machines.
- **v3.0** (2026-06-02): Updated `steps/sunbeam_launch_vm.md` with another confirmation of the `No route to host` launch-time reachability pattern from run 26644918393 / UUID f34d99dd-687b-4324-961b-2cdc1ce10bac: `sunbeam launch` returned `ubuntu@10.242.4.191`, Neutron associated that floating IP to port `ac1bbca9-d65b-4cd3-83e0-f8a32cce0a69`, and Nova recorded `network-vif-plugged` for instance `7ce0d1b2-ff72-4289-942a-3224c7caf851` on `tor3-sqa-testflinger` / `cluster_3`, but all 30 SSH probes from bootstrap node `octopot.maas` still failed with `No route to host`.
- **v2.99** (2026-06-02): Updated `steps/juju_sunbeam_controller.md` with another confirmation of the bootstrap-guest reachability pattern from run 26662393808 / UUID cde3f622-1f89-49ca-b183-39d0a69c1522: runner-side access to `traefik-public-lb` (`10.241.38.134`) stayed healthy (`sshuttle` UP; refstack `200` responses) while the new Juju bootstrap VM on `demo-network` (`192.168.122.10`) timed out posting to Keystone on `tor3-sqa-shared_maas` / `dh1_j9_1`.
- **v1.0** (2026-02-16): Initial version covering basic workflow failure analysis
- **v1.1** (2026-03-23): Added Swift log access workflow, Solutions Run UUID concept, MAAS snap refresh / AppArmor / DB migration bug patterns learned from runs 23388633087, 23377136139, 23406159064
- **v1.2** (2026-03-23): Modular split — moved Known Bug Patterns to `steps/sunbeam_prepare_env.md` and `patterns/maas-snap-apparmor-dbupgrade.md`; added knowledge update instructions; UUID now a valid input
- **v1.3** (2026-03-30): Subagent temp paths changed from `/tmp` to `<work_dir>` (session state `files/` dir) — subagents cannot write to `/tmp`; main agent must resolve `<work_dir>` from `<session_context>` and pass it in the subagent prompt
- **v1.3** (2026-03-23): Added `steps/sunbeam_deploy.md` from analysis of run 23409860645 (Terraform state lock contention during concurrent cluster joins)
- **v1.4** (2026-03-23): Updated `steps/sunbeam_deploy.md` with SSH Broken Pipe / 2-hour idle timeout pattern from run 23415263364
- **v1.6** (2026-03-23): Added Step 4 "Know Your Substrate" — substrate table clarifying that only `virtual_maas` and `dedicated_maas` contain MAAS logs; all other substrates have no MAAS logs. Added note that MAAS logs accumulate across runs and the first portion may be months old — only analyse the window matching the investigated job's timestamp.
- **v1.9** (2026-03-24): Swift download now launched as a background subagent in parallel with GitHub log analysis the moment UUID is known; extraction goes to `/tmp/<UUID>/` (no strip-components, no mkdir); directory is never cleaned up — user is responsible for that
- **v2.0** (2026-03-24): Removed `steps/juju_kubernetes_controller.md`
- **v2.1** (2026-03-24): Re-added `steps/juju_kubernetes_controller.md` with 60-attempt API contact exhaustion pattern from run 23372951162 (UUID 990c6ff4, tor3-sqa-shared_maas dh1_j8_2)
- **v2.2** (2026-03-24): Third occurrence of juju_kubernetes_controller CrashLoopBackOff confirmed — run 23408678722 (UUID 1a004baa, tor3-sqa-shared_maas, 2026-03-22). Pattern is recurring/intermittent on tor3-sqa-shared_maas.
- **v2.3** (2026-03-25): Added `steps/existing_juju_maas_controller_microk8s.md` — MicroK8s dqlite "database is locked" pattern from run 23491280339 (UUID c0a33b83, tor3-sqa-virtual_maas cluster_4).
- **v2.4** (2026-03-25): Added `steps/metallb_microk8s.md` — unexpected EOF pattern from run 23491285216 (UUID 8647be66, tor3-sqa-virtual_maas cluster_6).
- **v2.5** (2026-03-25): Output Format — added scope rules: do not compare with other runs or mention cross-run patterns unless explicitly asked.
- **v2.6** (2026-03-25): Added `steps/magpie.md` — Juju controller MongoDB "not master and slaveOk=false" pattern from run 23512120069 (UUID c6a6fdfd, tor3-sqa-virtual_maas cluster_3).
- **v2.7** (2026-03-26): Added `steps/juju_openstack_controller.md` — controller unit not in expected Juju space during enable_ha from run 23573425818 (UUID 00908688, ext-sqa-ps6_openstack cluster_1).
- **v2.8** (2026-03-26): Added `steps/deploy_charm_mysql.md` — 60-minute juju wait-for timeout while mysql/N stuck in "Setting up cluster node" during 3-unit InnoDB cluster bootstrap, from run 23343431862 (UUID d4685121, tor3-sqa-sunbeam cluster_1).
- **v2.9** (2026-03-26): Switch `jobs.json` retrieval from `swift-mcp-get_object` (inline, truncates at ~50KB) to `swift-mcp-stage_object` + `curl` to disk; avoids temp-file parsing workaround.
- **v2.10** (2026-03-26): Use `claude-haiku-4.5` for Step A (Swift download) subagent and `jobs.json` fetch subagent — both are mechanical tasks, no reasoning needed, saves premium model usage.
- **v2.11** (2026-03-26): Added second `deploy_charm_mysql` pattern to `steps/deploy_charm_mysql.md` — Juju agent never connected (Nova ACTIVE but machine stays Juju `pending`) from run 23343435826 (UUID 57cc163c, tor3-sqa-sunbeam cluster_3).
- **v2.12** (2026-03-27): Added `steps/maas.md` — AppArmor `snap.maas.pebble` virsh/pkttyagent denial blocks KVM host registration, nodes stuck in Deploying, from run 23321492075 (UUID 8faad6c3, tor3-sqa-virtual_maas cluster_5, snap rev 41649).
- **v2.13** (2026-03-27): Updated `steps/sunbeam_deploy.md` with terraform apply timeout during `sunbeam configure` — Neutron API unreachable after Glance image upload, `context deadline exceeded` retries exhausted over 9 min, from run 23699798454 (UUID 36e0a8b4, tor3-sqa-dedicated-maas dh1_j6).
- **v2.14** (2026-03-29): Added Pattern C to `steps/juju_maas_controller.md` — Bootstrap agent started but Juju API (port 17070) never accessible, 60 attempts exhausted over ~11 min, from run 23710187387 (UUID c3c6526f, tor3-sqa-shared_maas dh1_j9_1). Distinct from sub-patterns A/B: SSH succeeds, config completes, agent starts, but API stays `connection refused`.
- **v2.15** (2026-03-30): Updated `steps/sunbeam_deploy.md` with Traefik routes not ready (502 Bad Gateway) pattern — `sunbeam configure` called immediately after last cluster join; Traefik still processing ingress-relation-joined events returns 502 on first Neutron API calls; from run 23674711306 (UUID f11a3633, tor3-sqa-shared_maas dh1_j9_1, rev 956).
- **v2.16** (2026-03-30): Added `steps/sunbeam_test_with_validation_plugin_no_features.md` — Pattern A: floating IP / VM public network unreachable; 2 smoke tests failed (`test_network_basic_ops` ping timeout, `ServerActionsTestJSON` SSH wait timeout); quick validation passed; from run 23674710083 (UUID bf85bf5d, tor3-sqa-shared_maas dh1_j8_1, branch main).
- **v2.17** (2026-03-30): After completing analysis, save the report to `outputs/<UUID>-analysis.md` in the repository root.
- **v2.18** (2026-03-30): Added Pattern B to `steps/juju_openstack_controller.md` — Nova `MessagingTimeout` fault during `openstack_bootstrap`; VM stuck in BUILD for 6 retries (~60s), never reached `enable_ha`; from run 23748868831 (UUID 1a687feb, tor3-sqa-sunbeam cluster_4, branch main, 2026-03-30).
- **v2.19** (2026-03-30): Added prohibition on GitHub MCP server — use `gh` CLI only for all GitHub Actions access.
- **v2.20** (2026-03-31): Extended Traefik 502 pattern in `steps/sunbeam_deploy.md` with second occurrence — run 23775401385 (UUID 8f44bea3, tor3-sqa-shared_maas dh1_j8_1); key new finding: Traefik convergence took 13+ minutes after last cluster join (not just seconds), meaning configure must wait well beyond the final join completion.
- **v2.21** (2026-03-31): Added `steps/sunbeam_launch_vm.md` — VM launched (Nova assigned IP) but SSH returns `No route to host` across all 30 retries over ~11 min; OVN/Neutron port wiring or guest cloud-init failure; from run 23688090524 (UUID 6e7c84f2, tor3-sqa-shared_maas dh1_j8_1, branch main, 2026-03-28).
- **v2.22** (2026-03-31): Added Pattern B to `steps/maas.md` — MAAS BMC deduplication causes self-deletion: `machines create` with a known BMC IP returns the existing machine's system ID; FCE's scheduled delete of the "old" ID removes the only machine record; node stuck in `unknown` for full 30-min timeout; from run 23791511796 (UUID a25bcd5f, tor3-sqa-dedicated_maas dh1_j2).
- **v2.23** (2026-03-31): Three improvements: (1) Added pre-flight check — if `outputs/<UUID>-analysis.md` already exists, show summary and ask user before doing any work; (2) launch order when UUID is provided now starts `jobs.json` fetch first (small, unblocks run_id/step triage), then immediately starts Step A bundle download; (3) added explicit prohibition in Step 8 against attributing failures to leftover state from previous runs without direct evidence — substantial cleanup happens between runs, assume a clean environment unless evidence says otherwise.
- **v2.24** (2026-03-31): Updated `steps/sunbeam_test_with_validation_plugin_no_features.md` — document `latest_validation.log` as quick-start entry point; explain how to find the right `validation_<type>_<timestamp>.log` by matching type (quick/smoke/refstack) and timestamp from the GitHub Actions SSH call; note that this log-finding guidance applies to all `sunbeam_test_with_validation_plugin_*` variants.
- **v2.25** (2026-03-31): Added `steps/sunbeam_test_with_validation_plugin.md` — proactive stub for the with-features variant; shares the same action, script, and log layout as `no_features`; no failures recorded yet.
- **v2.26** (2026-03-31): Updated `steps/sunbeam_test_with_validation_plugin_no_features.md` (v1.3) — added `pods_openstack_logs.tgz` to Swift Artifacts table; added Pod Log Patterns section documenting two benign noise patterns: (1) OVN NB/SB DB `SSL_ERROR_ZERO_RETURN` every ~10s (background health-check probe from pod start); (2) Neutron `RowNotFound` for LRPs during parallel Tempest credential setup (OVN sync race condition); confirmed neither explains floating IP failures; extraction recipe added. Also updated the analysis report with full pod log findings.
- **v2.27** (2026-03-31): Updated `steps/sunbeam_test_with_validation_plugin_no_features.md` (v1.4) — added Pattern B: Cilium `--devices='br+,...'` in `2024.1/beta` causes eBPF hooks (`cil_from_netdev`/`cil_to_netdev`) to attach to OVN's `br-ex` external gateway bridge, silently dropping floating IP traffic; not present in `2024.1/stable`; from analysis of `pods_kube-system_logs.tgz` in run 23674710083 (UUID bf85bf5d). Added all four pod-log tarballs to Swift Artifacts table. Updated analysis report with Cilium findings and new recommendations.
- **v2.26** (2026-03-31): Updated Pattern A in `steps/sunbeam_test_with_validation_plugin_no_features.md` — VM console analysis shows east-west OVN (DHCP, metadata proxy) working while only north-south (floating IP via external VLAN) fails; `failed to get user-data` in CirrOS is expected; metadata retry count (3 fails then success) as an OVN programming-lag indicator.
- **v2.28** (2026-04-01): Added Pattern C to `steps/sunbeam_test_with_validation_plugin_no_features.md` (v1.5) — on `2024.1/edge/cilium` with `!br-ex` fix applied, Cilium's BPF hooks on `enp1s0f1` (physnet1 OVS uplink, matched by `enp+`) cause floating IP failures after "UpdatePolicyMaps for all endpoints" event fires (triggered by tempest-0 pod creation at 15:22:35); test-instance FIP created before update worked; all Tempest FIPs after update failed; from run 23847613242 (UUID 03618294, dh1_j9_1, main, 2026-04-01).
- **v2.29** (2026-04-01): Added pattern to `steps/openstack.md` — `octavia-ovn-chassis` hook error (`ovs-vsctl` not found in LXD container); affects only one unit while others are active; silently passes first juju-wait (excluded) then blocks second juju-wait for full 4-hour timeout until SIGKILL; from run 23356105586 (UUID c0a6632d, tor3-sqa-shared_maas dh1_j9_1, branch main, 2026-03-20).
- **v2.30** (2026-04-01): Created new `steps/kubernetes-maas.md` (v1.0) — Pattern: Juju controller connection lost due to virtual infrastructure crash; `health ping timed out` → `connection is shut down` → juju-wait exit code 1 (not SIGKILL, very short duration); no juju_status/crashdump artifacts; SSH to MAAS nodes fails with `kex_exchange_identification` in teardown; exit code 124 on `build_layer_report.py` is cascade; from run 23162727086 (UUID 0551bd1b, tor3-sqa-virtual_maas cluster_4, main, 2026-03-16).
- **v2.31** (2026-04-02): Added Pattern C to `steps/maas.md` — Curtin `install_kernel` fails: apt-get update makes zero network requests in ~250ms (no Squid proxy activity), leaving package database empty; `linux-generic` unfindable; curthooks FAIL; `netboot_off` never called; nodes PXE-loop for 30-minute FCE timeout; Curtin 23.1.1-1124-g7324b43b; noble squashfs 20260223; from run 23910974753 (UUID f8251919, tor3-sqa-virtual_maas cluster_6, 2026-04-02).
- **v2.32** (2026-04-03): Section 4 "Know Your Substrate" — added disambiguation note: `virtual_maas` and `dedicated_maas-dh1_j6` share the same DNS domain; always trust the substrate name in `jobs.json`; node naming is the quick tell: `virtual_maas` uses `infra[1-3]`/`node[1-6]`, `dedicated_maas` uses Pokémon names (e.g. `leafeon`); updated IP→hostname mapping snippet to extract the actual hostname from syslog instead of a `virtual_maas`-only pattern.
- **v2.33** (2026-04-08): Added Pattern F to `steps/maas.md` — boot resource import fails because internal mirror `10.141.186.167` is unreachable from infra KVM VMs (`[Errno 113] No route to host`); FCE's `list_boot_images` readiness check is fooled by stale rack-controller TFTP cache from prior runs; region DB never populated; `machines create` fails immediately with empty architecture list; from run 24122101622 (UUID 01dd77f9, tor3-sqa-virtual_maas cluster_6, MAAS 3.6.4/snap rev 41799, 2026-04-08).
- **v2.35** (2026-04-08): Added "No cilium pod found" pattern to `steps/sunbeam_deploy.md` — node name FQDN vs. short hostname mismatch; `sunbeam-clusterd` checks for Cilium by FQDN from `sunbeam/hostname` label but k8s registers node by short hostname; Cilium was 1/1 Running throughout; from run 24136117775 (UUID 90f21456, dh1_j9_1, main, openstack rev 985, k8s v1.32.11 rev 4754, 2026-04-08).
- **v2.34** (2026-04-08): Added Pattern G to `steps/maas.md` — internal mirror IS reachable but provides incomplete catalog; MAAS import reconciliation deletes existing noble (24.04) commissioning squashfs from region DB; `machines create` succeeds (arch valid) but all nodes immediately fail commissioning with "Missing boot image ubuntu/amd64/no-such-kernel/noble"; `list-boot-images` returns `{'synced'}` (false positive from rack TFTP cache); from run 24090300838 (UUID 16c8a59a, tor3-sqa-virtual_maas cluster_1, snap rev 41799, ADDON maas_snap_nehjoshi5_maas-3.6-next, 2026-04-07).
- **v2.36** (2026-04-08): Added Pattern H to `steps/maas.md` — `install_kvm=True` deploy: cloud-init receives vendor-data but zero .deb downloads; libvirt never installed; no 2nd netboot-finished; likely MAAS race condition with `install_kvm` DB flag; AppArmor definitively not cause; from run 24117574587 (UUID 3830828c, cluster_4). Added Pattern I — Ubuntu never boots after curtin; cross-disk grub layout (grub_device on different disk from /boot); complete silence after iPXE local-boot; unconfirmable without KVM host serial console; from run 24120446627 (UUID 1078296e, cluster_2).
- **v2.37** (2026-04-09): Added Step 5a-i — SSH tunnel health check for `virtual_maas` runs: `generated/sshtest.txt` records ~1-minute periodic SSH connectivity probes through the tunnel; entries are timestamp + hostname (`infra1`) when healthy, SSH error when failed; if tunnel failure is detected, all MAAS log entries after the last successful timestamp are irrelevant (collected from underlying dedicated_maas hosts via fallback, not from this run's virtual cluster); tunnel failure itself is likely the direct cause of any step failure at or after that timestamp. Added `sshtest.txt` to Swift artifacts layout. Learned from run 24154740879 (UUID da912281, tor3-sqa-virtual_maas cluster_5, 2026-04-08): tunnel failed at 21:22:20, juju status hang started at 21:22:47 — tunnel loss was the root cause, not TLS cert corruption.
- **v2.38** (2026-04-08): Created `steps/test_kubeflow.md` (v1.0) — Pattern A: etcd leader election causes `storage is (re)initializing` / HTTP 429; `test_create_profile` fails when listing PodDefaults; `test_kubeflow_workloads` skipped (pytest-dependency); profile deletion stalls (~80s) because finalizers can't reach etcd; all Juju apps `active` at teardown (transient disruption); etcd error in `kfp-schedwf`: `request timed out, possibly due to previous leader failure`; from run 24122089114 (UUID 40826399-eabc-43ea-846c-f4d4f795740f, tor3-sqa-dedicated_maas dh1_j2, main, UATS track/1.10, 2026-04-08).
- **v2.39** (2026-04-09): Added Pattern B (vault `update-status` hook failure) and Pattern C (observability enable timeout — aodh/gnocchi payload containers not ready) to `steps/sunbeam_enable_plugins_all.md`; from runs 24181697345 (UUID e15bca12, cluster_1) and 24180158005 (UUID e8a7e71d, cluster_7), both tor3-sqa-virtual_maas, 2026-04-09. Note: Swift upload also failed in both runs — diagnosis limited to GitHub Actions logs only.
- **v2.40** (2026-04-14): Updated `steps/maas.md` (v1.12) — added Pattern J: `machines create` hangs 15 min on `dedicated_maas` (MAAS 3.7.2); HTTP handler blocks on Temporal `PowerOnWorkflow`; unresponsive IPMI BMC causes indefinite hang; httplib2 900s timeout fires; OAuth expired (901s > 300s threshold) on retry → exit code 2; from run 24420487838 (UUID 6400c87a, dh1_j2, 2026-04-14).
- **v2.41** (2026-04-15): Created `steps/sunbeam_maas_deploy.md` (v1.0) — Pattern A: HA Juju controller `wait-for` timeout; `juju-3` (b4w8xk) took 12m46s to deploy on KVM host `sunset` (vs 7m24s for `juju-2`); cloud-init finished at 15:49:24, 38s after `juju wait-for application controller --timeout 15m` expired; repeated libvirtd I/O errors on `sunset` during VM startup suggest transient storage contention; from run 24403605251 (UUID 69c781c2, tor3-sqa-dedicated_maas dh1_j2, openstack rev 945, 2026-04-14).
- **v2.42** (2026-04-15): Updated `steps/sunbeam_maas_deploy.md` (v1.1) — Pattern A confirmed in two additional runs (UUIDs f3d1c1f9, e9de9830, same substrate/cluster dh1_j2); all three linked to bug lp:openstack:2148312.
- **v2.43** (2026-04-15): Updated `steps/sunbeam_maas_deploy.md` (v1.4) — sixth confirmed occurrence of Pattern A, UUID 0b8b31f3, run 24478433479, dedicated_maas dh1_j2; juju-3 cloud-init finished 42s before deadline but Juju agent registration did not complete in time; no I/O errors in QEMU logs.
- **v2.44** (2026-04-21): Added Pattern B to `steps/sunbeam_launch_vm.md` (v1.1) — Nova BUILD timeout: VM never reached ACTIVE (distinct from Pattern A where VM is ACTIVE but SSH unreachable); `sunbeam launch` polls ~6m15s then reports "Timeout waiting for Server:<id> to transition to ACTIVE"; misleading "Please run sunbeam configure first" is a generic error; from run 24664528578 (UUID a44c1e26, tor3-sqa-testflinger cluster_2, main, 2026-04-20). Swift unavailable during analysis.
- **v2.45** (2026-04-26): Input simplified to UUID only (GitHub Run ID is no longer accepted from user; always obtained from `jobs.json` in Swift). Added Source Repositories table: workflow source in `~/sqa-cloud-deployment-pipeline/`, fce source in `~/cpe/foundation/`. Added hard stop on Swift MCP unavailability — no workarounds, report and stop. Renumbered steps 3→2, 4→3, 5→4, 6→5, 7→6, 8→7.
- **v2.46** (2026-04-26): Added `steps/microk8s.md` (v1.0) — Pattern A: Juju machine agent `down` while unit workload completes successfully; machine agent fails to maintain heartbeat with controller while unit sub-process runs normally; `juju-wait --machine-error-timeout 1800` fires after 30 min; from run 24889818217 (UUID 951e7528, tor3-sqa-virtual_maas cluster_3, Juju 4.0.8, 2026-04-24).
- **v2.47** (2026-04-29): Added `steps/juju_k8s_controller.md` (v1.1) — Pattern A: Juju 4.0.9 (`4.0/candidate`, snap rev 34852) fails to bootstrap on AKS with `invalid reference format` in `podcfg.tagImagePath`; root cause is a snap build defect in commit `29278b68a4` (2026-04-16): `JujudOCINamespace` changed from constant to variable for linker injection, but `snapcraft.yaml` passes it as empty string → `imageRepoToPath` constructs `/jujud-operator` (leading slash) → `reference.Parse` rejects it; `4.0/stable` unaffected (pre-dates the commit); from runs 25087533337 (UUID b4c9324a) and 25098199361, ext-sqa-aks useast, 2026-04-29.
- **v2.48** (2026-05-04): Added `cinder-volume` install hook / parallel joins pattern to `steps/sunbeam_deploy.md` — `cinder-volume/N` on a concurrently-joining peer node runs its install hook for >20 min (`(amqp) integration missing`); another node's internal 1200s `juju wait-for` expires → exit code 1 false failure; node IS present in cluster list as active; unit self-heals after timeout; linked to LP bug #2121929; from run 25179144394 (UUID a1c69781, tor3-sqa-testflinger cluster_2, 2024.1/beta, 2026-04-30).
- **v2.50** (2026-05-06): Added Pattern E to `steps/magpie.md` — MongoDB primary election during juju-wait (not juju deploy); renders Juju API completely unreachable for ~5–6 min; juju-wait exits immediately due to `--retry_errors 0`; subsequent juju status → `no reachable servers`; version collector confirms via `connection is shut down`; controller self-recovers; SSH tunnel healthy throughout; from run 25455244438 (UUID 965bc456, tor3-sqa-virtual_maas cluster_4, main, 2026-05-06).
- **v2.51** (2026-05-19): Added `steps/juju_sunbeam_controller.md` — Juju bootstrap VM reached by SSH and completed cloud-init, but `jujud` timed out reaching the Sunbeam public Keystone VIP from inside the guest (`dial tcp 10.241.36.134:443: i/o timeout`) while runner-side OpenStack and Tempest API calls still succeeded; from run 25987960567 (UUID 8e5756a7-2999-4969-8fd2-083e7612bb5f, tor3-sqa-shared_maas dh1_j8_1, branch aipoc, 2026-05-17).
- **v2.52** (2026-05-19): Updated `steps/sunbeam_deploy.md` with a new false-negative control-plane join pattern — `sunbeam.steps.k8s._get_k8s_node_to_update` cannot find joining nodes 32/34/36 in the deployment-labelled K8S node list, raising `Failed to get k8s nodes to update` even though the nodes later appear Ready; from run 25987960158 (UUID 9407930c-870f-4485-9768-8211e5ff610c, tor3-sqa-shared_maas dh1_j9_1, branch aipoc).
- **v2.53** (2026-05-19): Updated `steps/sunbeam_enable_plugins_all.md` with a new telemetry timeout pattern from run 25970616074 (UUID 2797dd8d-b892-43c2-9cde-391cdf3eb610): `sunbeam enable telemetry` waited 900s because `openstack-exporter/0` stayed in `(logging) integration incomplete` while `opentelemetry-collector/0` failed to publish Loki endpoint data on relation 269, even though collector pods were running.
- **v2.54** (2026-05-19): Updated `steps/juju_sunbeam_controller.md` with another confirmation of the same bootstrap-guest reachability pattern from run 25963558861 (UUID a228817a-7181-4b2b-ab2f-15205ea19ff9): `admin.openrc` used `https://10.241.38.134/openstack-keystone/v3` (`OS_INTERFACE=public`), refstack validation still returned `200` responses from that VIP, the runner had an `sshuttle` route for the `10.241.38.13x/32` API addresses, and the bootstrap VM later timed out to `10.241.38.134:443` from tenant address `192.168.122.89` on `tor3-sqa-shared_maas` / `dh1_j9_1`.
- **v2.55** (2026-05-19): Updated `steps/sunbeam_deploy.md` with a second confirmation of the 1200s parallel-join false-negative pattern on `tor3-sqa-testflinger` cluster_3 — `fizeau.maas` (`control,storage`) timed out in `sunbeam cluster join`, but final cluster/K8S/Juju snapshots showed it fully joined and later machines were still being admitted after the timeout; from run 25948797819 (UUID f846183c-519b-4dc5-affd-611837fb05b1, branch `main`).
- **v2.56** (2026-05-19): Updated `steps/sunbeam_deploy.md` with a third confirmation of the same parallel-join false-negative family on `tor3-sqa-testflinger` cluster_3 — `barbos.maas` (`control,compute`) timed out after 1800s in `sunbeam cluster join`, but the timeout snapshot already showed `barbos` active in Juju while a later node's `openstack-hypervisor` hook was still executing, and final cluster/K8S snapshots showed `barbos` fully joined; from run 25940588751 (UUID 72cea7f3-5a7d-4baa-ac17-ebb88051fc40, branch `main`).
- **v2.57** (2026-05-19): Updated `steps/sunbeam_deploy.md` with a fourth confirmation of the same `tor3-sqa-testflinger` cluster_3 parallel-join false-negative family — `napple.maas` timed out first after 1200s and `fizeau.maas` later surfaced the fatal `CalledProcessError` after 1800s, but final cluster/K8S/Juju snapshots showed both nodes fully joined and all relevant units recovered to `active idle`; from run 25928796613 (UUID 02c1a007-6d9c-4a94-b412-12f62b3bceb8, branch `main`).
- **v2.58** (2026-05-19): Updated `steps/sunbeam_deploy.md` with a fifth confirmation of the same `tor3-sqa-testflinger` parallel-join false-negative family — `ancientminister.maas` (`control,compute`) timed out after 1800s on cluster_1 while `cinder-volume/3`, `cinder-volume-ceph/3`, and `openstack-hypervisor/3` were still converging, but final cluster/K8S/Juju snapshots showed `ancientminister` fully joined and the model recovered; from run 25932491932 (UUID a14f61b1-6b73-400c-9e54-5761c4e197e9, branch `main`).
- **v2.59** (2026-05-19): Updated `steps/sunbeam_deploy.md` with a new configure-time race pattern from run 25921180840 (UUID 72595a1b-fa63-4f49-9917-5bcae8737c4c): `deploy_sunbeam.py` starts `sunbeam configure` immediately after `cluster resize` with no wait for `openstack` model convergence; `neutron-1` on the newly joined control-plane node was created at 16:27:23Z but only became Ready at 17:03:00Z, and post-failure status showed `neutron/1` blocked with `(container:neutron-server) healthcheck failed: online`.
- **v2.60** (2026-05-19): Updated `steps/sunbeam_maas_deploy.md` with a new false-negative timeout pattern from run 25950047554 (UUID 543ceda1-ee88-4631-ad00-8604348744c6): `sunbeam cluster deploy` hit its hardcoded 1800s wait timeout on `tor3-sqa-dedicated_maas` / `dh1_j2`, but the timeout snapshot already showed `openstack-machines` fully active and post-failure status collection showed `openstack-infra`, `openstack-machines`, and `openstack` all active.
<<<<<<< Updated upstream
=======
- **v2.73** (2026-05-25): Updated `steps/sunbeam_test_with_validation_plugin_no_features.md` (v1.6) with a new `virtual_maas` tunnel-loss pattern from run 26208199607 / UUID 7862b0c0-1e97-4c96-9d50-abf1f14a645b: `quick` and `smoke` validation both passed, but the runner lost SSH connectivity to the bootstrap node during `sunbeam validation run refstack`; `generated/sshtest.txt` showed the last successful probe at 09:53:38 UTC followed by `kex_exchange_identification: Connection closed by remote host`.
- **v2.74** (2026-05-25): Updated `steps/sunbeam_enable_plugins_all.md` (v1.13) with a new `virtual_maas` tunnel-loss pattern from run 26208201711 / UUID 553ad773-f235-4643-aa06-c7bc6fe4c94b: `sunbeam enable -m manifest.yaml observability embedded` died with SSH exit `255` after the tunnel dropped; `generated/sshtest.txt` shows the last successful probe at 09:53:32 UTC followed by `kex_exchange_identification`, and post-failure version collection could no longer reach `10.241.144.2`. 
- **v2.75** (2026-05-27): Added `steps/setup_virtual_lab.md` (v1.0) — Pattern A: Testflinger relay host and `virsh start infra1/2/3` succeed, but `setup-tunnel` never reaches `10.241.144.2` over `sshuttle`; 4 `ssh-keyscan` retries exhaust over ~4 minutes, and later `collect-logs` also cannot SSH to `10.241.144.2`; from run 26503617854 (UUID 856857aa-ec36-40d5-adfc-03b67a0b5959, tor3-sqa-virtual_maas cluster_5, branch main).
- **v2.76** (2026-05-27): Updated `steps/sunbeam_deploy.md` (v2.18) with a new bootstrap-node Snap Store outage pattern from run 26462983826 (UUID ce4bafb6-62cf-42d1-a3ea-56ecf9b59752): the very first remote command in `deploy_sunbeam.py` failed because `sudo snap install openstack --channel 2024.1/beta` on `solqa-shared-maas-server-31.maas` got HTTP 408 from `api.snapcraft.io/v2/snaps/refresh`; no Sunbeam cluster artifacts were produced and later `juju models` errors were only log-collection cascades.
- **v2.77** (2026-05-27): Updated `steps/sunbeam_deploy.md` (v2.19) with a new bootstrap-time Juju controller charm download failure pattern from run 26457590888 / UUID d489a2f8-1786-4909-888f-3ad1ddcc16e3: `sunbeam prepare-node-script --bootstrap` installed `openstack` and `juju`, passed its direct `api.charmhub.io` reachability check, then failed bootstrapping Juju because the `juju-controller` charm download from `canonical-bos01.cdn.snapcraftcontent.com` hit `net/http: TLS handshake timeout`; no cluster-status artifacts were created and later `juju models` errors were only collection cascades.
- **v2.78** (2026-05-27): Added `steps/prepare_shared_maas_clusters_for_deployment.md` (v1.0) — `shared_maas` resource-pool preparation fails in the `Release Machines` substep because `maas sqa-bot machines read pool=sqa-<cluster>` returns a truncated HTTP body and the CLI exits with `IncompleteRead(... more expected)` / exit code 2 before any machine release or partitioning occurs; from run 26455309054 (UUID ba7fec2e-a8fc-4ef3-af81-a4cddb368035, tor3-sqa-shared_maas dh1_j9_1, branch main).
- **v2.79** (2026-05-27): Updated `steps/sunbeam_deploy.md` (v2.20) with a new `tor3-sqa-shared_maas` parallel-join false-negative pattern from run 26435144943 / UUID f14615e7-bfef-40ce-97df-7cb515cdb7ad: three joins hit the internal 1800s wait deadline while storage/backend and certificate hooks were still converging, yet a queued later join still completed and final cluster/Juju snapshots showed full recovery.
- **v2.80** (2026-05-27): Updated `steps/prepare_shared_maas_clusters_for_deployment.md` (v1.1) with a new `MAAS Login` hang pattern from run 26440193596 / UUID 9566a408-3d65-4f7c-9091-6e7d1ddaa52e: `maas login sqa-bot` against `http://10.239.7.110/MAAS` produced no further output for ~4h45 and the runner eventually killed it with exit code 143 before `Release Machines` began.
- **v2.86** (2026-05-27): Updated `steps/setup_virtual_lab.md` (v1.5) with a fourth confirmation of the tunnel-drop-after-initial-success pattern from run 26444927960 / UUID 73a69f8d-10c0-4d23-b889-d3c8db2c3a79: relay `10.241.3.48` started `infra1/2/3`, SSH reachability to `10.241.144.2/.3/.4` succeeded, `generated/sshtest.txt` captured one successful `infra1` probe followed by `Connection closed by 10.241.144.2 port 22`, and `maas login` degraded from `IncompleteRead(380435 bytes read, 158769 more expected)` to repeated `[Errno 113] No route to host` before failing after 10 attempts.
- **v2.85** (2026-05-27): Updated `steps/setup_virtual_lab.md` (v1.4) with a new one-node SSH validation failure pattern from run 26444917865 / UUID 6074c4cd-b457-489c-9805-1b68eff262e4: relay `10.241.3.43` and `virsh start infra1/2/3` succeeded, `sshuttle` reached `10.241.144.2` and `.3`, but `10.241.144.4` repeatedly returned `Connection closed by remote host` and never passed `ssh-keyscan`, so `setup-tunnel` exited before `maas login`.
- **v2.84** (2026-05-27): Updated `steps/setup_virtual_lab.md` (v1.3) with a third confirmation of the tunnel-drop-after-initial-success pattern from run 26444924273 / UUID 40f03deb-1fcf-4f83-8ad3-055125ddac10: relay `10.241.6.29` started `infra1/2/3`, SSH reachability to `10.241.144.2/.3/.4` succeeded at 10:26:43Z, `maas login` then degraded from `IncompleteRead(5954 bytes read, 533250 more expected)` to repeated `[Errno 113] No route to host`, and `generated/sshtest.txt` captured one successful probe followed by `Connection closed by 10.241.144.2 port 22`.
- **v2.83** (2026-05-27): Updated `steps/setup_virtual_lab.md` (v1.2) with another confirmation of the tunnel-drop-after-initial-success pattern from run 26444920976 / UUID 5147eb17-151f-4eb5-b59f-eaed68901db8: relay `10.241.4.13` started `infra1/2/3`, SSH reachability to `10.241.144.2/.3/.4` eventually succeeded, then `maas login` degraded from HTTP 502 to `IncompleteRead` to repeated `[Errno 113] No route to host`; `generated/sshtest.txt` captured one successful probe followed by `Connection closed by 10.241.144.2 port 22`.
- **v2.82** (2026-05-27): Updated `steps/setup_virtual_lab.md` (v1.1) with a new tunnel-drop-after-initial-success pattern from run 26444934886 / UUID fdb17b26-7267-48db-b674-ccb0b863055b: SSH scans for `10.241.144.2/.3/.4` succeeded, but `generated/sshtest.txt` showed the tunnel died immediately afterwards and `maas login` degraded from HTTP 502 to `IncompleteRead` to repeated `[Errno 113] No route to host`.
- **v2.81** (2026-05-27): Updated `steps/sunbeam_prepare_env.md` (v1.2) with a new bootstrap-node Snap Store timeout pattern from run 26444914683 / UUID 4c95aea6-4e93-4bd5-8703-c3747f93f640: `clean_disks.py` failed because `snap install microceph --channel squid/candidate` on the freshly provisioned node hit `Fetch and check assertions for snap "snapd"` → HTTP 408 from the Snap Store.
- **v2.87** (2026-05-27): Updated `steps/sunbeam_launch_vm.md` (v1.3) with a new false-negative pattern from run 26401300048 / UUID b0948bbd-e549-4004-8668-993513adf7b0: the runner-side `ssh ... sunbeam launch ... | grep "ubuntu@"` exited 255 and disconnected from the bootstrap node, but the remote `sunbeam launch` command continued, created server `1de70608-65f8-44a7-9a04-13e26c7dc19e`, reached `ACTIVE`, and associated floating IP `10.243.37.136`.
- **v2.88** (2026-05-27): Updated `steps/maas.md` (v1.15) with a new dedicated_maas false-negative pattern from run 26388147932 / UUID 59a74d0f-d147-4b31-969c-d1c24a20259a: during `maas:compose_vms`, runner-side snapped `maas` CLI calls (`machines read`, later `version read`, `rack-controllers read`, `machines read hostname=quilava`) hung locally until timeout/SIGKILL even though mienfoo's MAAS logs showed matching HTTP 200 responses immediately; snapshot confirmed the composed VMs were already `Ready`, so the failure was in the client on the runner, not the MAAS API.
- **v2.89** (2026-05-27): Updated `steps/sunbeam_enable_plugins_all.md` (v1.14) with a new dedicated_maas false-negative pattern from run 26388146493 / UUID c7593d1c-59c9-47dd-ba7a-51aac3cce2d0: `sunbeam enable secrets` printed `OpenStack secrets application enabled.` then exited with `('Connection aborted.', RemoteDisconnected('Remote end closed connection without response'))`, while snapshot Juju status showed the openstack model already fully active/idle; root cause is a post-success client-side HTTP disconnect, not a failed secrets deployment.
- **v2.90** (2026-05-27): Updated `steps/sunbeam_maas_deploy.md` (v1.10) with a new configure-time false-negative pattern from run 26388151075 / UUID f7410bbd-27e3-442c-9df6-885815701650: `sunbeam configure` failed with explicit `terraform command failed (state locked)` on tor3-sqa-dedicated_maas / dh1_j2 even though post-failure Juju and Kubernetes snapshots showed all Sunbeam models healthy.
- **v2.91** (2026-05-27): Added `steps/redeploy_dedicated_maas_infra_nodes.md` (v1.0) — dedicated MAAS infra redeploy failure where `maas_instance` creation completed for `anahuac` but the host never became SSH reachable on either its `.maas` address or later static infra IP, so both the step and later log collection timed out; from run 26388149930 (UUID 0cae049e-3e7d-44d5-b9d8-5546a97ec55a, tor3-sqa-dedicated_maas dh1_j2, branch `aipoc`).
- **v2.94** (2026-05-30): Added Pattern 15 to `steps/sunbeam_enable_plugins_all.md` (v1.15) — Juju CMR event delivery failure: `loki/0` (observability model) never received `logging-relation-*` hooks despite `loki-logging` offer showing `2/2` connected; `opentelemetry-collector` never received Loki URL via `send-loki-logs-relation-joined/changed` (those events never fired); `openstack-exporter/0` permanently stuck in `(logging) integration incomplete`; 900s `sunbeam enable telemetry` timeout; from run 26672262187 (UUID 274bb7c7-8088-4c75-93e2-ea5f72281911, tor3-sqa-testflinger cluster_1, Juju 3.6.23, branch main, 2026-05-30).
- **v2.92** (2026-05-30): Updated `steps/sunbeam_deploy.md` with Pattern 18 — bootstrap 3600s wait timeout where `ovn-central/0` permanently blocked after `check_pebble_handlers_ready()` timed out calling `can_connect()` on a sidecar Pebble socket during `ovsdb-cms-relation-changed`; pod was 4/4 Running with 0 restarts, indicating transient resource-contention-driven socket unresponsiveness rather than a service crash; from run 26680401911 (UUID 23f5cdb9-da4a-41c1-9fe4-b5ce5e049733, tor3-sqa-testflinger cluster_1, branch `main`, 2026-05-30).
- **v2.93** (2026-05-30): Updated `steps/sunbeam_deploy.md` (v2.22) — eighth confirmation of the parallel-join false-negative family; `mouser.maas` and `avery.maas` both timed out after 1800s with `k8s/0` mid "Allocating Control_Plane Cluster tokens" at deadline; remaining nodes completed after timeout; full cluster convergence confirmed; from run 26678355136 (UUID 7b8f6054-d5b2-4b45-b452-d1cf76574e92, tor3-sqa-testflinger cluster_2, branch `main`, 2026-05-30).
- **v2.95** (2026-05-30): Updated `steps/sunbeam_deploy.md` (v2.23) with a new premature-configure / Neutron ingress EOF pattern from run 26665920907 (UUID 3d5b2587-f94d-40e4-b90a-f640e3e43739): `sunbeam configure` started immediately after the final cluster-list success on `tor3-sqa-testflinger` cluster_3, but the bootstrap node's terraform apply failed creating `openstack_networking_router_interface_v2.user_router_interface` because the Neutron API behind `traefik-public` returned `OpenStack connection error, retries exhausted. Last error was: EOF`, while Juju still logged Traefik `Provider not ready` messages for Neutron (port 9696) throughout the configure window.
- **v2.96** (2026-06-02): Updated `steps/sunbeam_deploy.md` (v2.24) with a new `sunbeam cluster resize` timeout pattern from run 26662396210 (UUID c9f97030-6c7a-4272-81b3-685ab5acad99): all nodes joined and `openstack-machines` converged on `tor3-sqa-testflinger` / `cluster_1`, but `nova/1` never left Bootstrapping because Nova services on the new unit hit repeated MySQL-router `ECONNREFUSED` plus Placement/Keystone HTTP 502/500 failures, causing the 3600s resize wait to expire.
- **v2.97** (2026-06-02): Updated `steps/sunbeam_deploy.md` (v2.25) with a ninth confirmation of the `tor3-sqa-testflinger` parallel-join false-negative family on cluster_3 — `euler.maas` and `cajal.maas` both hit the internal 1800s `sunbeam cluster join` wait timeout while `k8s/2` was still waiting for a cluster token and `openstack-hypervisor/2` was still converging, but later joins still finished and final Sunbeam/Juju/Kubernetes snapshots showed full cluster convergence; from run 26657349467 (UUID 87f87610-4c3f-4b6d-ad7e-7c17b5d2483a, branch `main`, 2026-05-29).
- **v2.99** (2026-06-03): Updated `steps/maas.md` with Pattern 14 — Intel IOMMU (DMAR) DMA mapping bug corrupting bcache reads on HP ProLiant DL320e Gen8 (`anahuac`), causing deterministic `SIGSEGV` across multiple userspace binaries including `ModemManager`, `snapd`, and `snap` (crashing in `godebug.init.0()` with nil pointer dereference); from run 26719477705 (UUID 5eb8d383-dd43-47da-9175-481d8e62aa89).
- **v2.98** (2026-06-02): Updated `steps/sunbeam_deploy.md` (v2.26) with an 1800s cluster_2 confirmation of the storage-convergence false-negative variant — `ditto.maas` (`control,compute`) timed out in `sunbeam cluster join` while `cinder-volume/3`, `cinder-volume-ceph/3`, and `openstack-hypervisor/3` were still converging, but later joins completed and final cluster/Juju snapshots showed full recovery; from run 26664595605 (UUID 1e9216b1-618d-4c01-89d5-0b358b6c6fdb, branch `main`, 2026-05-29).
>>>>>>> Stashed changes
