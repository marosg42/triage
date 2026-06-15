# Step Knowledge: landscape

## Step Overview

The `landscape` step deploys the Landscape Server charm and establishes its database and message queue relations. It configures and bootstraps the Canonical Landscape service (landscape-server) and performs database schema initialization/migration on top of PostgreSQL.

## Swift Artifacts

Objects stored under `<uuid>/generated/landscape/` that are useful for diagnosing failures:

| Path | Description | When to check |
|---|---|---|
| `generated/landscape/log.txt` | FCE build log for this layer | Always — first stop |
| `generated/landscape/juju-crashdump-landscape-<timestamp>.tar.gz` | Juju crashdump containing unit and machine logs | Deep investigation when Juju wait timeouts occur |
| `generated/landscape/juju-dump-db-landscape-<timestamp>.tar.gz` | Landscape DB state backup | DB/schema migration issues |

## Key Log Files (inside crashdump)

Inside the `juju-crashdump-landscape-<timestamp>.tar.gz` archive, key files are:

| File | What it contains | When to use |
|---|---|---|
| `landscape-server_0/var/log/juju/unit-landscape-server-0.log` | Landscape Server unit Juju logs | Hook execution failures (e.g., db-relation-joined) |
| `postgresql_0/var/log/juju/unit-postgresql-0.log` | PostgreSQL unit Juju logs | Database provisioning/access failures |
| `debug_log.txt` | Combined debug log for all units | Overview of chronological unit errors |

## Grep Patterns

Useful search strings for this step's failure modes:

```bash
# Search for Juju wait failures or SIGKILLs in the FCE build log
grep -E "Traceback|ERROR|CalledProcessError" files/<uuid>/generated/landscape/log.txt

# Search for hook tracebacks or SRE module mismatches inside unit logs
grep -E "ERROR|Traceback|AssertionError|SRE module mismatch" files/<uuid>/crashdump/2/baremetal/var/log/juju/unit-landscape-server-0.log
```

## Known Failure Patterns

### Pattern 1: SRE module mismatch (Python version conflict)

**Symptom:**
In `landscape/log.txt`, the FCE build log fails due to `juju-wait` command hitting its 4-hour timeout (14400 seconds) and being killed with `SIGKILL` (signal 9).

Inside `unit-landscape-server-0.log` (under `/var/log/juju/` in the crashdump), you see:
```
2026-06-13 03:53:50 INFO unit.landscape-server/0.juju-log db:8: Fixing python paths
2026-06-13 03:53:50 WARNING unit.landscape-server/0.db-relation-joined Error processing line 1 of /opt/venvs/landscape/lib/python3.12/site-packages/distutils-precedence.pth:
Fatal Python error: init_import_site: Failed to import the site module
Python runtime state: initialized
Traceback (most recent call last):
  File "<frozen site>", line 201, in addpackage
  File "<string>", line 1, in <module>
  File "/usr/lib/python3/dist-packages/_distutils_hack/__init__.py", line 3, in <module>
    import re
  File "/usr/lib/python3.10/re.py", line 125, in <module>
    import sre_compile
  File "/usr/lib/python3.10/sre_compile.py", line 17, in <module>
    assert _sre.MAGIC == MAGIC, "SRE module mismatch"
AssertionError: SRE module mismatch
2026-06-13 03:53:50 ERROR unit.landscape-server/0.juju-log db:8: Landscape Server schema update failed with return code 1
```

**Root cause:**
The updated `landscape-server` package (e.g. `26.10~beta.2-0landscape0` from the PPA) uses Python 3.12, establishing its virtualenv under `/opt/venvs/landscape/`. However, on Ubuntu 22.04 (Jammy), the default system Python version is 3.10. 

During relation joined/changed hooks, the charm executes a "Fixing python paths" logic to add the virtualenv site-packages to `sys.path`. When Juju executes hooks with `PYTHONPATH` set (containing `/usr/lib/python3/dist-packages` and other Python 3.10-specific system directories), or when the virtualenv executes commands in an environment that inherits `PYTHONPATH`, Python 3.12 attempts to import the standard `re` module but loads `/usr/lib/python3.10/re.py` instead of the Python 3.12 version. The Python 3.12 interpreter's built-in `_sre` module version conflicts with Python 3.10's `sre_compile` module, resulting in `AssertionError: SRE module mismatch`, which crashes the python process.

Consequently, the schema update command (e.g. `setup-landscape-db`) crashes with return code 1. The unit gets stuck in `waiting` workload status ("Waiting on relations: db") indefinitely until the pipeline timeout kills it.

**Evidence to look for:**
- `unit-landscape-server-0.log`: `"SRE module mismatch"` and `"Landscape Server schema update failed with return code 1"`
- `apt/history.log` (on landscape-server unit): Installation of `python3.12` and a new version of `landscape-server` (e.g. `26.10~beta.2-0landscape0`) on Ubuntu 22.04.

## Notes

- Clear the `PYTHONPATH` from the environment when launching python commands in the landscape-server virtual environment to ensure it doesn't leak Python 3.10 system paths into Python 3.12 virtualenv execution.
