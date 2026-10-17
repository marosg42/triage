# Step Knowledge: sunbeam_create_cluster

## Step Overview

`products/sunbeam/deploy_sunbeam.py create-cluster` (run from the GitHub runner). On every
joiner it SSHes `sudo snap install openstack --channel <ch>; sunbeam prepare-node-script | bash -x`
and sets snap feature flags, then on the bootstrap node runs `sunbeam cluster add -f yaml <node>`
per joiner to get tokens, and finally runs `sunbeam cluster join <token>` on each joiner
**sequentially over SSH** (`ssh -o ServerAliveInterval=60 -o ServerAliveCountMax=10 ...`).
Any non-zero SSH exit (including transport errors, exit 255) aborts the whole step
(`join_cluster -> future.result() -> run_cmd -> CalledProcessError`). Each join takes ~10–15 min.

## Swift Artifacts

| Path | Description | When to check |
|---|---|---|
| `generated/github-runner/run.log` | Step output: per-node join start/finish (`Node joined cluster with roles: ...`) | Always — first stop |
| `generated/sunbeam/sunbeam_cluster_list.txt` | Post-failure cluster membership | Did the "failed" node actually join? |
| `generated/sunbeam/juju_machines_openstack-machines.txt` | Juju machines per node | Same |
| `generated/sunbeam/sosreport-<node>-*.tar.xz` | Per-node sosreport (syslog, auth.log, sunbeam logs) | Node-side view of the join / SSH sessions |

## Key Log Files (inside per-node sosreport)

| File | What it contains | When to use |
|---|---|---|
| `home/ubuntu/snap/openstack/common/logs/sunbeam-<ts>.log` | On-node `sunbeam cluster join` log | Did the join complete on-node? (last line `Finished running step ... COMPLETED`) |
| `var/log/auth.log` | sshd sessions from runner (egress `91.189.92.152`) | When did the server see the session close? Did a failed connect reach sshd at all? |
| `var/log/syslog` | networkd/OVS/microovn events | br-ex/eth1 bridging, netplan reconfigure during join |

## Grep Patterns

```bash
# Join timeline on the runner
grep -n "cluster join\|Node joined cluster\|Command failed\|not responding\|Connection reset" generated/github-runner/run.log | head -n <error_line>
# Server-side session lifecycle on a node
grep -a "sshd\|Session [0-9]* logged out" sosreport-<node>*/var/log/auth.log | grep "T1[0-1]:"
# Runner egress health in the same window (Temporal reports after the failure)
grep "Failed to report to Temporal on\|Successfully reported to Temporal on" run.log | grep -v echo
```

## Known Failure Patterns

### Pattern 1: Runner-side network outage kills `sunbeam cluster join` SSH (join actually succeeds)

**Symptom:**
```
ERROR - [localhost] Command failed: ssh ... doble.maas -- sunbeam cluster join <token>
Timeout, server doble.maas not responding.
ERROR - [localhost] Command failed: ssh ... racah.maas -- sunbeam cluster join <token>
kex_exchange_identification: read: Connection reset by peer
Connection reset by 10.241.2.26 port 22
subprocess.CalledProcessError: ... returned non-zero exit status 255.
```

**Root cause:** Transient loss of connectivity on the runner's side (seen on
tor3-sqa-testflinger, ~19 min). The in-flight join SSH hits the 10-min ServerAlive timeout;
the next node's SSH gets a path-level RST at banner exchange. The join on the first node
continues and completes on-node; the next node never starts its join.

**Evidence to look for:**
- On-node `sunbeam-<ts>.log` of the timed-out node ends with `... Result: COMPLETED`; node is active in `sunbeam_cluster_list.txt`.
- Node `auth.log`: the runner's session closes only via server-side TCP timeout (several min after the client gave up); the reset node has **no** sshd entry for the failed attempt.
- `run.log` post-failure "Report to Temporal with retries": attempts fail with `Failed client connect ... Timeout expired` while earlier reports in the run succeeded first try ⇒ runner egress outage, not a lab/fabric-only issue.
- Rule out node-local causes: br-ex is built on the external NIC (`eth1`), not the mgmt NIC; same bridging succeeded on earlier network-role joiners.

- **Red herring: openssh upgrade during juju provisioning.** About 30 s into each join, Juju's `apt-get upgrade` upgrades `openssh-server`, and syslog shows `Stopping ssh.service` / `Closed ssh.socket`. The socket is back about 5 s later (`Listening on ssh.socket`). `ssh.service` then only starts on the next *new* connection (socket activation), so a late `Starting ssh.service` (e.g. 10:27) does **not** mean port 22 was down. The same sequence appears on joiners that succeeded and on the bootstrap node (`Listening on ssh.socket` 08:20:28 → first new connect accepted 09:34:56). Do not blame it, and do not file happy-bot's "join-triggered apt upgrade breaks SSH" bug without stronger evidence.
- Cascade: `join_cluster()` uses `max_workers=1` but the executor still runs every queued join after the first failure. Expect one `Timeout ... not responding` followed by kex RSTs for **all** remaining joiners.

**Observed in:** run 38701649076 (UUID 770a7f56-cd59-40da-8873-9c8afc0354de, cluster_1, 2026.1/beta, outage ≈10:04–10:23 UTC on 2026-10-15).
- run 38700115038 (UUID 98c2c05d-7954-4424-b5cb-979a4f66a9f0, cluster_3, 2026.1/beta, 2026-10-15). euler's join completed at about 10:08 while the runner timed out at 10:15:31. cajal, octopot, fleep and gravetusk got kex RSTs at 10:16:33–10:17:33 with no sshd log entries. Temporal reports failed 10:18–10:22 and recovered at 10:25:39.

**See also:** `steps/sunbeam_deploy.md` Patterns 13/14 (same transport-level `Timeout, server <node> not responding` family during bootstrap).

---

_Add more patterns below as they are discovered._

## Notes

- Substrate is usually `tor3-sqa-testflinger` — no MAAS logs; use sosreports.
- Runner reaches mgmt nodes (10.241.2.0/24) directly via egress NAT `91.189.92.152`; the sshuttle daemon only tunnels 10.242.2.0/24 and the 10.241.2.100–109 VIPs.
