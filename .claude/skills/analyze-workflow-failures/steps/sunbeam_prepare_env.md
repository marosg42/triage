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
for d in <work_dir>/maas-logs/*/; do
  ip=$(basename $d)
  echo -n "$ip: "
  head -1 $d/var/log/syslog 2>/dev/null | grep -oP 'infra\d+' || echo "(no syslog)"
done
```

## Grep Patterns

```bash
# Find the primary MAAS API error
grep -h "maasserver_routable_pairs\|relation.*does not exist\|Failed to render preseed" \
  <work_dir>/maas-logs/*/var/log/syslog | sort | head -20

# Check snap refresh events around failure
grep -h "auto-refresh\|post-refresh\|taskrunner\|snap.*41404\|snap.*40614" \
  <work_dir>/maas-logs/*/var/log/syslog | sort

# Find AppArmor denials during hook
grep -h "DENIED.*post-refresh\|post-refresh.*DENIED\|gss/mech" \
  <work_dir>/maas-logs/*/var/log/syslog | sort

# Check MAAS API backend health
grep -h "maas-api" <work_dir>/maas-logs/*/var/log/haproxy.log | sort

# Get clean timeline around a window (replace timestamp prefix)
grep -h "2026-03-22T01:0" <work_dir>/maas-logs/*/var/log/syslog \
  | grep -v "kernel\|audit\|apparmor\|named\|maas-machine\|#011\|twisted\|django\|maasserver\|provisioning" \
  | sort

# Confirm missing DB table
grep "maasserver_routable_pairs" <work_dir>/maas-logs/*/var/snap/maas/common/log/dump.dmp
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
grep -h "auto-refresh\|post-refresh" <work_dir>/maas-logs/*/var/log/syslog | sort | tail -20

# How long did the hook run?
grep -h "post-refresh" <work_dir>/maas-logs/*/var/log/syslog | sort
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

### Pattern 4: node snapd binary corrupted — snapd crash-loops, socket refused

**Symptom (in GitHub Actions / layer log):**
```text
error: cannot communicate with server: Post "http://localhost/v2/snaps/microceph": dial unix /run/snapd.socket: connect: connection refused
subprocess.CalledProcessError: Command '['ssh', ..., '<node>.maas', '--', 'sudo', 'snap', 'install', 'microceph', '--channel', 'squid/candidate']' returned non-zero exit status 1.
  File ".../products/sunbeam/clean_disks.py", line 28, in <module>
```

**Root cause:** `clean_disks.py` unconditionally runs `snap install microceph` on each
node with a configured OSD device (to zap the disk via `microceph.ceph-bluestore-tool`).
On the failing node, `snapd.service` is dead: its Go runtime aborted at startup with
`fatal error: invalid runtime symbol table` / `runtime: invalid pc-encoded table
f=crypto/rsa.encrypt`, i.e. the `/usr/lib/snapd/snapd` executable is **corrupted** on
that host. snapd crash-loops until systemd hits its restart limit
(`Start request repeated too quickly`), leaving `/run/snapd.socket` refusing connections.
This is a node/hardware fault (disk or memory corruption of the snapd binary), not a
pipeline bug. Distinct from Pattern 3 (store-side HTTP 408) — here snapd never runs.

**Quick check:**
```bash
# Which node failed, and the socket-refused error
grep -n "connection refused\|snapd.socket\|clean_disks" generated/sunbeam/output.log

# Confirm snapd crash on that node's sosreport
SOS=$(find generated/sunbeam -name 'sosreport-<node>-*.tar.xz' | head -1)
mkdir -p /tmp/n && tar -xf "$SOS" -C /tmp/n
cat /tmp/n/*/sos_commands/snap/systemctl_status_snapd      # Active: failed, restart counter 5
grep "invalid runtime symbol table\|invalid pc-encoded table" \
  /tmp/n/*/sos_commands/snap/journalctl_--no-pager_--unit_snapd
```

**Recommendation:** Recycle/reprovision the suspect node (fresh image clears the corrupt
binary); optionally add a `snapd` health pre-check to `clean_disks.py` for clearer triage.

### Pattern 5: shared MAAS transient DNS failure during first node SSH

**Symptom (in GitHub Actions / layer log):**
```text
ssh: Could not resolve hostname solqa-shared-maas-server-10.maas: Temporary failure in name resolution
subprocess.CalledProcessError: Command '['ssh', ..., 'solqa-shared-maas-server-10.maas', '--',
'ls', '-l', '/dev/disk/by-dname', '||', 'exit', '0']' returned non-zero exit status 255.
```

**Root cause:** On `tor3-sqa-shared_maas`, `sunbeam_prepare_env` does not generate `/etc/hosts`
entries on the GitHub runner (that action is virtual-MAAS-only), so `clean_disks.py` depends on
MAAS DNS for the freshly provisioned node names. If the first lookup returns a transient resolver
failure, SSH exits 255 and the step aborts because `clean_disks.py` does not retry DNS/SSH failures.
Later log collection may successfully reach the same hostname once DNS recovers.

**Quick check:**
```bash
# Terraform succeeded and emitted node hostnames immediately before the failure
grep -n "Apply complete\\|nodes =\\|solqa-shared-maas-server" generated/sunbeam/output.log

# The failing SSH lookup and traceback
grep -n "Temporary failure in name resolution\\|CalledProcessError\\|/dev/disk/by-dname" \
  generated/sunbeam/output.log

# Confirm this is shared MAAS and hosts-file generation was skipped/empty
grep -n "substrate: tor3-sqa-shared_maas\\|Generate hosts file in GH runner for virtual MAAS" \
  generated/github-runner/run.log
test ! -s generated/sunbeam/hosts && echo "no runner hosts entries"

# If post-failure collection later reaches the same host, the DNS failure was transient
grep -n "sosreport-.*server-10\\|Permanently added 'solqa-shared-maas-server-10.maas'" \
  generated/sunbeam/output.log
```

**Recommendation:** Add retry/backoff around the first SSH call in `clean_disks.py` or generate
runner-side host entries for shared-MAAS Terraform outputs before node preparation.

### Pattern 6: testflinger transient SSH banner failure during disk probe

**Symptom (in GitHub Actions / layer log):**
```text
kex_exchange_identification: banner line contains invalid characters
banner exchange: Connection to 10.241.4.20 port 22: invalid format
subprocess.CalledProcessError: Command '['ssh', ..., '<node>.maas', '--',
'ls', '-l', '/dev/disk/by-dname', '||', 'exit', '0']' returned non-zero exit status 255.
```

**Root cause:** On `tor3-sqa-testflinger`, `sunbeam_prepare_env` can reach a freshly deployed
node's port 22 but fail the SSH protocol banner exchange before authentication. Node sosreport
logs may show only `Connection closed ... [preauth]`, while later post-failure sos collection
successfully SSHes to the same node, confirming a transient SSH service/connection failure rather
than a missing host, missing disk, or permanent node outage. `clean_disks.py` does not retry the
initial `/dev/disk/by-dname` inventory command, so a single SSH exit 255 aborts the layer.

**Quick check:**
```bash
# Failing inventory command and SSH banner error
grep -n "banner line contains invalid characters\|banner exchange: Connection.*invalid format\|/dev/disk/by-dname" \
  generated/sunbeam/output.log

# Confirm the same node became reachable later during sos collection
grep -n "sosreport-<node>\\|Permanently added '<node>.maas'" generated/sunbeam/output.log

# Node-side sshd usually records only a preauth close for the failed attempt
SOS=$(find generated/sunbeam -name 'sosreport-<node>-*.tar.xz' | head -1)
tar -xOf "$SOS" '*/sos_commands/logs/journalctl_--no-pager_--boot' \
  | grep 'sshd' | grep 'preauth\|Accepted publickey'
```

**Recommendation:** Add retry/backoff around `clean_disks.py` SSH probes on Testflinger nodes,
especially for exit 255 before authentication.

### Pattern 7: Testflinger provisioning outage — jobs `complete` with no IP, retries fail fast

**Symptom (in GitHub Actions log):**
```text
testflinger-submitter DEBUG Job <id> status: complete          # no preceding "Found IP"
testflinger-submitter INFO Deploying N machines on ['sqa-sunbeam-X'] ...   # retry round
...
testflinger-submitter DEBUG FAILED!!!
Failed to deploy 7 machines, only K were deployed
##[error]Process completed with exit code 1.
```

**Root cause:** On `tor3-sqa-testflinger`, `testflinger/deploy_multiple_machines.py` submits
one provision job per agent, then retries failures twice on other available agents. A
lab-side Testflinger/MAAS provisioning problem makes jobs reach `complete` without ever
reporting an IP. Signature that it is systemic rather than per-machine:
- Retry-round jobs fail in **~1–2 min**, while real provisions take ~16–20 min. They fail
  before any OS deployment.
- Agents on healthy positive streaks flip to `-1` in the later queue snapshots that the
  submitter prints.
- No traceback, and no node/sosreport data in `generated/sunbeam/`.
The submitter can also reselect agents that just failed.

**Quick check:**
```bash
grep -E "Going to use|Submitted job|Found IP|status: complete|Failed to deploy" run_failed.log
# Compare submit→complete durations per round; fast retry failures = systemic.
```
The exact provision error is only in the Testflinger job log
(`https://testflinger.canonical.com/jobs/<id>`, auth required). It is not in the bundle.

**Recommendation:** Check lab MAAS/Testflinger health for the window. In the submitter,
exclude just-failed agents and back off when a whole round fails fast. Also save failed-job
provision logs into the bundle.

---

_Add more patterns below as they are discovered._

## Notes

- Failures here are almost always MAAS-side, not Sunbeam-side — check MAAS logs first
- The MAAS tgz is 40–60 MB; always download via `stage_object` + `curl`, not `get_object`
- On `tor3-sqa-virtual_maas` substrate, `infra3` is most often the node with the issue
  (it hosts the MAAS regiond that serves API requests and the HA PostgreSQL standby)
- If `maasserver_routable_pairs` errors appear before any snap refresh event, the DB may
  have been left in a broken state by a previous run's failed refresh (Variant A residue)
