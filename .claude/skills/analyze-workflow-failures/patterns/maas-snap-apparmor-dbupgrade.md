# Pattern: MAAS Snap AppArmor dbupgrade Failure

## Summary

MAAS snap auto-refresh triggers a `post-refresh` hook that runs `maas-region dbupgrade`.
The AppArmor profile for `snap.maas.hook.post-refresh` denies `python3` read access to
`/etc/gss/mech.d/` (GSSAPI/Kerberos library init path). This causes the dbupgrade script
to crash silently without migrating the DB schema. MAAS then runs on a schema that is
missing tables the new revision needs.

## Symptom (in GitHub Actions logs)

```
ServerError: 400 Bad Request (Failed to render preseed: relation "maasserver_routable_pairs" does not exist)
```

Appears during `terraform apply` in any step that calls the MAAS API to provision machines.

## Two Variants

### Variant A — Hook timed out, snap rolled back

**What happened:**
- `post-refresh` hook started and hung
- snapd killed it after exactly 10m0s
- Snap rolled back to old revision
- But the old revision *also* queries `maasserver_routable_pairs` → errors may predate refresh

**Log signals (syslog):**
```
snapd taskrunner.go: ... run hook "post-refresh": <exceeded maximum runtime of 10m0s>
```
AppArmor denial appears early in hook window, then syslog goes quiet for ~10 minutes, then rollback.

Hook duration: **exactly 10m0s** = killed by snapd.

**Observed in:** run 23388633087 (cluster_6)

---

### Variant B — Hook exited early, refresh "succeeded" but migration skipped

**What happened:**
- `post-refresh` hook started
- AppArmor denied `/etc/gss/mech.d/` ~10s in — dbupgrade Python process crashed on startup
- Hook exited fast (exit code 0 from the shell wrapper, even though the Python script failed)
- snapd considered refresh successful; new MAAS revision started
- DB schema never migrated; failure occurs minutes to hours later on first preseed request

**Log signals (syslog):**
```
kernel: audit: type=1400 ... apparmor="DENIED" operation="open" profile="snap.maas.hook.post-refresh" name="/etc/gss/mech.d/" ...
```
Appears ~10s after hook start. Hook completes in **<30s** total — suspiciously fast for a DB migration.

Hook duration: **<30 seconds** = suspicious; real DB migrations take longer.

**Observed in:** runs 23377136139 (cluster_2) and 23406159064 (cluster_4)

---

## How to Confirm

### 1. Find the refresh event
```bash
grep -h "auto-refresh\|post-refresh\|41404\|taskrunner" <work_dir>/maas-logs/*/var/log/syslog | sort
```

### 2. Measure hook duration
Find lines like:
```
2026-03-21T11:40:28  infra3 snap[...]: ... Doing: run-hook (post-refresh) ...
2026-03-21T11:40:42  infra3 snapd[...]: ... hook "post-refresh" done
```
Duration < 30s → Variant B. Duration = 10m0s → Variant A.

### 3. Find AppArmor denial
```bash
grep -h "DENIED.*post-refresh\|post-refresh.*DENIED" <work_dir>/maas-logs/*/var/log/syslog | sort
```
Expected line:
```
kernel: audit: ... apparmor="DENIED" operation="open" profile="snap.maas.hook.post-refresh" name="/etc/gss/mech.d/" ...
```

### 4. Confirm missing table in DB schema
```bash
grep "maasserver_routable_pairs" <work_dir>/maas-logs/*/var/snap/maas/common/log/dump.dmp
```
If no output → table was never created → migration was skipped.

### 5. Correlate with HAProxy (Variant A only)
```bash
grep -h "maas-api" <work_dir>/maas-logs/*/var/log/haproxy.log | sort
```
During rollback, backends cycle DOWN → UP.

## Relevant Log Files

| File | What to look for |
|---|---|
| `var/log/syslog` | Snap hook events, AppArmor denials, hook start/end times |
| `var/log/haproxy.log` | Backend UP/DOWN during refresh window (Variant A) |
| `var/snap/maas/common/log/dump.dmp` | Confirm `maasserver_routable_pairs` table absence |
| `var/log/postgresql/postgresql-16-ha.log` | WAL writes during migration (should be noisy if migration ran) |

## Root Cause

AppArmor profile bug in the MAAS snap package. The `snap.maas.hook.post-refresh` profile
does not allow the hook to read `/etc/gss/mech.d/`, which Python's GSSAPI/Kerberos bindings
attempt to open at import time. This causes `maas-region dbupgrade` to crash before any
migration runs. The snap hook framework doesn't detect this as a failure in Variant B because
the hook's shell wrapper exits cleanly.

