# Step Knowledge: juju_maas_controller

## Step Overview

This step bootstraps a Juju controller onto a MAAS-allocated machine. FCE runs
`fce build --layer juju_maas_controller`, which:
1. Installs/refreshes the `juju` snap to the configured channel
2. Adds the MAAS cloud and credentials to Juju
3. Runs `juju bootstrap` with constraints (`arch=amd64 tags=juju`) to allocate and deploy
   a controller instance via MAAS

On virtual MAAS substrates (`tor3-sqa-virtual_maas`), the controller instance is a KVM VM
managed by libvirt. On shared MAAS substrates, it is a physical machine.

Entry point: `fce build --layer juju_maas_controller` (via `.github/actions/builds/run-fce-build`)

## Swift Artifacts

| Path | Description | When to check |
|---|---|---|
| `generated/juju_maas_controller/log.txt` | FCE log for the layer — contains the bootstrap command output | Always first |
| `generated/juju_maas_controller/model_defaults.yaml` | Juju model defaults used for bootstrap | Config verification |
| `generated/foundation.log` | Full FCE foundation log (covers MAAS setup, virtual lab) | Context before bootstrap |

## Grep Patterns

```bash
# Find the bootstrap failure
grep -i "failed to bootstrap\|not deployed\|not change to Deployed\|bootstrap instance" log.txt

# Find which MAAS machine was allocated
grep -i "bg\|Launching controller instance\|arch=amd64" log.txt

# Check if juju controller was ever reachable
grep -i "show-controller\|controller.*not found\|unreachable" log.txt

# In GH runner log: find the exact error and timing
grep "failed to bootstrap\|not deployed\|ERROR" <run_failed.log> | grep -v "##\[group\]"
```

## Known Failure Patterns

### Pattern 1: Bootstrap agent started but Juju API server never accessible (port 17070)

**Symptom (in `generated/juju_maas_controller/log.txt`):**
```
2026-03-29-13:49:33 root DEBUG Connected to 10.241.37.101
2026-03-29-13:49:33 root DEBUG Running machine configuration script...
2026-03-29-13:50:18 root DEBUG Bootstrap agent now started
2026-03-29-13:50:22 root DEBUG Contacting Juju controller at 10.241.37.101 to verify accessibility...
2026-03-29-14:01:21 root DEBUG ERROR unable to contact api server after 60 attempts: unable to connect to API: dial tcp 10.241.37.101:17070: connect: connection refused
2026-03-29-14:01:27 root ERROR [localhost] Command failed: juju bootstrap ...
```
followed by `ERROR controller foundations-maas not found`.

**Root cause:** The MAAS machine was successfully allocated, the OS was deployed, SSH connected
to `10.241.37.101`, and the machine configuration script ran successfully. The Juju bootstrap
agent was started on the remote machine. However, the Juju API server (port 17070) never
became accessible — Juju retried 60 times over ~11 minutes before giving up.

**Distinguishing characteristics:**
- SSH connection **succeeds** (unlike sub-patterns A/B where provisioning fails)
- Machine configuration script **completes**
- "Bootstrap agent now started" appears in the log
- The blocking failure is port 17070 (`connection refused`) — the bootstrap agent process
  started but the API daemon never bound to that port or crashed silently after starting

**Possible root causes** (cannot distinguish without direct access to the controller machine):
- Bootstrap agent process crashed on startup (OOM, segfault, or Juju bug)
- Port 17070 blocked by firewall or iptables rule on the target machine
- Juju agent installed but API server component failed to initialise
- Snap channel downgrade interaction: FCE switched juju from `3.6/stable` → `3/stable`
  just before bootstrap; the installed agent version was `3.6.20` (from `3/stable` stream).
  A mismatch between the snap version on the runner and the installed agent version could
  cause the agent to fail silently.

**Timing pattern (c3c6526f — tor3-sqa-shared_maas dh1_j9_1):**
```
13:44:59  fce starts juju_maas_controller build
13:45:00  Juju snap switched from 3.6/stable → 3/stable (snap refresh)
13:45:05  juju bootstrap started; agent version 3.6.20 located in streams
13:45:22  cfnqa8 allocated (arch=amd64 mem=32G cores=4)
13:49:17  Waiting for address (~4 min OS deployment)
13:49:22  Attempting SSH to 10.241.37.101:22
13:49:33  Connected — machine config script started
13:50:18  Machine config script completed (~45 s)
13:50:22  Bootstrap agent started; API reachability polling begins (port 17070)
14:01:21  60 attempts exhausted → "unable to connect to API: connection refused"
14:01:27  Process completed with exit code 1
```

**Key differences from sub-patterns A/B:**

| Signal | Pattern C (this) | Sub-pattern A | Sub-pattern B |
|---|---|---|---|
| SSH connects | ✅ Yes | ✅ Yes (before reboot) | ❌ Provisioning hangs |
| "Bootstrap agent now started" | ✅ Yes | ✅ Yes | ❌ No |
| API port 17070 | ❌ connection refused | ❌ never checked | ❌ never reached |
| OS install status | ✅ Completed | ✅ Completed | ❌ Incomplete |
| Failure timeout | ~11 min (60 API attempts) | 30 min (bootstrap-timeout) | 30 min |

**Note:** This substrate is `tor3-sqa-shared_maas` — no MAAS logs are available for deeper
investigation. The machine IP was 10.241.37.101, MAAS system ID cfnqa8.

**Substrate:** Observed on `tor3-sqa-shared_maas dh1_j9_1`

**Observed in:**
- Run 23710187387 (UUID: c3c6526f-b2a1-489c-86f9-9c287cf7cb1b, tor3-sqa-shared_maas dh1_j9_1, machine cfnqa8)

---

### Pattern 2: MAAS controller instance stuck in "started" state (never reaches "Deployed")

**Symptom (in `generated/juju_maas_controller/log.txt` and GH Actions log):**
```
2026-03-19-18:59:15 root DEBUG Installing Juju agent on bootstrap instance
...
   (30 minutes of silence)
...
2026-03-19-19:29:07 root DEBUG ERROR failed to bootstrap model: bootstrap instance started but did not change to Deployed state: instance "bg3nrh" is started but not deployed
2026-03-19-19:29:13 root ERROR [localhost] Command failed: juju bootstrap ...
```
followed by:
```
ERROR controller foundations-maas not found
juju controller is bootstrapped but unreachable.
```

**Root cause:** MAAS allocated a machine for the Juju controller (`bg3nrh` in this case),
powered it on (state: "started"), but the OS deployment never completed within the
30-minute `bootstrap-timeout=1800` window. Juju waited the full timeout, then aborted.
The controller never registered, so `juju show-controller foundations-maas` returns nothing.

> **Check the tunnel first (virtual_maas):** if cleanup reports
> `401 Unauthorized (Invalid API key)` or `sshtest.txt` shows a host-key change mid-bootstrap,
> this is tunnel loss, not a provisioning stall. See Pattern 12.

On virtual MAAS substrates, the machine is a KVM VM managed by libvirt.

#### Sub-pattern A: VM failed to return after OS install reboot

From MAAS log analysis of UUID 566b109e (see below), curtin **successfully completed OS
installation** on the machine, but the VM **never came back after the reboot**:

- Curtin finished all stages (including GRUB/EFI install) at 19:05:00 — all `SUCCESS`
- VM rebooted at 19:05:05 (systemd shutdown sequence visible in console log)
- VM's MAC address (`00:16:3e:f3:21:78`) never sent a DHCP request after the reboot
- No HTTP callbacks from the VM's IP (`10.241.144.83`) after 19:05:07
- Console output (via virtio-serial / maas-machine monitor) stopped completely at reboot
- MAAS temporal-worker kept polling `bg3nrh` every ~60s until the 30-min timeout
- QEMU/libvirt logs for this VM's host were not collected (only `-2` VMs on infra2 had logs)

The VM silently vanished — no DHCP, no console output, no cloud-init first-boot callbacks.
Possible root causes (cannot distinguish without QEMU console logs from the KVM host):
- UEFI/EFI boot entry not picked up by VM firmware after reboot
- GRUB misconfiguration or disk not found at boot
- KVM/QEMU transient failure on the host (VM process crashed or hung)
- Network bridge issue on the KVM host preventing DHCP

#### Sub-pattern B: PXE boot / OS installation never progressed

The original common case: machine powers on, PXE boots, but curtin installation either
never starts or hangs mid-way. No `SUCCESS` messages in the curtin stages.

Common causes:
- MAAS PXE boot failure or TFTP issue
- Ubuntu OS installation hung mid-way
- libvirt/KVM resource contention (CPU, memory, disk I/O)
- Broken VM state from a previous run not being cleaned up

**Key signals:**
- Duration between "Installing Juju agent on bootstrap instance" and the error is exactly
  `bootstrap-timeout` seconds (1800s = 30 min) — confirming a timeout, not a crash
- The machine name is a random MAAS system ID (`bg3nrh`, etc.)
- Constraints used: `arch=amd64 tags=juju` — confirms bootstrap allocated a tagged node
- `juju show-controller foundations-maas` returns `{}` / "not found"

**Timing pattern (566b109e — sub-pattern A: install succeeded, VM didn't return):**
```
18:59:04  juju bootstrap started
18:59:15  MAAS allocated bg3nrh (arch=amd64 mem=4G cores=2) — machine started
18:59:29  bg3nrh DHCP first lease (MAC 00:16:3e:f3:21:78, IP 10.241.144.83)
19:00:19  PXE boot / curtin installation underway
19:05:00  Curtin OS install completed: all stages SUCCESS (GRUB/EFI configured)
19:05:05  VM rebooted (systemd shutdown messages end console stream)
19:05:07  Last HTTP callback from VM (Cloud-Init status POST to MAAS)
           ← VM goes completely silent: no DHCP, no console output, no callbacks ←
19:29:07  bootstrap-timeout expired → "not deployed" error
19:29:14  Process completed with exit code 1
```

**How to distinguish sub-patterns from MAAS logs:**

| Signal | Sub-pattern A (install OK, reboot failed) | Sub-pattern B (install stuck) |
|---|---|---|
| Curtin `SUCCESS` messages | Yes — all stages complete | No or partial |
| VM activity after reboot | None (no DHCP, no HTTP) | N/A (never got to reboot) |
| Console log stops at | Systemd shutdown sequence | Some curtin stage |
| GRUB/EFI install | SUCCESS | Not reached or incomplete |

If MAAS logs are available (virtual_maas / dedicated_maas substrates), look for:
```bash
# Did curtin complete?
grep "\[juju-1\].*finish: cmd-install.*SUCCESS\|install-grub.*SUCCESS" syslog

# Did VM get DHCP after reboot?
grep "00:16:3e.*DHCP\|bg3nrh.*DHCP" syslog | awk '/19:05/ || /19:[12][0-9]/'

# Last console output timestamp
grep "\[juju-1\]" syslog | tail -5
```
in the run with `{"name": ["Tag with this Name already exists."]}` are **expected and harmless**.
The script immediately follows each with `maas root tag update-nodes`, which succeeds. These do
not contribute to the bootstrap failure.

**Substrate:** Observed on `tor3-sqa-virtual_maas cluster_3` (virtual MAAS, KVM VMs)

**Observed in:**
- Run 23308064939 (UUID: 566b109e-..., tor3-sqa-virtual_maas cluster_3, machine bg3nrh)

---

### Pattern 3: SSH Broken Pipe during machine configuration script (exit code 255)

**Symptom (in `generated/juju_maas_controller/log.txt`):**
```
2026-04-09-03:33:04 root DEBUG Connected to 10.241.37.105
2026-04-09-03:33:04 root DEBUG Running machine configuration script...
2026-04-09-03:34:04 root DEBUG client_loop: send disconnect: Broken pipe
2026-04-09-03:34:04 root DEBUG ERROR failed to bootstrap model: subprocess encountered error code 255
2026-04-09-03:34:11 root ERROR [localhost] Command failed: juju bootstrap ...
```
followed by `ERROR controller foundations-maas not found`.

**Root cause:** The SSH connection to the bootstrap controller machine was severed mid-script
— exactly 60 seconds after connecting — before the machine configuration script completed
and before the Juju bootstrap agent was ever started. Exit code 255 is the SSH client's own
code for a transport-level failure (not a remote script failure). The script may have been
still running or had completed silently when the connection broke.

The 60-second window suggests a network middlebox (NAT or stateful firewall) with a ~60 s
idle TCP timeout tore down the SSH session when the config script produced no output.

**Distinguishing characteristics:**
- SSH connection **succeeds**
- "Running machine configuration script..." appears
- "Bootstrap agent now started" **never appears** (connection dies before script finishes)
- Exit code is **255** (SSH transport failure), not 1 (script failure)
- Time from SSH connect to failure: ~60 seconds (not ~11 min like Pattern C)

**Key differences from other patterns:**

| Signal | Pattern D (this) | Pattern C (API port 17070) | Pattern A/B (deploy timeout) |
|---|---|---|---|
| SSH connects | ✅ Yes | ✅ Yes | ✅ / ❌ |
| Config script completes | ❌ No (broken pipe) | ✅ Yes | ❌ Never reached |
| "Bootstrap agent now started" | ❌ Never | ✅ Appears | ❌ Never |
| Failure mode | Broken pipe / exit 255 | Port 17070 connection refused | 30-min bootstrap-timeout |
| Time SSH→failure | ~60 s | ~45 s config + ~11 min API poll | ~30 min |

**Timing pattern (470c0f4a — tor3-sqa-shared_maas dh1_j8_2):**
```
03:28:09  fce starts juju_maas_controller build
03:28:10  Juju snap switched from 3.6/stable → 3.6/candidate
03:28:18  Snap refresh complete (8.6 s)
03:28:24  juju bootstrap started; agent version 3.6.21 located in streams
03:28:45  gtm7yw allocated (arch=amd64 mem=32G cores=4)
03:32:50  Waiting for address (~4 min OS deployment)
03:32:55  Attempting SSH to 10.241.37.105:22
03:33:04  Connected — machine configuration script started
03:34:04  client_loop: send disconnect: Broken pipe (exactly 60 s later)
03:34:04  ERROR failed to bootstrap model: subprocess encountered error code 255
03:34:11  Process completed with exit code 1
```

**Substrate:** Observed on `tor3-sqa-shared_maas dh1_j8_2` — no MAAS logs available.

**Observed in:**
- Run 24170470366 (UUID: 470c0f4a-6322-4ed4-a258-287fcb20a34b, tor3-sqa-shared_maas dh1_j8_2, machine gtm7yw)

---

### Pattern 4: Bootstrap succeeds, but enable-ha times out (3rd HA member stuck in PXE boot)

**Symptom (in `generated/juju_maas_controller/log.txt`):**
The `bootstrap` sub-step completes successfully (`Bootstrap complete, controller ...
is now available`), then `enable_ha` runs and `wait_for_ready` polls `juju status`
until it raises:
```
Traceback (most recent call last):
  ...
  File ".../layers/jujucontrollerlayer.py", line 281, in build
    raise Exception("Timed out waiting for juju ha to become ready.")
Exception: Timed out waiting for juju ha to become ready.
```

**Root cause:** `juju enable-ha` asks MAAS for two more controller members to form a
3-node HA quorum. One of the new members never finishes provisioning — it sits in
MAAS state `Deploying: Performing PXE boot` and never advances to `Installing OS` /
`Configuring OS` / `Deployed`. With only 2 of 3 members up, HA never becomes ready
and `wait_for_ready` hits its timeout (~30 min).

**Distinguishing characteristics:**
- `bootstrap` sub-step **succeeds** (unlike Patterns 1/2/3, which fail during bootstrap)
- Failure is in the **`wait_for_ready`** sub-step, after `enable_ha`
- Error is the literal string `Timed out waiting for juju ha to become ready.`
- In the status polls, one controller machine is stuck at
  `'message': 'Deploying: Performing PXE boot'` for the entire window while the
  others reach `Deployed`

**How to confirm quickly:**
```bash
L=files/<UUID>/generated/juju_maas_controller/log.txt
grep -nE "Finished step: juju_maas_controller:bootstrap|Starting step: juju_maas_controller:wait_for_ready|Timed out waiting for juju ha" "$L"
# Per-machine stuck-state histogram (replace 7frnkm with the instance-id seen):
grep -oE "'<instance-id>'[^}]*'message': '[^']*'" "$L" | grep -oE "'message': '[^']*'" | sort | uniq -c
```

**Timing pattern (a3a71dce — tor3-sqa-shared_maas dh1_j9_1):**
```
00:12:14  bootstrap COMPLETE (machine 0 = cfnqa8) ✅
00:12:14  enable_ha → allocates machine 1 (cewwd7, zone3) + machine 2 (7frnkm, zone2)
00:12:15  wait_for_ready polling begins
00:18:42  machine 1 Deployed; controller/1 active 00:19:23 ✅
00:12:57  machine 2 (7frnkm) → "Deploying: Performing PXE boot" … never advances ❌
00:41:45  final poll — machine 2 still "Performing PXE boot" (57 consecutive polls)
00:42:15  Process completed with exit code 1
```

**Substrate:** `tor3-sqa-shared_maas dh1_j9_1` — no MAAS logs available, so the exact
PXE/TFTP/DHCP cause cannot be confirmed from artifacts; points to an infra/node-side
network-boot failure on the shared MAAS node backing the stuck member.

**Observed in:**
- Run 27796218691 (UUID: a3a71dce-53f0-4349-acb3-47bb843442cd, tor3-sqa-shared_maas dh1_j9_1, stuck machine 7frnkm / 10.241.37.107 / zone2)
- Run 28478367772 (UUID: d1379164-5e0e-483e-8a1b-e3644f806f2e, tor3-sqa-shared_maas dh1_j8_1, stuck machine qkwafx / juju-24-1-3 / zone3); notable: MAAS first reported "failed to acquire node" for zone "default" at 22:11:22Z, then retried into zone3 where a machine was allocated but stalled in PXE boot for ~30 min

---

### Pattern 5: Bootstrap + all 3 nodes deploy, but HA voting never converges (enable-ha timeout)

**Symptom (in `generated/juju_maas_controller/log.txt`):**
Identical final error to Pattern 4 (`Timed out waiting for juju ha to become ready.`
from `jujucontrollerlayer.py` line 281), but the cause is different: **all three
controller machines reach `Deployed`/`started`**, yet the Juju HA (MongoDB
replica-set) never reaches a stable 3-voting-member quorum.

**How to tell it apart from Pattern 4:** In Pattern 4 a MAAS machine is stuck in
`Performing PXE boot` and never deploys. In Pattern 5 **all machines deploy fine** —
the failure is purely in HA voting. Run the vote histogram:
```bash
L=files/<UUID>/generated/juju_maas_controller/log.txt
python3 -c "
import ast
polls=[l for l in open('$L') if 'controller status:' in l]
from collections import Counter
c=Counter()
for l in polls:
    d=ast.literal_eval(l.split('controller status:',1)[1].strip())
    votes=sum(1 for m in d['machines'].values() if m.get('controller-member-status')=='has-vote')
    c[votes]+=1
print('has-vote distribution:', dict(c))
"
```
- **Pattern 5 signature:** max simultaneous `has-vote` is **2 (never 3)**; the secondary
  members sit in `adding-vote` and the 3rd vote oscillates between machines 1 and 2.
- **Pattern 4 signature:** one machine never leaves `Deploying: Performing PXE boot`.

**Root cause:** `juju enable-ha` adds two secondary controllers; MAAS deploys them
successfully, but Juju's peergrouper never promotes both secondaries to voting members
at the same time. The `has-vote`/`adding-vote` flapping between the two secondaries is
the signature of an unstable MongoDB replica set (members repeatedly failing
heartbeat/sync), so a healthy 3-node quorum is never reached and `wait_for_ready` times
out after ~30 min. This is a Juju-controller / Mongo HA convergence problem on the
controller nodes — not a MAAS provisioning stall and not an FCE logic fault.

**Timing pattern (f30628d1 — tor3-sqa-dedicated_maas dh1_j6):**
```
22:47:59  bootstrap started
23:01:23  bootstrap COMPLETE (machine 0 = fxyd7f / juju-1) ✅
23:01:23  enable_ha → machine 1 = bdkae3 (juju-3), machine 2 = hfkqgp (juju-2)
23:17:10  ALL 3 machines Deployed + juju started ✅ (provisioning fine)
23:26:04  m0 has-vote, m1 has-vote, m2 adding-vote  (2 votes)
23:28:06  m0 has-vote, m1 adding-vote, m2 has-vote  (vote flapped; still 2)
23:31:24  Timed out waiting for juju ha to become ready ❌
          (over 58 polls: 1 has-vote in 47 polls, 2 has-vote in 11; never 3)
```

**Substrate:** `tor3-sqa-dedicated_maas dh1_j6`. MAAS logs are normally available for
dedicated_maas, **but** if the post-failure `Collect logs` step also fails there will be
no `generated/maas/logs-*.tgz` — controller MongoDB/jujud logs are then unavailable and
the replica-set fault can only be inferred from the `juju status` vote history.

**Observed in:**
- Run 27786306863 (UUID: f30628d1-dce8-4843-8190-b3bba5070e12, tor3-sqa-dedicated_maas dh1_j6; secondaries bdkae3/hfkqgp stuck flapping adding-vote; Collect logs step also failed)

---

### Pattern 6: enable-ha fails because MAAS can't allocate the 3rd controller node (no free tagged machine)

**Symptom (in `generated/juju_maas_controller/log.txt`):**
Same final error as Patterns 4 and 5 (`Timed out waiting for juju ha to become ready.`),
but the cause is a **MAAS capacity/availability** failure: one of the new HA controller
members never gets a machine at all. In the `juju status` polls that member shows:
```
instance-id: pending
juju-status: down
machine-status message: failed to acquire node: No available machine matches
  constraints: [... ('tags', ['juju', 'sqa-dh1_jX_Y']), ('zone', ['zoneN'])]
  → cycles through zone1/zone2/zone3/default, then "No available machine matches constraints"
```

**How to tell it apart from Patterns 4 and 5:**
| Signal | Pattern 6 (this) | Pattern 5 (HA flap) | Pattern 4 (PXE stall) |
|---|---|---|---|
| 3rd member gets a machine | ❌ never (`instance-id: pending`) | ✅ deployed | ✅ allocated, stuck in PXE |
| Status message | "No available machine matches constraints" | all Deployed, votes flap | "Deploying: Performing PXE boot" |
| juju-status of stuck member | `down` | `started` | `pending` |

Quick check — does any member never get an instance-id?
```bash
L=files/<UUID>/generated/juju_maas_controller/log.txt
python3 -c "
import ast
last=[l for l in open('$L') if 'controller status:' in l][-1]
d=ast.literal_eval(last.split('controller status:',1)[1].strip())
for mid,m in d['machines'].items():
    ms=m.get('machine-status',{})
    print(mid,'inst:',m.get('instance-id'),'juju:',m.get('juju-status',{}).get('current'),'|',(ms.get('message') or ms.get('current'))[:80])
"
```

**Root cause:** `juju enable-ha` requests two more controller members; MAAS deploys
the first but has **no free machine matching `tags=juju,sqa-dh1_jX_Y`** for the second.
Juju's provisioner retries across every availability zone, exhausts its retry budget,
and the member ends `down`/`pending`. With only 2 of 3 controllers ever existing, the
3-node HA quorum can never form → `wait_for_ready` times out (~30 min). This is a
shared-MAAS **capacity** problem (tagged-node pool too small, or concurrent runs holding
the nodes), not a Juju/Mongo convergence fault and not an OS-provisioning stall.

**Timing pattern (b643263b — tor3-sqa-shared_maas dh1_j8_2):**
```
05:50:01  bootstrap started
05:56:08  bootstrap COMPLETE (machine 0 = f7gpny / juju-26-1-5) ✅
05:56:09  enable_ha → machine 1 = q6k7dq (deploys OK), machine 2 = never acquired
06:01:16  machine 1 Deployed/started
05:56–06:26  machine 2: "No available machine matches constraints ... tags=juju,sqa-dh1_j8_2"
            across all zones; instance-id stays "pending", juju-status "down"
06:26:09  Timed out waiting for juju ha to become ready ❌
          (59 polls: has-vote on 1 member in 55 polls, 2 in 4; never 3)
```

**Substrate:** `tor3-sqa-shared_maas dh1_j8_2` — no MAAS logs available. On shared MAAS
the machine pool is shared across runs, so "No available machine" can mean the cluster
lacks a 3rd juju-tagged node or another consumer was holding it.

**Observed in:**
- Run 27807500951 (UUID: b643263b-9bc8-4b1e-ab10-49b2cc6b5667, tor3-sqa-shared_maas dh1_j8_2; machine 2 never allocated, "No available machine matches constraints")

---

### Pattern 7: Bootstrap fails immediately with MAAS API EOF — Pacemaker VIP failover race condition (virtual_maas)

**Symptom (in `generated/juju_maas_controller/log.txt`):**
```
2026-07-01-01:18:21 root DEBUG [localhost]: juju bootstrap ...
2026-07-01-01:33:22 root DEBUG ERROR
2026-07-01-01:33:22 root DEBUG  creating MAAS environ: Get "http://10.241.144.5/MAAS/api/2.0/version/": EOF
2026-07-01-01:33:22 root ERROR [localhost] Command failed: juju bootstrap ...
ERROR creating MAAS environ: Get "http://10.241.144.5/MAAS/api/2.0/version/": EOF
ERROR controller foundations-maas not found
juju controller is bootstrapped but unreachable.
```

There is a **15-minute gap** between the bootstrap command and the error. The bootstrap never
got past the initial MAAS API connectivity check — no machine was ever allocated.

**Root cause:** The `maas` step (or `Setup virtual lab`) triggered a controlled Pacemaker
cluster reconfiguration (graceful shutdown of the primary infra node) that was still in
progress when `juju_maas_controller` started. Specifically:
1. The infra node holding the MAAS VIP and PostgreSQL primary was gracefully shut down by
   the pipeline (Pacemaker `shutdown[<node>]` set by sysadmin).
2. This triggered a Pacemaker failover: the PostgreSQL primary moved to a standby node
   (taking ~35–60s to elect a new primary), and the VIP moved to a different infra node.
3. During the failover, MAAS regiond returned HTTP 500 errors because the DB was unreachable.
4. HAProxy (the VIP load balancer) restarted **5+ times** as Pacemaker reconfigured the
   cluster, dropping every in-flight TCP connection with EOF/RST.
5. `juju bootstrap` started at the exact wrong moment — after the `maas` step reported
   success but before the cluster had fully stabilized. Juju retried the MAAS API connection
   for ~15 minutes until its internal retry budget was exhausted, never succeeding.

**How to distinguish from other patterns:**

| Signal | Pattern 7 (this) | Pattern 1/2/3/C/D (bootstrap fails) |
|---|---|---|
| Error message | `creating MAAS environ: ... EOF` | `failed to bootstrap model:` or `connection refused` |
| Bootstrap gap | ~15 min (no output between start and fail) | ~30 min (full bootstrap-timeout) or ~15-60 min |
| Machine ever allocated? | ❌ Never | Usually yes |
| VIP/MAAS API issues | ✅ MAAS returning 500s at bootstrap start | No |
| Pacemaker events? | ✅ `shutdown[infra1]` just before bootstrap | Not present |

**Key MAAS log signals to look for:**
```bash
# Pacemaker graceful node shutdown (in infra3 syslog)
grep "shutdown\[infra" files/maas-logs/*/var/log/syslog | grep "sysadmin\|pacemaker-attrd"
grep "pgsql-status.*HS:alone\|Master does not exist" files/maas-logs/*/var/log/syslog

# HAProxy restarts in the ~5-minute window around bootstrap start
grep "Exiting Master process\|Loading success" files/maas-logs/*/var/log/haproxy.log

# MAAS HTTP 500s during the DB failover window
grep "HTTP/1.1.*500" files/maas-logs/*/var/log/syslog | grep "maas_nginx"

# Juju's final attempt — logged as HTTP 499 (client closed)
grep "api/2.0/version.*499" files/maas-logs/*/var/log/syslog
```

**Timing pattern (ca637be6 — tor3-sqa-virtual_maas cluster_6):**
```
01:15:29Z  maas step starts
01:17:49Z  maas step completes ✅
01:17:49Z  juju_maas_controller starts
01:17:51Z  FCE starts; juju snap switched to 4.0/candidate
01:17:58Z  Pacemaker: shutdown[infra1] set by sysadmin
01:17:58Z  Pacemaker: stops HAProxy on infra1; starts res_maas_vip on infra3
01:18:00Z  PostgreSQL: "Master does not exist"; infra3 transitions HS:async → HS:alone
01:18:14Z  Juju snap install complete
01:18:19Z  MAAS nginx starts returning HTTP 500 (DB unavailable)
01:18:21Z  juju bootstrap starts
01:18:52Z  Corosync: "Node 1000 was shut down by sysadmin"; Members left: 1000 (infra1)
01:18:56Z  infra2 promoted to PostgreSQL primary (HS:alone → PRI)
01:18:58Z–01:22:57Z  HAProxy restarts 5+ times on infra1 (reconfiguring)
01:19:01Z  infra3 becomes sync standby (HS:async → HS:sync)
01:20:07Z  MAAS /MAAS/rpc/ returns HTTP 200 (DB recovered, ~1.5 min after bootstrap start)
01:33:22Z  Juju's final attempt: nginx logs HTTP 499 Go-http-client/1.1 for /MAAS/api/2.0/version/
01:33:22Z  juju bootstrap fails: "creating MAAS environ: ... EOF"
```

**Substrate:** `tor3-sqa-virtual_maas` — MAAS logs **are** available (virtual_maas) and provide
the Pacemaker/HAProxy evidence critical for diagnosis.

**Observed in:**
- Run 28478377482 (UUID: ca637be6-d60f-4fc5-9ed5-7d12676cc2bd, tor3-sqa-virtual_maas cluster_6; infra1 was the Pacemaker primary shut down; HAProxy restarted 5+ times 01:17:58–01:22:57)

---

### Pattern 8: Machine-configuration/agent phase hangs → 30-min bootstrap-timeout ("cancelled")

**Symptom (in `generated/juju_maas_controller/log.txt`):**
```
2026-07-07-15:41:29 root DEBUG Connected to 10.241.128.82
2026-07-07-15:41:29 root DEBUG Running machine configuration script...
2026-07-07-16:04:29 root DEBUG Bootstrap cancelled, you may need to manually remove the bootstrap instance
2026-07-07-16:04:29 root DEBUG ERROR failed to bootstrap model: cancelled
```
followed by `ERROR controller foundations-maas not found`.

**Root cause:** The machine was allocated, the OS deployed, SSH connected, and Juju began the
synchronous *machine configuration script* phase (fetch `jujud` tools → install machine agent →
start it). That phase then **stalled with no output** and the 30-minute `bootstrap-timeout=1800`
expired, so Juju cancelled the bootstrap ("cancelled", not "connection refused") and destroyed
the instance.

**Distinguishing characteristics (vs. Pattern 1):**
- SSH connects and "Running machine configuration script..." appears.
- **Never** reaches "Bootstrap agent now started" or "Contacting Juju controller..." — the hang
  is *before* the API-reachability phase (Pattern 1 fails *after*, on port 17070 refused).
- Final error is `failed to bootstrap model: cancelled` (bootstrap-timeout), not a port error.
- ~23-min gap of total silence between the config-script line and the cancel line; total
  bootstrap wall time ≈ 30 min (1800s) + SSH overhead.

**Evidence to look for (dedicated_maas / virtual_maas — MAAS logs available):**
- VM serial console (`var/log/libvirt/qemu/<vm>-serial0.log`): base + Juju cloud-init
  **finish cleanly** ("Cloud-init ... finished ... Up NNs", juju-bootstrap host keys generated),
  then the console goes **silent** — no `jujud` output, no panic, no reboot.
- Host `var/log/libvirt/qemu/<vm>.log`: QEMU killed by a clean teardown SIGTERM
  (`terminating on signal 15 ... shutting down, reason=destroyed`) at the cancel time —
  confirms the VM stayed alive (not a crash) and was torn down by the timeout.
- Region syslog: no further MAAS metadata contact from the controller IP after cloud-init
  finished, and `Reloaded DNS configuration; ip <controller-ip> released` at teardown time.
- The in-guest `machine-0`/`jujud` logs do **not** survive (instance destroyed on cancel), so
  the exact stall (tools fetch vs. agent start vs. transient SSH/network loss) usually cannot be
  pinned down — recommend `--debug` bootstrap / keeping the failed instance next time.

**Timing pattern (fa55838d — tor3-sqa-dedicated_maas dh1-j2, controller VM juju-1 / 4f6cr3):**
```
15:32:47  juju bootstrap starts (bootstrap-timeout=1800)
15:33:07  machine 4f6cr3 (juju-1 VM, 16G/8c) allocated; agent 3.6.25 located
15:41:08  Waiting for address
15:41:27  controller VM cloud-init finishes cleanly (juju-bootstrap keys, /var/lib/juju ready)
15:41:29  SSH connected → "Running machine configuration script..."   ← stalls here
16:04:29  bootstrap-timeout expires → "Bootstrap cancelled" / "failed to bootstrap model: cancelled"
16:04:35  QEMU juju-1 killed by teardown (SIGTERM); 16:04:43 IP 10.241.128.82 released
```

**Substrate:** `tor3-sqa-dedicated_maas` (dh1-j2) — MAAS logs available; controller runs as a
KVM VM (`juju-1`) on the MAAS host, so use the libvirt/qemu serial console for the guest view.

**Observed in:**
- Run 28870133563 (UUID: fa55838d-32df-4ea0-9d1b-abfa23160f3e, tor3-sqa-dedicated_maas dh1-j2, controller VM juju-1 / system_id 4f6cr3 / 10.241.128.82)

---

### Pattern 9: Bootstrap rejected instantly — `hwe_kernel: "<series> has no kernels available."` (boot images still importing)

**Signature** (`generated/juju_maas_controller/log.txt`, fails within ~6 s of `juju bootstrap`):
```
ERROR failed to bootstrap model: cannot start bootstrap instance:
  unexpected: ServerError: 400 Bad Request ({"hwe_kernel": ["jammy has no kernels available."]})
```
Followed by the misleading secondary error:
```
ERROR controller foundations-maas not found
ERROR juju controller is bootstrapped but unreachable.
```
> The "bootstrapped but unreachable" line is a **red herring** — `juju_cli.py:153` calls
> `is_controller_reachable()` unconditionally after a failed `bootstrap`. No controller and no
> machine allocation ever existed. Do not chase network/HA/VIP theories.

**What it means:** this is an *allocation-time* rejection, not a deploy/timeout failure. MAAS
refuses to allocate a machine for a `distro_series` whose boot resource set is not
`complete: true` (kernel/initrd present but squashfs still downloading is enough to fail).

**Root cause:** FCE's `ConfigureMAAS._sync_images()`
(`foundationcloudengine/layers/configuremaas.py:355`) triggers `boot-resources import` and then
"waits" with `wait_for_not_boot_resource_importing()` (`foundationcloudengine/maas_cli.py:1332`),
which **returns on its first poll if `is-importing` is already false**. MAAS 3.8 drives the
import through asynchronous Temporal workflows, so `is-importing` is still false a couple of
seconds after the trigger and the wait becomes a no-op. `wait_for_rack_image_sync()` then passes
on the *pre-existing* rack content. The pipeline continues while a multi-GB sync runs in the
background and bootstrap races it ~30–40 min later.

**How to confirm** (`generated/maas/log.txt`, look for an 11-second "sync"):
```bash
grep -n "boot-source-selections create\|boot-resources import\|is-importing\|list-boot-images\|{'synced'}" \
  files/<UUID>/generated/maas/log.txt
```
Telltale sequence — note `is-importing` polled ~2 s after `import`, and the `list-boot-images`
dump containing only the *other* release (e.g. only `ubuntu/noble` when jammy was just selected):
```
15:28:58  boot-source-selections create 1 os=ubuntu release=jammy ...
15:28:59  boot-resources import
15:29:01  boot-resources is-importing      ← 2 s later → wait returns instantly
15:29:09  {'synced'}                        ← stale; image list has no jammy entry
```

Then prove the download was still running at bootstrap time (MAAS syslogs — **use `grep -a`**):
```bash
# completions vs. starts: a trailing "Starting activity" with no matching "Sync complete"
grep -ah "Starting activity download_bootresourcefile\|Sync complete for file" \
  files/maas-logs/*/var/log/syslog | sort | tail -20

# terminal download failures that force a full restart of a large file
grep -ah "'activity_type': 'download-bootresourcefile'" files/maas-logs/*/var/log/syslog \
  | grep -o "temporalio.exceptions.ApplicationError: [^\"]\{0,40\}" | sort | uniq -c
```
Common aggravators: `ApplicationError: ReadTimeout` and
`maasservicelayer.utils.image_local_files.LocalStoreInvalidHash` → `Invalid SHA256 checksum`,
both of which restart the ~500 MB squashfs from zero.

**⚠️ Grep trap — do not misread this as an abandoned sync:**
```
[WARNING] No resources to download for selection with id 1
```
(`maastemporalworker.workflow.bootresource:1031`) is emitted by the periodic
`MasterImageSyncWorkflow` / `SyncSelectionWorkflow` and means "no *new* files to queue", **not**
"sync gave up". It appears repeatedly while downloads are actively in flight. Confirm liveness
by finding a `Starting activity download_bootresourcefile` with no matching `Sync complete`
before the logs end.

**Fix direction:** poll until `is-importing` becomes true (or sleep before the first poll), and
assert every configured boot-source selection is `complete: true` in `boot-resources read`
before the MAAS layer returns; alternatively select the bootstrap series at MAAS install time
so the sync overlaps commissioning.

**Seen in:**
- Run 33884295743 (UUID: 744e849d-c63c-43be-87bf-558dc27340d4, tor3-sqa-virtual_maas cluster_4,
  SKU `fcb-master-magpie-deb-resolute-mixed`, MAAS `1:3.8.0~rc1-18659`, FCE 2.21.2+git.150,
  jammy selected 15:28:58, bootstrap rejected 16:06:30)

---

### Pattern 10: `wait_for_ready` fails with Juju CA TLS error after virtual-MAAS tunnel loss

**Symptom (in `generated/juju_maas_controller/log.txt`):**
```
... Bootstrap complete, controller "foundations-maas" is now available
... Finished step: juju_maas_controller:enable_ha
... Starting step: juju_maas_controller:wait_for_ready
... controller status: {... machine 0 Deployed/has-vote, machines 1/2 adding-vote ...}
... Command failed: juju status -m foundations-maas:controller --format=yaml
ERROR unable to connect to API: tls: failed to verify certificate: x509: certificate signed by unknown authority (possibly because of "crypto/rsa: verification error" while trying to verify candidate authority certificate "juju-ca")
```

**Root cause:** On `tor3-sqa-virtual_maas`, the runner's `sshuttle`/SSH tunnel to the virtual
lab can drop while the HA controllers are still converging. The same `10.241.144.x` address
range is also reachable on the underlying physical/dedicated network, so a later `juju status`
may reach a different Juju controller at the colliding IPs instead of failing with a simple
route error. The foreign controller presents a different `juju-ca`, causing the TLS
verification error and aborting `wait_for_ready`.

**Distinguishing characteristics:**
- Bootstrap and `enable-ha` already succeeded.
- The fatal command is a later `juju status`, not `juju bootstrap`.
- `generated/sshtest.txt` has `kex_exchange_identification` / `Connection closed by
  10.241.144.2 port 22` at or shortly before the failing poll.
- The last successful controller status may show HA members only a few minutes into
  `Performing PXE boot` / `Loading ephemeral`; do not mistake that early deploy state for a
  MAAS provisioning timeout.
- MAAS logs collected after the tunnel failure are absent or untrustworthy for this run.
- If the tunnel drops during `juju bootstrap` instead, see Pattern 12.

**How to confirm quickly:**
```bash
grep -n "tls: failed to verify certificate\|crypto/rsa: verification error\|juju-ca" \
  files/<UUID>/generated/juju_maas_controller/log.txt
grep -n -B2 -A2 "kex_exchange_identification\|Connection closed by 10.241.144.2" \
  files/<UUID>/generated/sshtest.txt
```

If live access still exists, compare the served CA/UUID/hostnames at the Juju API endpoints
against the client's cached controller identity; a mismatch means the runner is talking to the
colliding physical controller, not the virtual lab's controller.

**Substrate:** `tor3-sqa-virtual_maas`

**Observed in:**
- Run 36128038076 (UUID: 61138f48-9fd2-435e-8dac-5025801c6a45, cluster_4; tunnel health
  degraded at 13:23, `juju status` failed at 13:30:45 with Juju CA TLS verification error)

---

### Pattern 11: Bootstrap succeeds, but an HA member hits terminal `Failed deployment` (curtin install failure)

**Symptom (in `generated/juju_maas_controller/log.txt`):**
Same final error as Patterns 4/5/10 (`Timed out waiting for juju ha to become ready.` from
`jujucontrollerlayer.py` line 281), but in the `juju status` polls one HA member shows a
**terminal** MAAS state rather than a stalled one:
```
'1': {'juju-status': {'current': 'down', 'message': 'agent is not communicating with the server'},
      'instance-id': 'afdxrc', 'display-name': 'juju-23-1-2', 'dns-name': '10.241.32.71',
      'machine-status': {'current': 'provisioning error',
                         'message': 'Failed deployment: Performing PXE boot'},
      'controller-member-status': 'adding-vote'}
```

**Root cause:** `juju enable-ha` allocated a MAAS machine that progressed normally through
`Deploying` → `Performing PXE boot` → `Installing OS` → `Configuring OS` and then **failed
the curtin OS install** (MAAS status 11, installation script result `exit_status: 1`). MAAS
marks the machine `Failed deployment`; Juju surfaces it as `provisioning error` and does not
retry. `wait_for_ready` can never converge, so it burns the full ~30-minute timeout.

> **Reading the status message:** the trailing text in `Failed deployment: <progress>` is only
> MAAS's **last progress marker**, not the failure point. `Failed deployment: Performing PXE
> boot` almost always means the failure happened later, during curtin install. Confirm with
> `maas <profile> node-script-results read <system_id>` (Installation entries,
> `status_name`/`exit_status`) — live-env only; not available in the archive.

**Distinguishing characteristics vs Pattern 4:**

| Signal | Pattern 11 (this) | Pattern 4 |
|---|---|---|
| `machine-status.current` | `provisioning error` (terminal) | `pending` / stuck `Deploying` |
| Progress before failing | reached `Installing OS` + `Configuring OS` | never advanced past PXE boot |
| `juju-status.current` | `down` — "agent is not communicating with the server" | `pending` |
| Time to reach failed state | ~3 min after `enable-ha` | never — stalls the whole window |

**How to confirm quickly:**
```bash
L=files/<UUID>/generated/juju_maas_controller/log.txt
grep -c "provisioning error" "$L"
# Per-machine status histogram for the suspect Juju machine index (e.g. '1'):
grep -o "'1': {'juju-status': {'current': '[a-z]*'[^}]*}, 'dns-name'" "$L" | sort | uniq -c
# Identify the MAAS VM name + system ID of the failing member:
grep -o "'instance-id': '[a-z0-9]*', 'display-name': '[^']*'" "$L" | sort -u
```
`display-name` is the **MAAS VM name** (e.g. `juju-23-1-2`); `instance-id` is the MAAS
system ID (e.g. `afdxrc`).

**Timing pattern (bf23df9b — tor3-sqa-shared_maas dh1_j8_1):**
```
03:41:41  machine 0 allocated: qe3ydh / juju-22-1-1 / 10.241.32.28 (zone1)
03:46:31  bootstrap COMPLETE ✅
03:46:31  enable_ha → machine 1 (afdxrc, zone2) + machine 2 (a3cshk, zone3)
03:46:54  afdxrc Deploying → 03:47:15 PXE boot → 03:47:45 Installing OS → 03:48:40 Configuring OS
03:49:46  afdxrc ❌ "Failed deployment: Performing PXE boot" (terminal); juju agent down
03:50:44  a3cshk Deployed; controller/2 active ✅
04:16:17  final poll — machine 1 unchanged (6 × pending, then 51 × down)
04:16:47  Exception: Timed out waiting for juju ha to become ready.
```

**Repeat-offender check (shared_maas):** before blaming Juju or the pipeline, check the
machine's Installation history in MAAS. A VM with `Failed / exit 1` installs across many
months and only occasional passes is simply a bad KVM guest in the shared pool — exclude or
retire it. Corroborate with siblings from the same pool/tags/image deploying fine in the same
window, which exonerates pool, image, network and the Juju request.

**Substrate:** `tor3-sqa-shared_maas dh1_j8_1` — no MAAS logs collected, so the curtin failure
itself is not visible in the artifact bundle.

**Improvement opportunity:** `wait_for_ready` should fail fast when any controller machine
reports `machine-status.current == 'provisioning error'` (a terminal state Juju will not
retry) instead of consuming the full 30-minute timeout with a misleading "HA not ready"
message.

**Observed in:**
- Run 36516595760 (UUID: bf23df9b-ab34-4639-b543-ab057e4692bc, tor3-sqa-shared_maas dh1_j8_1,
  failing machine `afdxrc` / `juju-23-1-2` / 10.241.32.71 / zone2, pod host
  `solqa-shared-maas-server-23`)

---

### Pattern 12: Tunnel loss during bootstrap → misleading "started but not deployed"

**Symptom:** looks like Pattern 2 — `juju bootstrap` times out at 1800 s — but the cleanup
fails with a MAAS auth error:
```
ERROR failed to bootstrap model: ... is started but not deployed
error cleaning up: ... 401 Unauthorized (Invalid API key)
```

**Root cause:** the `virtual_maas` sshuttle tunnel dropped mid-bootstrap. Juju's MAAS API polls
then reached the dedicated_maas MAAS at the same IP, which rejects the virtual lab's API key.
Juju never sees the instance reach `Deployed`. Same mechanism as Pattern 10, but it hits
during `bootstrap` rather than `wait_for_ready`.

**How to confirm quickly:**
- `generated/sshtest.txt`: `REMOTE HOST IDENTIFICATION HAS CHANGED` (host-key change) during
  the bootstrap window. See SKILL.md 4a-i.
- The MAAS archive has Pokémon hostnames, with
  `GET /MAAS/api/2.0/machines/?agent_name=...&id=<sysid>` → `401` about every 10 s:
  ```bash
  grep -a "id=<sysid>" files/maas-logs/*/var/log/maas/http/access.log | grep " 401 "
  ```
- Ignore the MAAS logs as evidence for this run's VM (they are from dedicated_maas).

**Substrate:** `tor3-sqa-virtual_maas`

**Observed in:**
- Run 38672200059

---

_Add more patterns below as they are discovered._

## Notes

- `bootstrap-timeout=1800` (30 minutes) is the configured timeout for the MAAS controller
  deployment. If the machine takes longer to provision, the bootstrap will always fail.
- The `tags=juju` constraint ensures bootstrap picks a machine tagged with `juju` in MAAS.
- On virtual MAAS, machines are VMs managed by KVM/libvirt on infra hosts (10.241.144.1–4).
- The `foundation.log` covers earlier layers (MAAS setup, virtual lab). If it contains old
  timestamps (e.g., 2020), it is a cached log from a prior run — ignore it.
- `maas root tags create` errors for tags that already exist are always benign — the script
  handles them by proceeding to `tag update-nodes`.
