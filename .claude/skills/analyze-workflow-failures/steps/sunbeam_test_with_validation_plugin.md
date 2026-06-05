# Step Knowledge: sunbeam_test_with_validation_plugin

## Step Overview

Runs Sunbeam's built-in validation suite against the deployed OpenStack cluster — **with**
optional plugins enabled (passed via `extra_args`). Uses the same action and script
(`products/sunbeam/test_with_validation_feature.py`) as
`sunbeam_test_with_validation_plugin_no_features`; the only difference is the layer name and
which features/plugins `extra_args` activates before running validation.

Validation types that may run: `quick` (API-only, ~23 tests), `smoke` (~129 tests),
`refstack`. Which types are executed depends on the SKU's `extra_args`.

## Swift Artifacts

Objects stored under `<uuid>/generated/sunbeam/` that are useful for diagnosing failures:

| Path | Description | When to check |
|---|---|---|
| `generated/sunbeam/latest_validation.log` | Copy of most recently completed validation run | Start here — fastest entry point |
| `generated/sunbeam/validation_<type>_<timestamp>.log` | Timestamped log for each validation type run | Match type and timestamp to find the right log (see below) |
| `generated/sunbeam/output.log` | Step script DEBUG log | Step execution context, plugin enable/disable output |
| `generated/sunbeam/juju_status_openstack.txt` | Juju model status snapshot | Check for non-active units |
| `generated/sunbeam/juju_debug_log_openstack.txt` | Juju model debug log | Charm errors during test window |
| `generated/sunbeam/kubectl_get_pod.txt` | K8s pod list | Check for CrashLoopBackOff or Pending pods |

## Key Log Files

No tgz archive for this step — all logs are flat files in `generated/sunbeam/`.

### Finding the right validation log

Each `sunbeam validation run <type>` call writes to a timestamped file:
```
generated/sunbeam/validation_<type>_<timestamp>.log
```
where `<type>` is `quick`, `smoke`, or `refstack`, and `<timestamp>` matches the wall-clock
time the command was launched (visible in the GitHub Actions log as the SSH call timestamp).

`latest_validation.log` — if present — is a copy of the **most recently completed** validation
run (whatever type that was). It is the fastest starting point when only the last run matters.

**How to locate the right log:**
1. Check `latest_validation.log` first — if the step ran only one validation type it will be
   the one that failed.
2. If multiple types ran, find each by type:
   ```bash
   ls <work_dir>/<uuid>/generated/sunbeam/validation_*.log
   ```
3. Confirm the right file by matching its timestamp suffix to the SSH call timestamp in the
   GitHub Actions log:
   ```bash
   # GitHub log shows: sunbeam validation run smoke --output validation_smoke_2026-03-28T041135Z.log
   # → use validation_smoke_2026-03-28T041135Z.log
   ```

Each validation log contains full Tempest output:
- Per-test results (`ok`, `FAILED`, `SKIPPED`)
- Captured Python tracebacks on failure
- Captured `pythonlogging` section with OpenStack API call history
- `Totals` summary at the end

## Grep Patterns

```bash
UUID=<uuid>
DIR=<work_dir>/${UUID}/generated/sunbeam

# Quick start: check latest_validation.log if it exists
[ -f ${DIR}/latest_validation.log ] && grep -E "FAILED|Ran:|Failed:" ${DIR}/latest_validation.log

# List all validation logs and their types
ls ${DIR}/validation_*.log 2>/dev/null

# Summary from every validation log (shows which types ran and their outcomes)
grep -E "Ran:|Passed:|Failed:|Skipped:" ${DIR}/validation_*.log

# Show all failed tests in smoke log
grep "FAILED" ${DIR}/validation_smoke_*.log

# Get full failure block for a specific test
grep -A 40 "<TestClassName>.*FAILED" ${DIR}/validation_smoke_*.log

# Check quick validation result
tail -20 ${DIR}/validation_quick_*.log

# Look for SSH or ping timeout in smoke log
grep -i "timeout\|reachable\|ping\|ssh" ${DIR}/validation_smoke_*.log | grep -i "fail\|error\|timed"

# Check refstack log if present
[ -f ${DIR}/validation_refstack_*.log ] && tail -30 ${DIR}/validation_refstack_*.log

# Check which plugins/features were enabled (from step DEBUG log)
grep -i "enable\|plugin\|feature" ${DIR}/output.log | head -20
```

## Known Failure Patterns

> No failures recorded yet for this step variant. See
> `steps/sunbeam_test_with_validation_plugin_no_features.md` for known patterns — the failure
> modes (floating IP unreachable, SSH timeout, etc.) are the same across both variants.

---

_Add patterns below as they are discovered._

## Notes

- Same action and script as `sunbeam_test_with_validation_plugin_no_features` —
  `products/sunbeam/test_with_validation_feature.py` — only `extra_args` differs
- Check `output.log` to see which plugins were enabled before validation ran; a plugin
  that failed to enable could change which tests are available or affect the data plane
- This step runs on shared MAAS substrates — no MAAS logs are available
- "quick" validation (API-only) always runs before "smoke"; if quick passes but smoke fails,
  the data plane is the suspect (VMs/networking), not the API layer

