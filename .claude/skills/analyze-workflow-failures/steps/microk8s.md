# Step Knowledge: microk8s

## Step Overview

Deploys a 3-unit MicroK8s cluster on MAAS-provisioned VMs using the `microk8s` charm (track `1.28/stable` or similar). Juju provisions three machines (one per zone) via the `foundations-maas` controller, runs `juju-wait` with a 14400s overall timeout and a 1800s machine-error timeout.

## Swift Artifacts

Objects stored under `<uuid>/generated/microk8s/` that are useful for diagnosing failures:

| Path | Description | When to check |
|---|---|---|
| `generated/microk8s/log.txt` | FCE build log for the microk8s layer | Always — first stop |
| `generated/microk8s/juju_status_foundations-maas_microk8s.json` | Final Juju model state (JSON) | Machine/unit state at failure |
| `generated/microk8s/juju_status_foundations-maas_microk8s.txt` | Final Juju model state (human) | Quick summary |
| `generated/microk8s/bundle.yaml` | Deployed Juju bundle | Check charm revision/channel |
| `generated/microk8s/model_config.yaml` | Juju model config | Space/zone settings |
| `generated/maas/logs-<timestamp>.tgz` | MAAS infra log archive | VM console, MAAS events |
| `generated/sshtest.txt` | SSH tunnel health log | Check for tunnel failures |

## Key Log Files (inside MAAS tgz archive)

| File | What it contains | When to use |
|---|---|---|
| `var/log/libvirt/qemu/microk8s-N-serial0.log` | VM serial console (cloud-init, Juju agent startup) | Confirm agent started, check nonce/machine number |
| `var/log/libvirt/qemu/microk8s-N.log` | QEMU process log | VM config, crash evidence |
| `var/log/syslog` (on infra1/2/3) | MAAS events, Squid proxy, DHCP | Machine deployment timeline |
| `var/log/maas/var.lib.maas.log` | MAAS node state transitions | Confirm DEPLOYING→DEPLOYED |

## Grep Patterns

```bash
# Track machine state transitions in juju-wait verbose output
grep -n "Machine [0-9] is in\|started\|allocating\|executing\|workload" <work_dir>/run_*_failed.log | head -50

# Find when each microk8s machine deployed in MAAS
grep -a "microk8s-[123]: Status transition" <maas-logs>/10.*/var/log/syslog

# Check Juju agent start in serial console
grep "jujud-machine\|Starting Juju\|nonce\|echo machine" <maas-logs>/10.*/var/log/libvirt/qemu/microk8s-*-serial0.log

# Find first machine down event
grep -n "Machine [0-9] is in down" <work_dir>/run_*_failed.log | head -5

# Check machine status in final juju status JSON
python3 -c "
import json
s = json.load(open('generated/microk8s/juju_status_foundations-maas_microk8s.json'))
for mid, m in s['machines'].items():
    print(f'Machine {mid}: juju={m[\"juju-status\"][\"current\"]}, maas={m[\"machine-status\"][\"current\"]} host={m.get(\"hostname\",\"?\")}')
"

# Check proxy activity for a specific machine IP after cloud-init
grep -a "<machine-ip>" <maas-logs>/10.*/var/log/syslog | grep "squid\|maas-http" | grep "<time-window>"
```

## Known Failure Patterns

### Pattern 1: Juju machine agent `down` — unit succeeds but machine stays down (Juju 4.0.x)

**Symptom (GitHub Actions log):**
```
ERROR:root:Machine 2 is in down for 30 minutes.
subprocess.CalledProcessError: Command '['juju-wait', '-m', 'foundations-maas:microk8s',
  '-t', '14400', '--machine-error-timeout', '1800', '--retry_errors', '0',
  '--workload', '--verbose']' returned non-zero exit status 1.
```

**Final juju status shows:**
- Machine N: `juju-status: down`, `machine-status: running` (MAAS says Deployed)
- Unit microk8s/N: `active`, `idle`, `node is ready`

**Root cause:** The Juju machine agent on one of the three microk8s VMs failed to maintain a heartbeat connection with the Juju controller, despite the machine being alive. The unit agent sub-process started and ran the charm hooks to completion (active/idle), but the machine agent's own controller connection was never established or immediately dropped. `juju-wait --machine-error-timeout 1800` fires after 30 minutes of the machine staying `down`.

**Timeline pattern:**
1. All machines register with controller simultaneously (~13:31 in observed run)
2. Machines 0 and 1 transition to `started`; Machine N goes to `down` immediately
3. Unit N's agent begins `executing` hooks shortly after (machine is `down` but unit runs)
4. Charm completes successfully; unit reaches `active/idle`
5. Machine N never transitions to `started` — stays `down` for 30 minutes
6. juju-wait fires at `t_down + 1800s`

**Evidence to look for:**
- Serial console: Juju agent DID start (`OK Started juju agent for machine-N`) — rules out agent install failure
- MAAS syslog: No errors after deployment; machine continued making apt/proxy requests — rules out VM crash
- juju-wait verbose: Unit executing/maintenance/active but machine stays `down` throughout
- Final juju status JSON: `machine-status: running` confirms MAAS sees machine alive; only Juju heartbeat failed

**Note:** This is inconsistent with normal machine agent behavior. Likely a Juju 4.0.x (observed on 4.0.8) machine agent bug where the machine worker loop fails to register while unit workers continue. The workload is actually healthy — the failure is a monitoring/bookkeeping issue.

---

_Add more patterns below as they are discovered._

## Notes

- The failing machine is typically on a specific zone (zone3 in the observed run); other zones succeed. No persistent zone network issue has been identified.
- The microk8s charm takes 15-30+ minutes to fully deploy (snap install + cluster formation) — expect long `executing`/`maintenance` periods in juju-wait output.
- `juju export-bundle -m foundations-maas:microk8s` will fail if the model doesn't exist yet (expected at step start).

