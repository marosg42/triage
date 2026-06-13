# Step Knowledge: sunbeam_maas_deploy

## Step Overview

Runs `sunbeam cluster bootstrap`, `sunbeam cluster deploy`, and `sunbeam configure`
on a set of MAAS-allocated VMs. Bootstrap provisions a 3-node HA Juju controller on
`juju-controller`-tagged machines, then deploys the OpenStack k8s workloads on the
`sunbeam`-tagged machines.

Substrate: `tor3-sqa-dedicated_maas` (Pokémon-named KVM hosts, VMs named
`juju-1/2/3` and `sunbeam-1/2/3`).

## Swift Artifacts

Objects stored under `<uuid>/generated/` that are useful for diagnosing failures:

| Path | Description | When to check |
|---|---|---|
| `generated/sunbeam/output.log` | Full FCE sunbeam_maas_prepare_env + deploy log | Always — first stop |
| `generated/maas/logs-<timestamp>.tgz` | Full MAAS infrastructure log archive | Deep investigation |
| `generated/version_collector_sunbeam_maas_deploy.log` | Snap/apt versions + juju status at failure time | Check juju controller state |
| `generated/sunbeam/manifest.yaml` | Sunbeam manifest used for bootstrap | Check cluster config |
| `generated/versions.yaml` | Software versions for all layers | Identify snap revisions |

## Key Log Files (inside MAAS logs tgz archive)

Hosts: `10.241.128.2` (anahuac), `10.241.128.3` (sunset), `10.241.128.4` (noma).
These are the physical KVM hosts hosting the `juju-*` and `sunbeam-*` VMs.

| File | What it contains | When to use |
|---|---|---|
| `var/log/syslog` | MAAS node status transitions, libvirtd events | Primary investigation |
| `var/log/libvirt/qemu/juju-N-serial0.log` | VM console output during OS install | Check curtin timing, cloud-init finish |
| `var/log/libvirt/qemu/sunbeam-N-serial0.log` | Sunbeam VM console output | Check sunbeam node deploy |
| `var/log/libvirt/qemu/juju-N.log` | QEMU startup/shutdown log | Check I/O errors on KVM host |

## Grep Patterns

```bash
# Find VM deploy transitions
grep "juju-\|sunbeam-" <work_dir>/maas-logs/*/var/log/syslog | grep "DEPLOYING\|DEPLOYED\|READY\|transition"

# Find cloud-init finish timestamps (crucial for HA controller timing)
grep "cloud-init.*finished\|Cloud-init.*finished" <work_dir>/maas-logs/*/var/log/libvirt/qemu/juju-*-serial0.log

# Find KVM host I/O errors
grep "End of file while reading\|Input/output error" <work_dir>/maas-logs/*/var/log/libvirt/qemu/*.log

# Find juju wait-for in GitHub Actions log
grep "wait-for\|timeout\|returned non-zero" run_<run_id>_failed.log
```

## Known Failure Patterns

### Pattern 1: HA Juju controller `wait-for` timeout — slow KVM deployment

**Symptom:**
```
An unexpected error has occurred.
Error: Command '['/snap/openstack/945/juju/bin/juju', 'wait-for', 'application',
       '-m', 'controller', 'controller', '--timeout', '15m']'
       returned non-zero exit status 1.
Process completed with exit code 1.
```

**Root cause:** `sunbeam cluster bootstrap` deploys a 3-node HA Juju controller on
MAAS VMs. After bootstrapping `juju-1`, it simultaneously deploys `juju-2` and
`juju-3`, then calls `juju wait-for application controller -m controller --timeout
15m`. If one of the HA nodes takes longer than ~12–13 minutes for OS installation
(curtin) + cloud-init (which installs the Juju agent), the deadline expires before
all 3 `controller` units can become active. The `wait-for` then exits with code 1.

Observed on `dedicated_maas dh1_j2`: `juju-3` (hosted on KVM host `sunset`) took
12m 46s for OS deployment vs 7m 24s for `juju-2` (on KVM host `noma`). Cloud-init
on `juju-3` didn't finish until 15:49:24, 38 seconds after the error was reported.

**Evidence to look for:**
- `maas-logs/10.241.128.3/var/log/syslog`: `juju-3: Status transition from DEPLOYING to DEPLOYED` — check this timestamp; if it is >10m after deploy start, the 15m deadline is likely tight
- `maas-logs/10.241.128.3/var/log/libvirt/qemu/juju-3-serial0.log`: Look for `cloud-init.*finished at.*Up NNN seconds` — if finished time is after the `juju wait-for` deadline, this confirms the pattern
- `maas-logs/*/var/log/libvirt/qemu/juju-3.log`: `End of file while reading data: Input/output error` — repeated during VM startup signals transient I/O contention on the KVM host
- GitHub Actions log: `sunbeam cluster bootstrap` started timestamp vs error timestamp should be ~22m (15m wait + ~7m setup overhead)

**Observed in:** run 24403605251 (UUID 69c781c2, tor3-sqa-dedicated_maas dh1_j2,
openstack rev 945, 2024.1/stable, 2026-04-14); run 24410111949 (UUID f3d1c1f9,
same substrate/cluster, 2026-04-14); run 24415173684 (UUID e9de9830, same
substrate/cluster, 2026-04-15); run 24465842464 (UUID 2c232149, same
substrate/cluster, 2026-04-15); run 24472204170 (UUID 210a50ea, same
substrate/cluster, 2026-04-15) — juju-3 on `sunset` finished cloud-init 24s after
deadline; no I/O errors in juju-3.log this time (curtin latency alone caused the miss);
run 24478433479 (UUID 0b8b31f3, same substrate/cluster, 2026-04-15) — juju-3 on
`sunset` finished cloud-init 42s before deadline but Juju agent did not register in time;
curtin took 11m31s, cloud-init 2m54s (total 14m25s); no I/O errors in QEMU logs;
run 24482841742 (UUID 98ef576d, same substrate/cluster, 2026-04-16) — juju-3 on
`sunset` finished cloud-init 11s after deadline; curtin took 12m18s, cloud-init 2m47s
(total 15m05s); juju-2 on `noma` deployed in 7m33s for comparison; no I/O errors in
QEMU logs.

**Bug:** [lp:openstack:2148312](https://bugs.launchpad.net/openstack/+bug/2148312)

---

### Pattern 2: `sunbeam cluster deploy` false-negative 30-minute timeout after apparent convergence

**Symptom:**
```
+ sunbeam cluster deploy
wait timed out after 1799.9999981459987s
Error: wait timed out after 1799.9999981459987s
Process completed with exit code 1.
```

**Root cause:** `sunbeam cluster deploy` hit its internal ~30 minute wait deadline even though the deployment had effectively converged. The timeout snapshot in the GitHub log already showed the `openstack-machines` model in `available` state with all machine-model applications (`cinder-volume`, `k8s`, `microceph`, `openstack-hypervisor`, `sunbeam-machine`) `active`, and post-failure collection showed the `openstack-infra`, `openstack-machines`, and `openstack` models all active. This points to a false-negative readiness gate inside `sunbeam cluster deploy` rather than a workload actually failing.

**Evidence to look for:**
- GitHub failed log: `sunbeam cluster deploy` starts immediately after bootstrap, then fails exactly `1799.999...s` later with `wait timed out`
- The timeout snapshot dumped by the command shows `model_status=StatusInfo(current='available'...)` for `openstack-machines`
- The same timeout snapshot shows all machine-model apps and units `active`/`idle`
- Post-failure `generated/sunbeam/juju_status_openstack-infra.txt`, `juju_status_openstack-machines.txt`, and `juju_status_openstack.txt` show all models active; only some `mysql` units still report `Agent=executing` while workload stays `active`

<<<<<<< Updated upstream
**Observed in:** run 25950047554 (UUID 543ceda1-ee88-4631-ad00-8604348744c6, tor3-sqa-dedicated_maas dh1_j2, branch `main`, addon `sunbeam_2024.1_beta`, 2026-05-16).
=======
**Observed in:** run 25950047554 (UUID 543ceda1-ee88-4631-ad00-8604348744c6, tor3-sqa-dedicated_maas dh1_j2, branch `main`, addon `sunbeam_2024.1_beta`, 2026-05-16); run 26309155094 (UUID 43e3ca63-51e1-4731-81de-985c417f3206, tor3-sqa-dedicated_maas dh1_j6, branch `aipoc`, manifest channels `2024.1/beta`, 2026-05-22) — timeout snapshot already showed `openstack-machines` `available`, all listed apps `active`, and post-failure Juju/Kubernetes snapshots remained healthy.

---

### Pattern 3: `sunbeam cluster deploy` ends with generic `RemoteDisconnected` after Cinder/MySQL ingress wiring stalls

**Symptom:**
```
An unexpected error has occurred.
Error: ('Connection aborted.', RemoteDisconnected('Remote end closed connection without response'))
Process completed with exit code 1.
```

**Root cause:** During `sunbeam cluster deploy`, the OpenStack model began wiring the
`cinder-volume-mysql-router` backend-database relation. The MySQL leader (`mysql/0`) created
`mysql-primary` / `mysql-replicas` services, then immediately tried to resolve the new
`mysql-replicas` DNS name and crashed with `RuntimeError: Failed to resolve canonical name for
mysql-replicas`. Although Juju retried the hook, the deployment never fully converged: the
machine-model `cinder-volume` units kept reporting incomplete relations, and Traefik was still
logging multiple ingress relations as `not ready yet` at the exact failure window. The outer
`sunbeam cluster deploy` command eventually surfaced only a generic HTTP-layer
`RemoteDisconnected`, but the decisive underlying fault was the incomplete OpenStack relation /
ingress convergence triggered by the MySQL DNS-resolution race.

**Evidence to look for:**
- GitHub Actions log: `sunbeam cluster deploy` starts at `11:50:02Z` and fails at `12:50:56Z`
  with `('Connection aborted.', RemoteDisconnected(...))`
- `generated/sunbeam/juju_debug_log_openstack.txt`: at `12:19:53`, `mysql/0`
  `database-relation-changed` fails with `RuntimeError: Failed to resolve canonical name for
  mysql-replicas`
- `generated/sunbeam/juju_debug_log_openstack-machines.txt`: at `12:33:12`, all
  `cinder-volume/N` units still report `Relations {'amqp', 'database', 'identity-credentials'}
  incomplete`
- `pods_openstack_logs.tgz` → `logs-openstack-traefik-0.txt`: at `12:50:37`, several ingress
  relations are still `not ready yet`
- Post-failure `juju status`: `cinder-volume` remains blocked with `(amqp) integration missing`
  and `cinder-volume-mysql-router` remains blocked with `Missing relation: database`

**Observed in:** run 26282004470 (UUID 84bd779e-2150-40d6-b969-7128afb30ab4,
 tor3-sqa-dedicated_maas dh1_j6, branch `main`, 2026-05-22).

---

### Pattern 4: `sunbeam configure` Terraform apply fails while Neutron backends flap under Traefik

**Symptom:**
```
Error configuring cloud
Traceback (most recent call last):
  File "/snap/openstack/1004/lib/python3.12/site-packages/sunbeam/commands/configure.py", line 293, in run
    self.tfhelper.apply(reporter=context.reporter)
  ...
sunbeam.core.terraform.TerraformException: terraform command failed: /snap/openstack/1004/bin/terraform apply -input=false -auto-approve -no-color -json
stderr:
Process completed with exit code 1.
```

**Root cause:** `sunbeam cluster deploy` completed, but `sunbeam configure` started while the
Neutron API was still unstable behind Traefik. During the entire configure window, Traefik kept
removing `neutron-0` and `neutron-2` from the backend pool because `/healthcheck` requests timed
out, and the Neutron pods repeatedly fired Pebble `online` check failures that triggered
`neutron-server-pebble-check-failed` hooks. Although some health probes returned `200`, the
service was not stably healthy enough for Terraform-driven OpenStack API operations, so
`sunbeam configure` surfaced only a generic Terraform exception with empty stderr.

**Evidence to look for:**
- GitHub Actions log: `sunbeam cluster deploy` completes at `11:03:44Z`; `sunbeam configure`
  starts immediately and fails at `11:24:06Z` with `TerraformException`
- `pods_openstack_logs.tgz` → `logs-openstack-traefik-public-0.txt`: at `11:02:55Z` and
  `11:03:00Z`, Traefik removes `neutron-2` and `neutron-0` from the backend list due to
  `Client.Timeout exceeded while awaiting headers`; the flapping continues through `11:24:02Z`
- `pods_openstack_logs.tgz` → `logs-openstack-neutron-0.txt`: repeated Pebble `Check "online"
  failure ... timed out after 3s` events from `11:21:39Z` onward, with `Change ... Perform HTTP
  check "online" failed` at `11:21:59Z`, `11:23:22Z`, and `11:24:45Z`
- `pods_openstack_logs.tgz` → `logs-openstack-neutron-2.txt`: the same repeated Pebble `online`
  check timeouts, reaching threshold-triggered failures at `11:21:24Z` and `11:23:37Z`
- `generated/sunbeam/juju_debug_log_openstack.txt`: repeated `neutron-server-pebble-check-failed`
  hooks on `neutron/0` and `neutron/2` during `11:21-11:24`
- Post-failure `generated/sunbeam/juju_status_openstack.txt`: the model largely recovers, but
  `neutron/0` is still `Agent=executing`, showing configure raced ongoing Neutron convergence

**Observed in:** run 26355823715 (UUID 63cf459e-6a19-4b11-a859-368a4c80a9b8,
 tor3-sqa-dedicated_maas dh1_j6, branch `aipoc`, manifest channels `2024.1/beta`,
 2026-05-24).

---

### Pattern 5: `sunbeam configure` fails on Terraform state lock despite a healthy deployed cloud

**Symptom:**
```
An unexpected error has occurred.
Error: terraform command failed (state locked): /snap/openstack/1004/bin/terraform apply -input=false -auto-approve -no-color -json
stderr:
Process completed with exit code 1.
```

**Root cause:** `sunbeam cluster deploy` had already completed successfully, but the immediately-following `sunbeam configure` hit Terraform backend lock contention inside the openstack snap. The lock owner details were not surfaced (`stderr:` was empty), but the direct failure condition was explicit: the configure-time `terraform apply` found the state already locked. Post-failure Juju and Kubernetes snapshots showed the deployed cloud itself was healthy, so this was a false-negative configure failure caused by Terraform state locking rather than a broken OpenStack deployment.

**Evidence to look for:**
- GitHub Actions log: `sunbeam cluster deploy` completes at `11:54:23Z`; `sunbeam configure` starts immediately; at `12:03:50Z` it fails with `terraform command failed (state locked)`
- `generated/sunbeam/juju_status_openstack-infra.txt`: at `12:54Z`, all `sunbeam-clusterd` units are `active/idle`
- `generated/sunbeam/juju_status_openstack-machines.txt`: at `12:54Z`, all machine-model apps are `active`, all 6 machines are `started`, and all units are `idle`
- `generated/sunbeam/juju_status_openstack.txt`: at `12:53Z`, the OpenStack control-plane applications and units are all `active/idle`
- `generated/sunbeam/kubectl_get_pod.txt`: all OpenStack pods are `Running` at snapshot time

**Observed in:** run 26388151075 (UUID f7410bbd-27e3-442c-9df6-885815701650,
 tor3-sqa-dedicated_maas dh1_j2, branch `aipoc`, openstack snap rev `1004`,
 `2024.1/beta`, 2026-05-25).

---

### Pattern 6: `sunbeam cluster deploy` times out because storage node curtin late command fails with `ceph-bluestore-tool` permission error

**Symptom:**
```
Error: Timeout waiting for machines to deploy.
Process completed with exit code 1.
```

**Root cause:** `sunbeam cluster deploy` waited for all `openstack-machines` nodes to finish MAAS deployment, but the storage-only node `suicune` never completed curtin. Its host-specific curtin userdata ran a late command that installed `ceph-osd` and then executed `ceph-bluestore-tool zap-device` against `/dev/disk/by-id/wwn-0x600508b1001c2289bcab3857b9bd9e4c`. On this run the command failed inside curtin with `Operation not permitted`, so MAAS marked the node failed during `Deploying: Configuring OS`. Because `suicune` remained in `Failed deployment`, `sunbeam cluster deploy` eventually hit its machine-deploy timeout.

**Evidence to look for:**
- GitHub Actions log: `sunbeam cluster bootstrap` completes, `sunbeam cluster deploy` starts immediately, then fails ~60 minutes later with `Error: Timeout waiting for machines to deploy.`
- `generated/sunbeam/juju_debug_log_openstack-machines.txt`: machine `5` / instance (for example `dtpqgp` or `brd87a`) progresses through `Deploying: Loading ephemeral` → `Installing OS` → `Configuring OS`, then flips to `Failed deployment: Marking node failed - Installation failed ...`
- `generated/sunbeam/juju_machines_openstack-machines.json`: machine `5` shows `display-name: suicune`, the run-specific instance id, and final state `provisioning error` / `Failed deployment: Loading ephemeral`
- `generated/maas/logs-*.tgz` → `10.241.128.2/var/log/syslog`: during the failure window, curtin logs the exact command `ceph-bluestore-tool zap-device --dev /dev/disk/by-id/wwn-0x600508b1001c2289bcab3857b9bd9e4c --yes-i-really-really-mean-it`, then `error from zap: (1) Operation not permitted`, followed by `finish: cmd-install/stage-late/driver_51_osd_zap/cmd-in-target: FAIL`
- `suicune.dh1-j2.tor3-sqa-dedicated-maas.solutionsqa-curtin_config.txt`: `driver_51_osd_zap` is present in the host-specific curtin late commands for `suicune`

**Observed in:** run 26687730876 (UUID 99a74612-fa41-4cd0-8013-a63535b1db5b, tor3-sqa-dedicated_maas dh1_j2, branch `main`, 2026-05-30) — `suicune` failed `driver_51_osd_zap` on host `noma` at `17:47:26Z`; run 26677146886 (UUID cb9f85e4-5326-4528-bac0-fb34bec2c24d, tor3-sqa-dedicated_maas dh1_j2, branch `main`, 2026-05-30) — `suicune` failed `driver_51_osd_zap` on host `anahuac` at `09:03:37Z`; run 26721324961 (UUID 6d4c2445-ec78-44d9-9fbc-6ad73fb19004, tor3-sqa-dedicated_maas dh1_j2, branch `main`, 2026-05-31); run 26666874596 (UUID e2855722-2c0c-4c23-8872-6a3fc72ed2ae, tor3-sqa-dedicated_maas dh1_j2, branch `main`, 2026-05-30); run 26644898503 (UUID 96f56fa7-cce5-48b5-9ec2-28a41cfbbbdf, tor3-sqa-dedicated_maas dh1_j2, branch `main`, 2026-05-29) — machine `5` / instance `akxawm` (`suicune`) failed `driver_51_osd_zap` on host `anahuac` at `17:23:19Z`, with MAAS syslog logging `ceph-bluestore-tool zap-device` returning `Operation not permitted` before the node flipped to `Failed deployment`.
>>>>>>> Stashed changes

---

_Add more patterns below as they are discovered._

## Notes

- The `15m` timeout in `juju wait-for application controller` is hardcoded inside the
  openstack snap's `sunbeam cluster bootstrap` implementation. It cannot be changed
  without modifying the snap.
- The MAAS node deploy sequence is: `juju-1` first (bootstrap), then `juju-2` and
  `juju-3` simultaneously (enable-ha). The critical path is from when the simultaneous
  pair starts deploying to when the slower one's Juju agent is ready.
- Cloud-init on deployed VMs includes Telegraf installation via `late_commands` — this
  requires reaching an external apt repo and can add 1–2 minutes on top of the base
  curtin install time.
- Physical KVM hosts are Pokémon-named: `anahuac` (10.241.128.2), `sunset`
  (10.241.128.3), `noma` (10.241.128.4). VM serial logs for each juju node are in the
  MAAS tgz under `var/log/libvirt/qemu/juju-N-serial0.log` on the respective host.

### Pattern 7: `sunbeam cluster bootstrap` fails with `aiohttp` 5-minute timeout due to dead connection pool (TCP idle timeout)

**Symptom:**
```
An unexpected error has occurred. Please see https://canonical-openstack.readthedocs-hosted.com/en/latest/how-to/troubleshooting/inspecting-the-cluster/ for troubleshooting information.
Error: 
```

**Root cause:** `sunbeam cluster bootstrap` utilizes a MAAS client (`python-libmaas`, backed by `aiohttp`) which pools TCP connections. The client is used early during the command (e.g., validating hardware) and then sits idle for 20+ minutes while `juju bootstrap` and `sunbeam-clusterd` deploy. When the `Add machines` step begins, `aiohttp` attempts to reuse the 20-minute idle TCP connection to the MAAS API. Because a stateful firewall, NAT, or load balancer has silently dropped the idle connection state, the `GET /MAAS/api/2.0/machines/` request is blackholed. `aiohttp` waits for exactly 300 seconds (its default `ClientTimeout`), receives no response, and raises a `TimeoutError`.

**Evidence to look for:**
- GitHub Actions log: `sunbeam cluster bootstrap` pauses for exactly 5 minutes immediately after the `Set OVN provider` step, then fails.
- `generated/sunbeam/all_snaps.tgz` -> `home/ubuntu/snap/openstack/common/logs/sunbeam-*.log`:
  - Shows MAAS client activity (e.g. `Root device is a physical device`) at the start of the command.
  - Shows a ~20+ minute gap where the MAAS client is not used (while Juju is active).
  - Shows `Starting step 'Add machines'` followed exactly 300 seconds later by a `TimeoutError` traceback pointing to `aiohttp.streams.readany`.

**Observed in:** run 26710851237 (UUID 0a948444-837e-4dd6-8d31-4642581e907e, tor3-sqa-dedicated_maas dh1_j2).

### Pattern 8: `sunbeam cluster deploy` fails because Juju application deployment gets a Charmhub connection EOF

**Symptom:**
```
Error configuring cloud: TerraformException()
Error: terraform command failed: /snap/openstack/1015/bin/terraform apply -input=false -auto-approve -no-color -json
stderr: 
```

**Root cause:** During the Terraform apply phase of `sunbeam cluster deploy`, the Juju controller attempts to contact Charmhub via `https://api.charmhub.io/v2/charms/refresh` to resolve and fetch the required charms (such as `glance-k8s`). If Charmhub abruptly closes the connection (`EOF`) or experiences a transient service disruption, Juju's internal resolution attempt limit is exceeded. This causes the Juju Terraform provider to fail to create the application resource, bubbling up as a generic `TerraformException()` inside the sunbeam execution runner.

**Evidence to look for:**
- `generated/sunbeam/all_snaps.tgz` -> `home/ubuntu/snap/openstack/common/etc/<cluster-env>/deploy-openstack/terraform-apply-*.log`:
  - Logs a Juju provider Client Error: `[ERROR] provider.terraform-provider-juju_...: Response contains error diagnostic: diagnostic_severity=ERROR diagnostic_summary="Client Error" ... diagnostic_detail="Unable to create application, got error: resolving with preferred channel: attempt count exceeded: Post \"https://api.charmhub.io/v2/charms/refresh\": EOF"`
  - Specifically targets a module (e.g., `vertex "module.glance.juju_application.service" error: Client Error`).
- GitHub Actions log: `sunbeam cluster deploy` fails with `TerraformException()` and empty stderr.

**Observed in:** run 27377752955 (UUID b5182e15-8b17-49fd-b78c-9a37c947ac7d, tor3-sqa-dedicated_maas dh1_j6, branch `main`, 2026-06-11).
>>>>>>> Stashed changes
