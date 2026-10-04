# Step Knowledge: sunbeam_launch_vm

## Step Overview

Launches a test Ubuntu VM inside the deployed Sunbeam OpenStack cloud using
`sunbeam launch ubuntu -n test-instance`, then verifies that the VM is SSH-accessible
by connecting through the bootstrap node as a jump host. This validates end-to-end VM
lifecycle: Nova scheduling, image boot, network wiring, and cloud-init.

The script is `products/sunbeam/launch_vm.py`. For MAAS substrates the bootstrap node
is the first entry in `nodes.yaml`; for cloud substrates the runner connects directly.

## Swift Artifacts

| Path | Description | When to check |
|---|---|---|
| `generated/sunbeam/output.log` | FCE build log for the sunbeam layer (log collection phase) | Always — first stop |
| `generated/sunbeam/juju_status_openstack.txt` | Juju status of OpenStack model | Check charm health |
| `generated/sunbeam/show_units_openstack.txt` | `juju show-units` for OpenStack | Unit agent status |
| `generated/sunbeam/kubectl_get_pod.txt` | Kubernetes pod state | Pod-level failures |
| `generated/sunbeam/pods_openstack_logs.tgz` | Per-pod container logs | Nova/Neutron/OVN errors |
| `generated/version_collector_sunbeam_launch_vm.log` | Snap/apt versions at layer end | Version forensics |

## Key Log Files

| File | What it contains | When to use |
|---|---|---|
| `pods_openstack_logs.tgz` → `logs-openstack-nova-*.txt` | Nova compute/API logs | VM scheduling failures |
| `pods_openstack_logs.tgz` → `logs-openstack-neutron-*.txt` | Neutron logs | Port binding / network wiring |
| `pods_openstack_logs.tgz` → `logs-openstack-ovn-central-*.txt` | OVN north/south logs | Routing table failures |
| `pods_openstack_logs.tgz` → `logs-openstack-nova-api-*.txt` | Nova API logs | MessagingTimeout, BUILD failures |

## Grep Patterns

```bash
WDIR=<work_dir>/<uuid>

# Find the SSH error in GitHub Actions log
grep "No route to host\|Connection refused\|Could not get the proper response" \
  <work_dir>/run_<run_id>_failed.log

# Count total retry attempts
grep -c "No route to host\|Connection refused" <work_dir>/run_<run_id>_failed.log

# Find the IP returned by sunbeam launch
grep "ubuntu@" <work_dir>/run_<run_id>_failed.log | grep "INFO.*localhost" | head -3

# Check OpenStack charm health at time of failure
grep -E "error|blocked|waiting" $WDIR/generated/sunbeam/juju_status_openstack.txt

# Search Nova logs for the test instance
tar -xzf $WDIR/generated/sunbeam/pods_openstack_logs.tgz -C <work_dir>/openstack-pods/
grep -l "test-instance\|10\.243\." <work_dir>/openstack-pods/logs-openstack-nova-*.txt
```

## Known Failure Patterns

### Pattern 1: VM launched but SSH returns "No route to host"

**Symptom:**
```
INFO  - [localhost]: `ssh -i /home/ubuntu/snap/openstack/956/sunbeam ubuntu@<IP>`
ERROR - [localhost] Command failed: ssh ... solqa-shared-maas-server-01.maas -- \
  ssh -i .../sunbeam ubuntu@<IP> -o StrictHostKeyChecking=no -- hostname
STDERR: ssh: connect to host <IP> port 22: No route to host
...
Exception: Could not get the proper response
```

**Root cause:** `sunbeam launch` successfully creates the VM in Nova and returns an IP,
but the VM's network path is never established. The jump host (`solqa-shared-maas-server-01.maas`)
receives a hard `No route to host` (ICMP unreachable) — not a timeout or refused
connection — indicating no routing exists to the VM's IP at all. This is distinct from
the VM being up but SSH not yet ready. Possible causes: OVN logical switch port not
bound, Neutron not pushing flow rules to the compute node, or cloud-init failing to
bring up the guest network interface.

All 30 retries (10s apart) fail consistently over ~11 minutes; the VM never becomes
reachable.

**Evidence to look for:**
- GitHub Actions log: all retry SSH calls return `No route to host` (not `Connection refused`)
- `juju_status_openstack.txt`: all OpenStack charms should be `active` (control plane healthy — routing failure is below the charm layer)
- `pods_openstack_logs.tgz` → Nova logs: verify the VM reached `ACTIVE` status and was scheduled to a compute node
- `pods_openstack_logs.tgz` → Neutron/OVN logs: look for port binding errors or missing flow-table entries around the VM creation timestamp

**First observed:** run 23688090524 (UUID 6e7c84f2, tor3-sqa-shared_maas dh1_j8_1, branch main, 2026-03-28)

**Confirmed again (fabric variant):** run 33096607971 (UUID 1cc1f490-753d-4409-9c87-42df07fc6adb,
tor3-sqa-testflinger cluster_1, branch sunbeam_charm_upgrades, 2026-08-27). `sunbeam launch`
returned `ubuntu@10.242.2.153`; the VM was ACTIVE on `timburr.maas` with cloud-init finished,
OVN NAT/localnet/port-binding/SGs all correct, and every charm and pod healthy. The break was
**outside the cloud**: the lab fabric did not deliver the flat provider network `10.242.2.0/24`
(`physnet1`, NIC `eth1`) to the segment the elected OVN gateway chassis (`weyl.maas`) was
plugged into. `tcpdump -i eth1` on weyl showed only unanswered outbound
`ARP, Request who-has 10.242.2.1 tell 10.242.2.182` and **zero inbound packets** for the FIP;
`ip route get 10.242.2.1` resolved `via 10.241.2.1 dev eno1`, i.e. the declared external
gateway lived on the management path, not the provider segment.

> **⚠️ Known false lead for this variant.** `juju_debug_log_openstack-machines.txt` logs an
> install-hook line `['eth1', 'br-ex']` per `openstack-hypervisor` unit. In the run above it is
> absent for `openstack-hypervisor/0` (timburr — the VM's compute host and the SSH jump host),
> which invites the conclusion that timburr's `eth1` was never attached to `br-ex`. **It is not
> the cause.** In OVN, floating-IP north-south traffic is handled by the elected *gateway
> chassis*, not the VM's compute host; here that was `weyl.maas` (`openstack-hypervisor/2`),
> which did log the line and whose `br-ex` was verified correct. Confirm the gateway chassis
> (OVN NB `logical_router_port` → `status: {hosting-chassis=...}`) before blaming any node's
> bridge wiring. The missing line on `/0` correlates with an earlier charm-download retry storm
> (`failed to download charm ... archiveSha256 length 0 not valid`) and is a log-capture artifact.

**Confirmed again:** run 26644918393 (UUID f34d99dd-687b-4324-961b-2cdc1ce10bac, tor3-sqa-testflinger cluster_3, branch main, 2026-05-29). In this occurrence, `sunbeam launch` returned `ubuntu@10.242.4.191`, Neutron associated that floating IP to port `ac1bbca9-d65b-4cd3-83e0-f8a32cce0a69`, and Nova recorded `network-vif-plugged` events for instance `7ce0d1b2-ff72-4289-942a-3224c7caf851` on `jasperoid.maas`, yet all 30 SSH probes from bootstrap node `octopot.maas` still failed with `No route to host`.

---

### Pattern 2: VM never reached ACTIVE (Nova BUILD timeout)

**Symptom:**
```
Instance creation request failed: Timeout waiting for
Server:<nova-server-id> to transition to ACTIVE
Error: Unable to request new instance. Please run `sunbeam configure` first.
```
The `sunbeam launch` command exits after ~6m15s with an empty stdout (`b''`).
No SSH connection string is ever printed. The `CalledProcessError` propagates
and fails the step.

**Key distinction from Pattern A:** The VM never reached `ACTIVE` at all — there
is no IP address to SSH to. Pattern A involves a VM that is `ACTIVE` but
unreachable via SSH. Pattern B is a failure earlier in the lifecycle (BUILD stage).

**Root cause:** Nova accepted the instance creation request (scheduler found a
valid host and assigned a server ID) but the nova-compute agent on the target host
failed to spawn the VM within the timeout. Likely causes:
- nova-compute agent down or hung on the scheduled hypervisor
- Libvirt/KVM failing to spawn (disk I/O, storage, or bridge unavailable)
- Glance image download stalled inside the compute node
- Resource exhaustion (RAM/disk) on the compute host

**The misleading "Please run `sunbeam configure` first" message** is a generic
catch-all error from `sunbeam launch` when the launch fails for any reason. It
does not indicate the cluster was actually unconfigured — `sunbeam_deploy`
completed successfully immediately before this step.

**Evidence to look for:**
- GitHub Actions log: `Timeout waiting for Server:<nova-server-id> to transition to ACTIVE`,
  STDOUT is `b''` (grep for `ubuntu@` never matched)
- `juju_status_openstack.txt`: check if any nova-compute unit is in `error` state
- `pods_openstack_logs.tgz` → `logs-openstack-nova-compute-*.txt`: search for
  `<nova-server-id>` to identify which compute node was targeted and why spawn failed

**First observed:** run 24664528578 (UUID a44c1e26-624b-4dbc-84c9-d6f14dce8ce1,
tor3-sqa-testflinger cluster_2, branch main, 2026-04-20)

---

### Pattern 3: Instance create succeeded, but later Nova status poll returned 504 while neutron was flapping

**Symptom:**
```
Instance creation request failed: HttpException: 504: Server Error for url:
http://<public-openstack-ip>:80/openstack-nova/v2.1/servers/<server-id>,
504 Gateway Timeout: The gateway did not receive a timely response
Error: Unable to request new instance. Please run `sunbeam configure` first.
```
The command still exits with empty stdout (`b''`), so `launch_vm.py` never reaches
its SSH retry loop.

**Root cause:** The initial `POST /servers` succeeded and Nova began building the
instance, but a later poll of `GET /servers/<server-id>` through the public
Traefik/Nova path timed out. In the same window, `neutron-2` was unhealthy:
`kubectl_get_pod.txt` showed `neutron-2` at `1/2 Running`, the neutron charm kept
firing `neutron-server-pebble-check-failed`/`recovered`, and `traefik-public`
reported repeated health-check timeouts against `neutron-2:9696/healthcheck`.
This points to transient OpenStack control-plane instability during launch rather
than a missing Sunbeam configuration.

**Evidence to look for:**
- GitHub Actions log: 504 on `GET /openstack-nova/v2.1/servers/<server-id>`
- Nova pod logs: `POST /openstack-nova/v2.1/servers` returned `202`, proving the
  create request was accepted
- Nova pod logs: later `network-vif-plugged` events for the same instance show
  Neutron was still wiring the VM on a specific host
- `kubectl_get_pod.txt`: `neutron-2` degraded (`1/2 Running`)
- `logs-openstack-traefik-public-*.txt`: repeated `Client.Timeout exceeded while
  awaiting headers` for `neutron-2` health checks in the failure window

**First observed:** run 26309147742 (UUID 3a40980c-7461-46e7-b0a2-8eaaa707e53e,
tor3-sqa-testflinger cluster_1, branch aipoc, 2026-05-23)

---

### Pattern 4: Runner-side SSH session disconnected, but remote `sunbeam launch` continued and succeeded

**Symptom:**
```
DEBUG - [localhost]: ssh -t ... solqa-shared-maas-server-10.maas -- TERM=dumb sunbeam launch ubuntu -n test-instance '|' grep '"ubuntu@"'
ERROR - [localhost] Command failed: ssh ... returned non-zero exit status 255.
STDOUT: b''
STDERR: Pseudo-terminal will not be allocated because stdin is not a terminal.
```
The GitHub Actions step fails about 16 seconds after starting the SSH command, before
`launch_vm.py` reaches its own 30-attempt SSH-to-VM loop.

**Root cause:** The failure is a false negative in the runner-side transport/wrapping,
not an OpenStack launch failure. On the bootstrap node, `auth.log` shows the runner's
SSH client connected successfully and then **disconnected by user** at the exact failure
moment. However, the bootstrap node's own Sunbeam CLI log shows the same `sunbeam launch`
command kept running after that disconnect: Nova accepted the create request, the server
reached `ACTIVE`, and Neutron associated floating IP `10.243.37.136`. The runner failed
because the SSH session/piped `grep "ubuntu@"` wrapper exited early, so the pipeline
never saw the eventual success output.

**Evidence to look for:**
- GitHub Actions log: `ssh ... sunbeam launch ... | grep "ubuntu@"` exits `255` with empty stdout
- Bootstrap `var/log/auth.log`: `Accepted publickey` followed by `Received disconnect ... disconnected by user`
- Bootstrap `home/ubuntu/snap/openstack/common/logs/sunbeam-<timestamp>.log`: `POST /openstack-nova/v2.1/servers` returns `202`
- Same Sunbeam log: server reaches `ACTIVE`, gets fixed IP, then floating IP is created and associated
- Neutron pod logs: floating IP association succeeds for the same server/port after the runner has already failed

**First observed:** run 26401300048 (UUID b0948bbd-e549-4004-8668-993513adf7b0,
 tor3-sqa-shared_maas dh1_j8_2, branch main, openstack snap rev 1004, 2026-05-26)

---

---

### Pattern 5: FIP unreachable because ovn-northd never programmed the NAT into the southbound DB

**Symptom:**
```
`ssh -i /home/ubuntu/snap/openstack/1094/sunbeam ubuntu@10.243.10.191`
ssh: connect to host 10.243.10.191 port 22: Connection timed out   (all 30 attempts)
Exception: Could not get the proper response
```
`sunbeam launch` succeeds and returns an IP; the VM is ACTIVE, cloud-init finishes and
sshd listens; every charm, pod and agent is healthy. Every SSH probe **times out**
(silent drop) rather than returning `No route to host`.

**Root cause:** ovn-northd generated the router's own flows (ARP responder / ICMP / SNAT
for the router port IP) but produced **no logical flow for the floating IP at all** — no
`ct_dnat` and no NAT ARP responder. Nothing on the provider VLAN answers ARP for the FIP,
so the physical gateway floods the request into the router datapath where it is dropped
(`lr_in_ip_input`). The OVN **northbound** DB `dnat_and_snat` entry is present and correct,
which makes the control plane look perfectly healthy.

Observed trigger: shortly before the FIP was created, the external router port was
converted from an HA gateway configuration to a single `l3gateway` chassis — the
`chassisredirect` port binding (`cr-lrp-<lrp-uuid>`, with an `ha_chassis_group`) was
**deleted** and the port re-created with `options: {l3gateway-chassis=<node>}`, while all
nodes except the elected gateway chassis tore down their `br-int ↔ br-ex` localnet patch
pairs. The first FIP created after that transition was never programmed. Upstream defect in
OVN 26.03.0 / microovn snap rev 1133.

**Evidence to look for:**
- `sosreport-<node>/var/snap/microovn/common/data/central/db/ovnsb_db.db`: the FIP address
  appears **only** in `Port_Binding` rows (`nat_addresses`, `neutron:port_fip`) and in
  **no** `"match"` string; all flow matches on the external CIDR reference only the router
  port IP (e.g. `arp.tpa == <lrp-ip> && arp.op == 1`).
  ```bash
  python3 - <<'EOF'
  import re; d=open('ovnsb_db.db','rb').read().decode('utf8','replace')
  print(len(re.findall(r'<FIP>', d)))
  print({m.group(1) for m in re.finditer(r'"match":"([^"]{0,200})"', d) if '<EXT-PREFIX>' in m.group(1)})
  EOF
  ```
- `ovnnb_db.db`: `dnat_and_snat` for the FIP present and correct; **no** top-level
  `external_mac` and no `enable_distributed_floating_ip` ⇒ centralized FIP, so the gateway
  chassis (not the compute node) owns the path.
- SB raft log: `{"Port_Binding":{"<uuid>":null}}` deleting the `cr-lrp-...` row in the same
  transaction that sets `type: l3gateway` / `l3gateway-chassis`. Note the `_date` epoch and
  compare with the FIP creation time.
- `var/log/syslog` on each node: `deleted interface patch-br-int-to-provnet-<uuid>` on all
  nodes except the gateway chassis, at the same second.
- Confirming healthy-but-irrelevant signals: SB `MAC_Binding` for the external gateway IP
  (proves provider-VLAN L2 works), guest `console.log` showing `Listening on ssh.socket`,
  NB ACL `tcp.dst == 22` allow, `eth1` enslaved to `br-ex` on all hypervisors.

**Recovery (for a live environment):** `ovn-appctl -t ovn-northd inc-engine/recompute` on
the active northd node, then re-check for the FIP's flows; failing that, unset/set the FIP
on the port.

**⚠️ Analysis trap:** every in-cloud link verifiable from the artifact bundle looks healthy,
which makes "the lab fabric/routing is broken" a very tempting — and wrong — conclusion.
Always grep the SB DB for the FIP before blaming the fabric.

**First observed:** run 33455469340 (UUID f34164a0-b329-4763-859e-70ab7304081a,
tor3-sqa-dedicated_maas dh1_j6, branch main, 2026-09-01). Gateway chassis `spearow`,
VM on `quilava`, northd on `elgyem`, FIP `10.243.10.191`, external net physnet1 VLAN 2744.

---

### Pattern 6: Nova os-keypairs returns 503 because Keystone is failing during a post-resize MySQL cluster-set wedge

**Symptom:**
```
Error: HttpException: 503: Server Error for url:
http://<traefik-public-ip>:80/openstack-nova/v2.1/os-keypairs,
503 Service Unavailable: The Keystone service is temporarily unavailable.
```
`sunbeam launch ubuntu -n test-instance` fails before an instance is requested and
before any `ubuntu@<ip>` line is emitted. The failing request is usually the launch
tool's keypair setup/check, not VM scheduling.

**Root cause:** the preceding `sunbeam cluster resize` scaled mysql-k8s out and one
or more new MySQL members failed to attach to the InnoDB cluster. The clone fallback
does not recover cleanly, leaving all mysql units unable to fetch cluster/cluster-set
topology. The mysql-router units then repeatedly hit MySQL `1040 Too many connections`
from `Dba.get_cluster_set`, which can make Keystone return HTTP 500 to Nova's token
validation. Nova converts that Keystone 500 into the user-visible 503 on
`/openstack-nova/v2.1/os-keypairs`.

**Evidence to look for:**
- GitHub Actions / `output.condensed.log`: `sunbeam launch ubuntu -n test-instance`
  fails with 503 on `/openstack-nova/v2.1/os-keypairs` and `The Keystone service is
  temporarily unavailable`.
- Nova API pod log: `keystonemiddleware.auth_token` logs `Bad response code while
  validating token: 500`, followed by requestlog `POST /openstack-nova/v2.1/os-keypairs`
  with `status: 503`.
- `pods_openstack_logs.tgz` → `logs-openstack-mysql-*.txt`: a newly added mysql unit
  logs `Failed to stop instance replication`, `Failed to attach instance ... to cluster`,
  then `Failed to add instance to cluster. Fallback to clone method`.
- All mysql units continue logging `Failed to fetch cluster status` /
  `Failed to fetch cluster set status` /
  `MySQLGetClusterEndpointsError: Failed to get endpoints from cluster topology`.
- Many `*-mysql-router` charm logs show `MySQL Error (1040): Dba.get_cluster_set:
  Too many connections`; Juju status may show router units in `error` with
  `hook failed: "update-status"` even while workload pods are `Running`.

**First observed:** run 35758835752 (UUID 830e85f7-3c3a-43c4-a3a0-9e062168fb01,
tor3-sqa-testflinger cluster_2, branch moretraefik, 2026-09-22). `sunbeam_launch_vm`
failed at 22:01:06; mysql-1/mysql-2 attach failures occurred during resize around
21:42-21:43, and cluster-status/router 1040 errors persisted through log collection.

## Notes

- The error `No route to host` is network-level (ICMP unreachable) and differs from
  `Connection refused` (VM up, SSH not ready yet) or timeout (firewall drop). The
  distinction is important for diagnosing where the wiring failed.
- `launch_vm.py` does not check `openstack server show` — it jumps straight to SSH
  probing. A VM in `ERROR` state will also produce `No route to host` indefinitely.
  Adding `openstack server show <id>` on failure would surface the Nova fault message
  without needing Swift access.
- The jump host for MAAS substrates is always the Sunbeam bootstrap node; for
  cloud substrates the runner connects directly.
- Sunbeam snap revision at time of first observation: `956`
- A **timeout** (silent drop) on every probe, with a healthy control plane, points at Pattern 5
  (FIP absent from the OVN southbound DB) before it points at the lab fabric.
