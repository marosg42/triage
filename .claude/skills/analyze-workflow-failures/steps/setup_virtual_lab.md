# Step Knowledge: setup_virtual_lab

## Step Overview

Bootstraps the `virtual_maas` test environment: provisions a Testflinger relay VM, prepares the remote KVM/LXD host, starts the shared virtual MAAS infra VMs, and validates runner connectivity to the virtual MAAS subnet through an `sshuttle` tunnel.

## Swift Artifacts

Objects stored under `<uuid>/generated/` that are useful for diagnosing failures:

| Path | Description | When to check |
|---|---|---|
| `generated/github-runner/run.log` | Full GitHub Actions runner log, including `setup-virtual-lab`, `setup-tunnel`, and retries | Always — primary evidence source |
| `generated/foundation.log` | FCE log collected during build/log collection phases | Check for post-failure SSH/log-collection cascades |
| `generated/maas/maas-api` | MAAS API key copied from the shared virtual MAAS seed host | Useful to confirm setup progressed through shared-virtual-maas staging |
| `generated/sshtest.txt` | Periodic SSH health checks through the tunnel | Check if log collection succeeded far enough to create it |

If `setup_virtual_lab` fails before `collect-logs` can SSH to the infra nodes, `generated/sshtest.txt`, `generated/maas/log.txt`, and `generated/maas/logs-*.tgz` may be missing entirely.

## Key Log Files (inside tgz archive)

This step often fails before a MAAS infrastructure tgz is collected. When `generated/maas/logs-*.tgz` is absent, rely on runner logs instead.

| File | What it contains | When to use |
|---|---|---|
| `generated/github-runner/run.log` | Action-level shell output, relay host IP, virsh start results, tunnel checks | Primary investigation |
| `generated/foundation.log` | Follow-on `collect-logs` SSH failures | Confirm whether reachability was still broken after the main failure |

## Grep Patterns

Useful search strings for this step's failure modes:

```bash
# Find the tunnel preflight failure
grep -nE 'Attempt [0-9]+: Scanning|All attempts failed for|sshuttle' <work_dir>/<uuid>/generated/github-runner/run.log

# Confirm relay host and infra startup progression
grep -nE 'REMOTE_HOST=|virsh start infra|Domain .* started|maas-api' <work_dir>/<uuid>/generated/github-runner/run.log

# Check whether post-failure log collection hit the same SSH problem
grep -nE 'Failed to ssh to|Connection timed out' <work_dir>/<uuid>/generated/foundation.log <work_dir>/<uuid>/generated/github-runner/run.log
```

## Known Failure Patterns

### Pattern 1: Virtual MAAS infra never reachable after tunnel setup

**Symptom:**
```text
Attempt 1: Scanning 10.241.144.2...
Scan failed. Retrying in 60 seconds...
...
All attempts failed for 10.241.144.2
##[error]Process completed with exit code 1.
```

**Root cause:** The relay VM used for `virtual_maas` came up and the remote host reported `virsh start` success for `infra1`/`infra2`/`infra3`, but the runner never established SSH reachability to the virtual MAAS subnet through the relay. In this state, `setup-tunnel` exits before MAAS login or later layers begin. The direct failure is loss of reachability to `10.241.144.2`; the precise guest-side reason (guest boot failure vs. tunnel/network failure) cannot be determined if no MAAS/log-collection artifacts were captured.

**Evidence to look for:**
- `generated/github-runner/run.log`: `ssh-keyscan` works for the relay host, `virsh start infra1/2/3` succeeds, then repeated scan failures begin for `10.241.144.2`
- `generated/foundation.log`: later `collect-logs` also reports `Failed to ssh to '10.241.144.2'`
- Artifact tree: `generated/sshtest.txt` and `generated/maas/logs-*.tgz` are absent because the tunnel never became usable enough for later collection

### Pattern 2: Tunnel reaches infra briefly, then drops before MAAS login completes

**Symptom:**
```text
Attempt 3: Scanning 10.241.144.2...
# 10.241.144.2:22 SSH-2.0-OpenSSH_9.6p1 Ubuntu-3ubuntu13.15
Attempt 1: Scanning 10.241.144.3...
# 10.241.144.3:22 SSH-2.0-OpenSSH_9.6p1 Ubuntu-3ubuntu13.15
Attempt 1: Scanning 10.241.144.4...
# 10.241.144.4:22 SSH-2.0-OpenSSH_9.6p1 Ubuntu-3ubuntu13.15
IncompleteRead(380435 bytes read, 158769 more expected)
[Errno 113] No route to host
Failed to login after 10 attempts
```

**Root cause:** `setup-tunnel` eventually established SSH reachability to the three infra nodes, but the tunnel or underlying virtual MAAS infra failed almost immediately afterwards. The background SSH health probe in `generated/sshtest.txt` captured one successful `infra1` response and then `Connection closed by 10.241.144.2 port 22`. The subsequent `maas login` loop can surface either a transient HTTP 502 or an `IncompleteRead` while MAAS is still partially answering, then repeated `No route to host` errors once connectivity to `10.241.144.0/21` is gone. This means the direct failure is loss of tunnel/reachability after initial success, not a persistent MAAS API application error.

**Evidence to look for:**
- `generated/github-runner/run.log`: SSH scans for `10.241.144.2/.3/.4` succeed, then `maas login root http://10.241.144.5:80/MAAS ...` fails with `IncompleteRead` (optionally preceded by `502`) and repeated `[Errno 113] No route to host`
- `generated/sshtest.txt`: exactly one successful probe (`infra1`) followed immediately by an SSH disconnect such as `kex_exchange_identification: Connection closed by remote host` or `Connection closed by 10.241.144.2 port 22`
- `generated/foundation.log`: later `collect-logs` still cannot SSH to `10.241.144.2`, confirming connectivity never recovered
- Artifact tree: `generated/maas/log.txt` and `generated/maas/logs-*.tgz` are absent because the run never regained enough access to collect MAAS-side logs

### Pattern 3: One virtual MAAS infra node never completes SSH validation

**Symptom:**
```text
Attempt 3: Scanning 10.241.144.2...
# 10.241.144.2:22 SSH-2.0-OpenSSH_9.6p1 Ubuntu-3ubuntu13.15
Attempt 1: Scanning 10.241.144.3...
# 10.241.144.3:22 SSH-2.0-OpenSSH_9.6p1 Ubuntu-3ubuntu13.15
Attempt 1: Scanning 10.241.144.4...
10.241.144.4: Connection closed by remote host
...
All attempts failed for 10.241.144.4
##[error]Process completed with exit code 1.
```

**Root cause:** `setup-tunnel` successfully reached the relay host and validated SSH to `infra1` and `infra2`, but `infra3` (`10.241.144.4`) never completed a usable SSH handshake. The action's `for i in 2 3 4; do run_ssh_keyscan ... || exit 1; done` logic treats any per-node failure as fatal, so the step exits before starting the background `sshtest.txt` probe or attempting `maas login`. The direct failure is therefore a single virtual MAAS infra node that booted far enough to accept TCP connections but immediately closed the SSH handshake, not a MAAS API error.

**Evidence to look for:**
- `generated/github-runner/run.log`: relay host is reachable, `virsh start infra1/2/3` succeeds, `10.241.144.2` and `.3` return SSH banners, but `.4` repeatedly returns `Connection closed by remote host` and never passes `ssh-keyscan`
- `.github/actions/setup/setup-tunnel/action.yml`: after `sshuttle` starts, the action exits immediately if any of `10.241.144.2/.3/.4` fails validation
- Artifact tree: `generated/sshtest.txt` is absent because the failure happened before the background probe was launched; `generated/maas/logs-*.tgz` is also absent, leaving no per-node guest logs for deeper diagnosis

### Pattern 4: Testflinger pool has zero available relay agents

**Symptom:**
```text
testflinger-submitter INFO Deploying 1 machines on ['sqa-virtual-maas'] with distro jammy and disk setup virtual_maas
testflinger-submitter WARNING Job needs 1 nodes but only 0 are available. Retry in 10 minutes
----- Agents from queue -----
Name                 State      Streak
alarmmonkey          reserve    93
...
----- Available Agents -----
Name                 State      Streak
----------------------------------------
testflinger-submitter ERROR Failed to find 1 available nodes in 30 minutes.
Failed to deploy 1 machines, only 0 were deployed
All attempts failed
```

**Root cause:** The virtual MAAS relay VM is requested from the `sqa-virtual-maas` Testflinger queue, but no queue agents are available. The action retries the submitter multiple times; if every poll shows the available-agent list empty and all advertised agents remain in `reserve`, no relay VM is provisioned. The step exits before LXD setup, SSH key preparation, `sshuttle`, MAAS login, or any downstream deployment layer can run. This is a Testflinger/lab-infrastructure capacity or wedged-reservation issue, not a failure in MAAS/Juju/Sunbeam deployment logic.

**Evidence to look for:**
- `generated/github-runner/run.log`: repeated `Job needs 1 nodes but only 0 are available`, followed by `Failed to find 1 available nodes in 30 minutes` and `Failed to deploy 1 machines, only 0 were deployed`
- Every `Agents from queue` snapshot has agents in `reserve` and an empty `Available Agents` section
- The final output selection step prints `All attempts failed`
- Later setup stages (`Setup LXD`, `Prepare virtual MAAS keys`, `Setup tunnel`, `Login to shared virtual MAAS`) are skipped
- `generated/sshtest.txt` and `generated/maas/maas-api` are absent because the run never reached tunnel setup or MAAS login

---

### Pattern 5: Relay VM DNS failure during `ssh-keyscan git.launchpad.net` (Setup LXD)

**Symptom (in GitHub Actions log):**
```text
+ ssh ubuntu@<relay> 'bash /home/ubuntu/setup.sh'
+ ssh-keyscan git.launchpad.net
getaddrinfo git.launchpad.net: Temporary failure in name resolution
# git.launchpad.net:22 SSH-2.0-Twisted_20.3.0+lp11      (x4)
##[error]Process completed with exit code 1.
```
There is no `+ git clone ... fce-lab` line. The failing sub-action is `setup-virtual-lab.setup_lxd`.

**Root cause:** The Testflinger relay was deployed and reachable from the runner over ssh/scp,
but its resolver failed transiently. ssh-keyscan (OpenSSH 8.9) opens one connection per
default key type, in the order RSA, ECDSA, ED25519, ECDSA-SK and ED25519-SK. Only the first
lookup, for RSA, failed. Launchpad serves only an RSA host key, so the four remaining probes
returned no key and ssh-keyscan exited 1. `set -eux` in `setup.sh` then aborted, and Setup LXD
has no retry.

**How to distinguish it from Patterns 1–3:** those patterns fail in Setup tunnel after
`virsh start`. Here `generated/sshtest.txt` and `generated/maas/` are absent, and
collect-logs `Failed to ssh to '10.241.144.2'` is only a downstream effect.

**Quick check:**
```bash
grep -nE 'ssh-keyscan git.launchpad.net|getaddrinfo|Temporary failure in name resolution|setup_lxd;outcome' run.log
```

**Recommendation:** Run `ssh-keyscan -t rsa` with a retry loop, or ship a pinned Launchpad
known_hosts entry. Also add a resolver-readiness check on the relay and retry `git clone`.

---

### Pattern 6: Relay SSH handshake reset shortly after provisioning (Setup LXD)

**Symptom (in GitHub Actions log):**
```text
+ ssh ubuntu@<relay> hostname
whomp
...
+ scp /tmp/<uuid>-setup.sh ubuntu@<relay>:setup.sh
+ rm /tmp/<uuid>-setup.sh
+ scp resources/config.inc.ironic.sh ... ubuntu@<relay>:/home/ubuntu/
kex_exchange_identification: read: Connection reset by peer      # ~60 s later
Connection reset by <relay> port 22
lost connection
##[error]Process completed with exit code 1.
```
The failing sub-action is `setup-virtual-lab.setup_lxd`. The step fails before
`ssh … bash setup.sh`, so there is no `ssh-keyscan git.launchpad.net` (unlike Pattern 5).

**Root cause:** Testflinger reported the relay deployed. About ten sequential ssh/scp
connections then succeeded within roughly 20 s. The next connection was accepted at the TCP
level, but sshd sent no SSH banner for about 60 s and then reset the connection. That
`kex_exchange_identification` error means the relay's sshd, or something on the path to it,
stopped servicing new sessions briefly. Possible causes include a late first-boot or
cloud-init sshd restart or reboot, host overload, or a network reset. Every ssh/scp in Setup
LXD runs once under `set -e`, with no retry or ConnectTimeout. One dropped handshake therefore
aborts the stage. The bundle has no relay-side logs, so the precise relay cause cannot be
determined after the fact. In one observed instance, happy-bot inspected the relay live. The
relay had not rebooted, and its sshd and auth.log never logged the failing connection.
That points to a reset on the network path rather than an sshd restart or reboot.

**Evidence to look for:**
- `run.log`: `Found IP: <relay> for job …, agent <name>` followed by successful `ssh-keyscan`
  banners and `hostname` output, then a ~60 s gap ending in `kex_exchange_identification: read: Connection reset by peer`
- Prepare virtual MAAS keys / Setup tunnel / Login to shared virtual MAAS are `skipped`;
  `generated/sshtest.txt` absent
- Collect logs later fails with `FileNotFoundError: [Errno 2] No such file or directory: 'maas'`
  (no MAAS login happened). This is only a downstream effect.
- **Caution:** `generated/maas/logs-*/10.241.144.*.tgz` may still be collected without a
  tunnel. These archives contain dedicated_maas Pokémon-named hosts, such as `leafeon`,
  `mienfoo` and `swoobat`, so ignore them.

**Quick check:**
```bash
grep -nE 'Found IP:|kex_exchange_identification|Connection reset by|lost connection|setup_lxd;outcome' run.log
```

**Recommendation:** Wait for relay stability, for example with `cloud-init status --wait` or
several consecutive `ssh true` probes. Add `ConnectTimeout`/`ConnectionAttempts` and a retry
wrapper to the relay ssh/scp calls. Use ControlMaster to cut the number of new connections.
Capture relay `journalctl -u ssh` and `last -x` output on failure.

---

### Pattern 7: Agents available, but every Testflinger provision fails fast with no IP

**Symptom (in GitHub Actions log):**
```text
testflinger-submitter DEBUG Going to use ['wringer-wooster']
testflinger-submitter INFO Submitted job 763cb460-... for agent wringer-wooster
testflinger-submitter DEBUG Job 763cb460-... status: complete        # ~65–110 s later, no "Found IP:"
failed jobs: [{'job_id': '763cb460-...', 'agent': 'wringer-wooster', 'finished': True, 'ip': ''}]
Failed to deploy 1 machines, only 0 were deployed
... ×3 attempts (9 jobs, several distinct agents)
All attempts failed
```

**Root cause:** Agents were in `waiting`, and the queue accepted the jobs right away, so this
is not Pattern 4. Each job went to `complete` in about 1–2 minutes and never reached reserve.
`monitor_jobs()` in `deploy_multiple_machines.py` records `ip: ''` when the status becomes
`complete` before the poll output contains `You can now connect to`. Every agent failed the
same way, which points to a lab-side provisioning outage. In one observed instance, happy-bot
ran `testflinger-cli results <job_id>` live. It showed `provision_status: 1` and
`DEVICE CONNECTOR: MAAS: Unable to read node's block device information: … Unable to find
the server at maas-tor3.internal`. The agents' `maas` profile `cert-maas-tel-01` could not
reach the tor3 lab MAAS, which returned HTTP 503 or hung. The pipeline template contains no
MAAS endpoint.

**Evidence to look for:**
- `run.log`: `status: complete` within about 2 minutes of `Submitted job`, with no `Found IP:` lines
- The first `Agents from queue` snapshot shows **negative streaks on all `waiting` agents**.
  Positive streaks appear only on agents in `reserve`, which means the outage started before
  this run.
  - Variant: the first snapshot can still show **positive** streaks on `waiting` agents
    when the outage starts during the run. The streaks then go negative only after this
    run's own jobs fail on them, for example alarmmonkey changing from 7 to -2.
    Same root cause. happy-bot showed the same `maas-tor3.internal` device-connector error.
    Count the `Submitted job` lines yourself, because happy-bot has under-counted the jobs.
- The bundle does not contain the provision error. The unauthenticated REST call
  `/v1/result/<id>` returns "Authentication is required". `testflinger-cli results <id>`
  reportedly works without credentials.

**Quick check:**
```bash
grep -nE 'Submitted job|status: complete|Found IP|only 0 were deployed|All attempts failed' run.log
```

**Recommendation:** Escalate the outage to the tor3 lab/Testflinger admins and re-run the
workflow. In `deploy_multiple_machines.py`, print the provision output tail when a job
completes without an IP. Also fail fast when every available agent has a negative streak.

---

_Add more patterns below as they are discovered._

## Notes

- This step is specific to `tor3-sqa-virtual_maas`; no MAAS tgz may exist when failure happens before or during tunnel validation.
- `setup-tunnel` validates the virtual MAAS network by probing `10.241.144.2`, `10.241.144.3`, and `10.241.144.4` after launching `sshuttle` through the Testflinger relay.
- `virsh start` success alone is not enough to prove the infra VMs booted correctly; the decisive signal is SSH reachability through the tunnel.
- The sshuttle tunnel set up here is not supervised; if it drops **later** in the run, the failure surfaces in whichever step is running (e.g. `sunbeam_enable_loadbalancer.md` Pattern 1, `juju_maas_controller.md` Patterns 10/12).
