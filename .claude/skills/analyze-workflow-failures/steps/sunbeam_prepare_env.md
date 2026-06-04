# Step Knowledge: sunbeam_prepare_env

## Step Overview

This step prepares the Sunbeam deployment environment on MAAS-provisioned machines.
It runs `terraform apply` via `products/sunbeam/deploy_machines.py` to allocate and
configure machines through the MAAS API. Failures here typically mean the MAAS API
is broken or returning errors, or that Terraform cannot reach/provision machines.

Entry point: `products/sunbeam/deploy_machines.py` → `deploy_machines()` → `terraform apply`

## Swift Artifacts

| Path | Description | When to check |
|---|---|---|
| `generated/sunbeam/log.txt` | FCE build log for the sunbeam layer | Always — first stop |
| `generated/maas/log.txt` | FCE build log for the MAAS layer | If MAAS itself may be broken |
| `generated/maas/logs-<timestamp>.tgz` | Full MAAS infra log archive (40–60 MB) | Deep investigation of MAAS-side failures |
| `generated/maas/maas-api` | MAAS API endpoint URL | Confirm which MAAS instance was targeted |
| `generated/github-runner/jobs.json` | GitHub job metadata (step list, run_id, conclusion) | Quick triage without `gh` CLI |

List subdirectories:
```
swift list: prefix = "<uuid>/generated/"  delimiter = "/"
```

## Key Log Files (inside MAAS tgz archive)

| File | What it contains | When to use |
|---|---|---|
| `var/log/syslog` | Everything: snapd, AppArmor, MAAS services, systemd | Primary investigation — always start here |
| `var/log/maas/var.lib.maas.log` | MAAS regiond service log | Quick MAAS-specific view |
| `var/log/haproxy.log` | HAProxy health checks for MAAS API backends | Check if MAAS API was up during failure |
| `var/log/postgresql/postgresql-16-main.log` | PostgreSQL primary instance | DB errors, constraint violations |
| `var/log/postgresql/postgresql-16-ha.log` | HA standby instance | Replication, WAL, migration activity |
| `var/snap/maas/common/log/dump.dmp` | PostgreSQL schema dump (pg_dump) | Confirm whether DB tables exist |
| `var/snap/maas/common/log/snap-perms.txt` | Snap file permissions/revision | Determine which MAAS snap revision is installed |

### Node layout (tor3-sqa-virtual_maas clusters)

Infra nodes are typically at `.2`, `.3`, `.4` in the subnet:
- `infra1` (10.x.x.2): may host regiond/rackd
- `infra2` (10.x.x.3): may host regiond/rackd
- `infra3` (10.x.x.4): often hosts regiond, rackd, PostgreSQL HA standby, HAProxy, libvirt

Check which node has the relevant service:
```bash
for d in /tmp/maas-logs/*/; do
  ip=$(basename $d)
  echo -n "$ip: "
  head -1 $d/var/log/syslog 2>/dev/null | grep -oP 'infra\d+' || echo "(no syslog)"
done
```

## Grep Patterns

```bash
# Find the primary MAAS API error
grep -h "maasserver_routable_pairs\|relation.*does not exist\|Failed to render preseed" \
  /tmp/maas-logs/*/var/log/syslog | sort | head -20

# Check snap refresh events around failure
grep -h "auto-refresh\|post-refresh\|taskrunner\|snap.*41404\|snap.*40614" \
  /tmp/maas-logs/*/var/log/syslog | sort

# Find AppArmor denials during hook
grep -h "DENIED.*post-refresh\|post-refresh.*DENIED\|gss/mech" \
  /tmp/maas-logs/*/var/log/syslog | sort

# Check MAAS API backend health
grep -h "maas-api" /tmp/maas-logs/*/var/log/haproxy.log | sort

# Get clean timeline around a window (replace timestamp prefix)
grep -h "2026-03-22T01:0" /tmp/maas-logs/*/var/log/syslog \
  | grep -v "kernel\|audit\|apparmor\|named\|maas-machine\|#011\|twisted\|django\|maasserver\|provisioning" \
  | sort

# Confirm missing DB table
grep "maasserver_routable_pairs" /tmp/maas-logs/*/var/snap/maas/common/log/dump.dmp
```

## Known Failure Patterns

### Pattern 1: maasserver_routable_pairs does not exist

**Symptom (in GitHub Actions log):**
```
ServerError: 400 Bad Request (Failed to render preseed: relation "maasserver_routable_pairs" does not exist)
```

**Root cause:** MAAS snap auto-refreshed to a new revision but the DB migration
(`maas-region dbupgrade`) was not applied due to an AppArmor profile bug in the snap's
`post-refresh` hook. The new MAAS code queries `maasserver_routable_pairs` but the table
was never created.

**Quick check:**
```bash
# Was a snap refresh recent?
grep -h "auto-refresh\|post-refresh" /tmp/maas-logs/*/var/log/syslog | sort | tail -20

# How long did the hook run?
grep -h "post-refresh" /tmp/maas-logs/*/var/log/syslog | sort
# Hook <30s = Variant B (silent failure), exactly 10m = Variant A (killed/rolled back)
```

**See also:** `patterns/maas-snap-apparmor-dbupgrade.md` for full detail including both
Variant A (hook timeout, rollback) and Variant B (hook early exit, silent migration skip).

### Pattern 2: testflinger node missing configured OSD device

**Symptom (in GitHub Actions log):**
```
wipefs: error: /dev/disk/by-dname/disk1: probing initialization failed: No such file or directory
subprocess.CalledProcessError: Command '['ssh', ..., 'ledian.maas', '--', 'sudo', 'wipefs', '-a', '/dev/disk/by-dname/disk1']' returned non-zero exit status 1.
```

**Root cause:** The `sunbeam_prepare_env` action always runs `clean_disks.py` before manifest generation. On `tor3-sqa-testflinger` the environment config can declare a single cluster-wide `osd_devices` path (`/dev/disk/by-dname/disk1`), but the fetched node list may include a machine that does not expose that device. In this run `ledian.maas` had only `disk0`/`nvme0n1`, so `clean_disks.py` aborted on the first `wipefs` call.

**Quick check:**
```bash
# Confirm the configured device path for the environment
sed -n '/tor3-sqa-testflinger-cluster_1:/,/^[^ ]/p' config/sunbeam.yaml | grep osd_devices

# Show the failing node's by-dname inventory from the layer log
sed -n '135,153p' generated/sunbeam/output.log

# Confirm hardware inventory from the node sosreport
tar -xOf generated/sunbeam/sosreport-ledian-*.tar.xz '*/sos_commands/block/lsblk' | head -20
```

### Pattern 3: bootstrap node snap store timeout during MicroCeph install

**Symptom (in GitHub Actions log):**
```text
error: cannot perform the following tasks:
- Fetch and check assertions for snap "snapd" (26865) (cannot get device session from store: store server returned status 408 ...)
subprocess.CalledProcessError: Command '['ssh', ..., 'node1.dh1-j6.tor3-sqa-dedicated-maas.solutionsqa', '--', 'sudo', 'snap', 'install', 'microceph', '--channel', 'squid/candidate']' returned non-zero exit status 1.
```

**Root cause:** `sunbeam_prepare_env` runs `clean_disks.py` before any deployment work. That script unconditionally installs the `microceph` snap on the freshly provisioned target node so it can use `microceph.ceph-bluestore-tool zap-device` to wipe the configured OSD disk. In this run the node had booted successfully and SSH was healthy, but snapd failed almost immediately while talking to the Snap Store: the `Fetch and check assertions for snap "snapd"` task got an HTTP 408 from the store. Because `clean_disks.py` does not retry or fall back when `snap install microceph` fails, the entire `sunbeam_prepare_env` layer aborts before manifest generation or any Sunbeam deployment begins.

**Quick check:**
```bash
# GitHub / layer log: failing command and traceback
sed -n '258,267p' generated/sunbeam/output.log
sed -n '128,137p' generated/lastlines.txt

# Node sosreport: snapd confirms the store-side timeout
SOS=$(find generated/sunbeam -name 'sosreport-node1-*.tar.xz' | head -1)
tar -xOf "$SOS" '*/sos_commands/snap/journalctl_--no-pager_--unit_snapd' \
  | grep 'Fetch and check assertions\|408\|microceph'
tar -xOf "$SOS" '*/sos_commands/snap/snap_changes_--abs-time' | grep 'Install "microceph"'
```

---

_Add more patterns below as they are discovered._

## Notes

- Failures here are almost always MAAS-side, not Sunbeam-side — check MAAS logs first
- The MAAS tgz is 40–60 MB; always download via `stage_object` + `curl`, not `get_object`
- On `tor3-sqa-virtual_maas` substrate, `infra3` is most often the node with the issue
  (it hosts the MAAS regiond that serves API requests and the HA PostgreSQL standby)
- If `maasserver_routable_pairs` errors appear before any snap refresh event, the DB may
  have been left in a broken state by a previous run's failed refresh (Variant A residue)

## Version History

- **v1.2** (2026-05-27): Added Pattern 3 — freshly provisioned bootstrap node fails `snap install microceph --channel squid/candidate` during `clean_disks.py` because snapd's `Fetch and check assertions for snap "snapd"` task gets HTTP 408 from the Snap Store; from run 26444914683 (UUID 4c95aea6-4e93-4bd5-8703-c3747f93f640, tor3-sqa-virtual_maas cluster_1).
- **v1.1** (2026-05-25): Added Pattern 2 — `tor3-sqa-testflinger` node missing configured OSD device (`/dev/disk/by-dname/disk1`) causes `clean_disks.py` to fail immediately with `wipefs: ... No such file or directory` before manifest generation.
- **v1.0** (2026-03-23): Initial version from analysis of runs 23388633087, 23377136139, 23406159064
