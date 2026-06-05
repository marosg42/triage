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

