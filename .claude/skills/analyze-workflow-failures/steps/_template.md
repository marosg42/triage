# Step Knowledge: <step_name>

## Step Overview

Brief description of what this pipeline step does and what infrastructure it touches.

## Swift Artifacts

Objects stored under `<uuid>/generated/<layer>/` that are useful for diagnosing failures:

| Path | Description | When to check |
|---|---|---|
| `generated/<layer>/log.txt` | FCE build log for this layer | Always — first stop |
| `generated/<layer>/logs-<timestamp>.tgz` | Full infrastructure log archive | Deep investigation |

List any other relevant Swift paths under this UUID.

## Key Log Files (inside tgz archive)

| File | What it contains | When to use |
|---|---|---|
| `var/log/syslog` | System and service events | Primary investigation |
| `var/log/<service>.log` | Service-specific log | Service-specific errors |

## Grep Patterns

Useful search strings for this step's failure modes:

```bash
# Find the primary error
grep -rn "<error_pattern>" <work_dir>/maas-logs/*/var/log/syslog

# Check service status around failure time
grep -h "<timestamp_prefix>" <work_dir>/maas-logs/*/var/log/syslog | grep "<service>"

# Find relevant events
grep -h "<keyword>" <work_dir>/maas-logs/*/var/log/ | sort
```

## Known Failure Patterns

### Pattern 1: <short name>

**Symptom:**
```
<error message as seen in GitHub Actions logs>
```

**Root cause:** <one paragraph explanation>

**Evidence to look for:**
- `<log file>`: `<what you expect to see>`
- `<log file>`: `<what you expect to see>`

**See also:** `patterns/<pattern-file>.md` for full detail.

---

_Add more patterns below as they are discovered._

## Notes

- Any deployment-specific quirks for this step
- Which infra node typically has the relevant logs
- Any known timing issues or race conditions

## Version History

- **v1.0** (<date>): Initial version
