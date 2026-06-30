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

