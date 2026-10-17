# Step Knowledge: sunbeam_enable_tls

## Step Overview

Standalone Sunbeam feature layer (`LAYER=sunbeam_enable_tls`, `FEATURE=tls`) run by
`products/sunbeam/enable_features.py` → `enable_tls()`. Sequence, all via SSH from the runner
to the Sunbeam control node (`node1`):
1. `sunbeam enable -m manifest.yaml tls ca --ca <b64 CA> ...` → "OpenStack Tls Ca application enabled."
2. `sunbeam tls ca list_outstanding_csrs --format yaml` (expect CSRs for traefik, traefik-public, traefik-rgw)
3. scp `generated/sunbeam/manifest.yaml` to the node
4. `copy_certs_to_units()` → `ssh -t ... sunbeam tls ca unit_certs -m manifest.yaml` — can run
   silently for 10+ minutes on a loaded single node.

For the same TLS commands run inside `sunbeam_enable_plugins_all`, see that step file (Patterns 12/13).

## Swift Artifacts

| Path | Description | When to check |
|---|---|---|
| `generated/github-runner/run.log` | Runner log (read only up to the failed step's `##[error]`) | Always |
| `generated/sunbeam/output.log` | Full sunbeam layer log incl. post-failure log collection | Always |
| `generated/sshtest.txt` | virtual_maas tunnel health probe | Always on `tor3-sqa-virtual_maas` |
| `generated/version_collector_sunbeam_enable_tls.log` | Post-failure SSH to `10.241.144.2` | Confirms tunnel still down |
| `generated/sunbeam/sosreport-*.tar.xz` | sosreport — check hostname (`juju-N` = wrong, colliding physical host) | Deep investigation |

## Grep Patterns

```bash
grep -n "unit_certs\|list_outstanding_csrs\|closed by remote host\|exit status 255" generated/sunbeam/output.log
tail -8 generated/sshtest.txt
grep -n "REMOTE HOST IDENTIFICATION HAS CHANGED\|No such file or directory" generated/sunbeam/output.log
```

## Known Failure Patterns

### Pattern 1: `unit_certs` SSH session severed — virtual_maas sshuttle tunnel dropped mid-command

**Symptom:**
```
ssh -t ... node1.dh1-j6.tor3-sqa-dedicated-maas.solutionsqa -- sunbeam tls ca unit_certs -m manifest.yaml
... (~11 min silence)
STDOUT: b''
Connection to node1.dh1-j6.tor3-sqa-dedicated-maas.solutionsqa closed by remote host.
subprocess.CalledProcessError: ... returned non-zero exit status 255.
```

**Root cause:** Infrastructure false failure. The runner→virtual-lab sshuttle tunnel died while
`unit_certs` was running; the SSH session was torn down. Steps 1–2 above had succeeded. Live
out-of-band checks (happy-bot via relay) showed the lab VMs still running, node1 healthy and
`list_outstanding_csrs` → `[]` — i.e. cert distribution actually completed on the node.

**Evidence to look for:**
- `sshtest.txt`: last `infra1` a few minutes before the failure, then
  `kex_exchange_identification: Connection closed by remote host` / `Connection closed by 10.241.144.2 port 22`, no further entries.
- `version_collector_sunbeam_enable_tls.log`: repeated `ssh 10.241.144.2 lsb_release -cs` → `Connection reset by peer` (exit 255).
- `output.log` log collection: `REMOTE HOST IDENTIFICATION HAS CHANGED` for node1 and
  `scp: .local/share/juju: No such file or directory`; MAAS log archive hostnames are Pokémon
  (`leafeon`/`mienfoo`/`swoobat`) and the sosreport is `sosreport-juju-1-*` — all from the
  colliding dedicated_maas hosts, irrelevant to the run.

**First observed:** run 38693071781 (UUID 4f0a7853-b7c9-414f-9b65-3e5e24c71074, tor3-sqa-virtual_maas cluster_1, branch main, 2026-10-15) — tunnel lost after 10:03:51Z, step failed 10:06:36Z.

---

### Pattern 2: `list_outstanding_csrs` → `juju show-user` pexpect timeout — Juju controller pod NotReady, VIP withdrawn

**Symptom:**
```
sunbeam tls ca list_outstanding_csrs --format yaml
Error: Timeout exceeded.
args: ['/snap/openstack/<rev>/juju/bin/juju', 'show-user']   timeout: 30   buffer: b''
subprocess.CalledProcessError: ... returned non-zero exit status 1.
```
(right after `OpenStack Tls Ca application enabled.`)

**Root cause:** The k8s-hosted Juju controller pod `controller-0` flipped to NotReady, so
MetalLB withdrew the `controller-service` VIP (`serviceWithdrawn … reason=notOwner`, no other
speaker takes over) and the ClusterIP had no endpoint. sunbeam's internal `juju show-user`
(30 s pexpect) hung until timeout. No retry in `get_outstanding_csrs()`. MetalLB itself healthy.
See `patterns/juju-k8s-controller-notready-vip-withdrawn.md`.

**Evidence to look for:**
- `pods_metallb-system_logs.tgz`: VIP withdrawn/announced gap on one speaker bracketing the failure.
- `kubectl_get_pod_detailed.txt`: `controller-0` `Ready.lastTransitionTime` = last re-announce; restartCount 0.
- `juju_debug_log_controller.txt`: no lines during the gap, then burst of `connection reset by peer` / agent re-logins.

**First observed:** run 38924413922 (UUID b53d2eac-c9a0-45f1-84a1-a400bfb8c48a, tor3-sqa-shared_maas dh1_j8_2, branch less_sshuttle, 2026-10-16) — VIP dark 19:52:02–19:53:27Z, failed 19:53:14Z.

---

_Add more patterns below as they are discovered._

## Notes

- `node1.dh1-j6.tor3-sqa-dedicated-maas.solutionsqa` is the virtual_maas Sunbeam node despite the
  dedicated-maas DNS domain — trust `jobs.json` for the substrate.
