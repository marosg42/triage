# Step Knowledge: canonicalk8s_test

## Step Overview

Runs the canonical-k8s (`k8s` / `k8s-worker` charms) integration test suite with pytest +
jubilant against an already-deployed model (usually `canonicalk8s-maas`, built by the
`canonicalk8s_maas` step). Invoked roughly as:

```
pytest -k test_k8s.py --no-deploy --model canonicalk8s-maas ...
```

Several tests (`test_remove_non_leader_control_plane`, worker add/remove tests) **remove and
re-add units**, which forces MAAS to release and immediately re-deploy a physical/virtual node.
That release/re-allocate cycle is where most substrate failures in this step surface — the test
itself is often an innocent bystander.

## Swift Artifacts

| Path | Description | When to check |
|---|---|---|
| `generated/canonicalk8s_test/log/3-commands[0].log` | The full pytest + jubilant run log (large, ~1.5 MB). Contains the `wait:` status-diff stream, the `FAILURES` section, and the crashdump teardown | Always — first stop |
| `generated/canonicalk8s_test/log/1-uv-sync.log`, `2-freeze.log` | Test venv build / dependency versions | Only for import/dependency errors |
| `generated/canonicalk8s-maas/` | Deploy-layer logs for the model under test | If the cluster was already unhealthy before the tests |
| `generated/juju_maas_controller/` | Controller crashdumps, DB dump | Controller-side problems |
| `generated/maas/logs-<timestamp>.tgz` | Full MAAS infra logs (virtual_maas / dedicated_maas only) | Any machine stuck in `allocating` / `Deploying` |
| `generated/sshtest.txt` | SSH tunnel health (virtual_maas) | Always, to confirm the MAAS archive is trustworthy |

## Key Log Files (inside `logs-*.tgz`)

| File | What it contains | When to use |
|---|---|---|
| `var/log/syslog` | Everything: `dhcpd`, `maas-agent`, `maas-rackd`, `maas-regiond`, `maas-http`, `maas-temporal-worker` | Primary investigation |
| `var/log/syslog` → `maas-http[...]` lines | nginx access log for the image/metadata server — shows the exact PXE boot stage reached | Machine stuck in "Performing PXE boot" |
| `var/log/syslog` → `dhcpd[...]` lines | Full DHCPDISCOVER/OFFER/REQUEST/ACK/NAK transactions | Any netboot problem |
| `var/log/syslog` → `maas-agent[...]` lines | Temporal activities incl. `apply-dhcp-config-via-omapi` | DHCP config divergence |

## Grep Patterns

```bash
T=files/<UUID>/generated/canonicalk8s_test/log/'3-commands[0].log'
M=files/<UUID>-maas-logs   # extracted logs-*.tgz

# 1. Which test failed and why
grep -n "FAILED\|Traceback\|TimeoutError\|passed," "$T"

# 2. The juju status snapshot printed with the TimeoutError — find the stuck machine/unit
#    (look for machine_status current='allocating' and units 'waiting for machine')
grep -n "machine_status=StatusInfo(current='allocating'\|waiting for machine" "$T"

# 3. How far the stuck machine got in PXE boot (substitute its IP / MAC)
grep -ah "maas-http\[" $M/*/var/log/syslog | grep -aE "10\.241\.144\.(85|91) - -"
#    Healthy sequence: ipxe.cfg -> ipxe.cfg-<MAC> -> boot-kernel -> boot-initrd -> squashfs
#    Missing 'squashfs' == the ephemeral initramfs never got a usable network.

# 4. DHCP livelock / rack divergence  (ALWAYS use -a: these syslogs are detected as binary)
grep -ah "DHCPNAK" $M/*/var/log/syslog | grep -a "<DATE>" \
  | grep -aoP "DHCPNAK on \S+ to \S+" | sort | uniq -c | sort -rn
grep -ah "dhcpd\[" $M/*/var/log/syslog | grep -a "<MAC>" | sed 's/.*dhcpd\[[0-9]*\]: //'

# 5. MAAS DHCP config push divergence between rack peers
grep -ahi "already existing host\|apply-dhcp-config-via-omapi" $M/*/var/log/syslog
grep -ah "failover peer" $M/*/var/log/syslog | grep -a "<DATE>"

# 6. Confirm boot images are not the problem — compare hashes with a successful deploy
grep -ah "boot-kernel\|boot-initrd\|squashfs" $M/*/var/log/syslog | grep -a "maas-http\["
```

## Known Failure Patterns

### Pattern 1: MAAS HA-DHCP release/re-allocate NAK loop (node stuck at "Performing PXE boot")

**Symptom:**
```
FAILED tests/integration/test_k8s.py::test_remove_non_leader_control_plane
       - TimeoutError: wait timed out after 2400s
```
with the printed status showing one machine frozen at
`machine_status=StatusInfo(current='allocating', message='Deploying: Performing PXE boot')`
and the new unit at `waiting / 'waiting for machine'`, while every other unit is `active/idle`.

**Root cause:** After the test removes a unit, MAAS releases the node and immediately re-deploys
it. MAAS pushes the new DHCP host mapping to both rack controllers over OMAPI, but the
**secondary rack skips it** — `WRN Ignoring already existing host: <MAC> ...
ActivityType=apply-dhcp-config-via-omapi` — and keeps the node's *pre-release* address. The
primary rack serves the new address. Both dhcpds then NAK the address the other hands out, so the
ephemeral initramfs never settles on a lease, never downloads the root `squashfs`, and the
deployment hangs until MAAS marks the node `Failed deployment` (~57 min) — long after the test's
2400 s wait expires. ISC failover reports "Both servers normal" throughout, so failover state is
**not** a reliable indicator.

**Evidence to look for:**
- `maas-http` access lines: the node fetches `ipxe.cfg`, `ipxe.cfg-<MAC>`, `boot-kernel`,
  `boot-initrd` — and then **no `squashfs` request ever**. Successful deploys fetch `squashfs`
  (~277 MB) within ~5 s of `boot-initrd`.
- `dhcpd`: a repeating cycle, with each rack rejecting the other's address —
  `DHCPOFFER on <newIP>` (primary) / `DHCPOFFER on <oldIP>` (secondary), and
  `DHCPREQUEST for <ip> (<other-rack>) ... : lease <ip> unavailable` → `DHCPNAK`.
  Tens to hundreds of NAKs, **all for a single MAC** — no other machine is affected.
- `maas-agent` on the secondary rack only:
  `WRN Ignoring already existing host: <MAC> ... WorkflowID=configure-dhcp:<secondary_rack_id>`
  with no counterpart on the primary.
- Often preceded, at release time, by
  `ERR Activity error. ActivityType=apply-dhcp-config-via-omapi ...
  "unable to decode: invalid IP address: None"` on **both** racks.
- `maas-apiserver`: `Lease update: commit for <oldIP> ...` and `... <newIP> ...` alternating for
  the same MAC and hostname.
- The node goes completely silent on DHCP some ~15 min in (initramfs gives up), then nothing until
  the test timeout.
- Boot image hashes (`/images/<hash>/...`) match those of machines that deployed successfully
  earlier in the same run — i.e. image import/sync is **not** the cause.

**Known bug:** **[LP #2164644](https://bugs.launchpad.net/maas/+bug/2164644)** — "DHCP: releasing
and re-deploying a machine can leave a rack with an old host lease". Filed 2026-08-20 by alemar99
*from SolQA canonical-k8s runs*, i.e. this exact test. Triaged / High, milestones 3.8.0 and 3.7.x,
**no fix branch as of 2026-08-28**. Two compounding defects:
1. On release MAAS sets the auto IP to `None` and calls `configure-dhcp`; the OMAPI path
   crash-loops (`invalid IP address: None`).
2. On re-deploy `apply-dhcp-config-via-omapi` swallows `HostAlreadyExists`
   (`Ignoring already existing host`), so `AddHost` with the new IP↔MAC binding is dropped.
Meanwhile a competing `ConfigureDHCPForAgentWorkflow` (same ID `configure-dhcp:<rack_system_id>`)
is stopped and re-issued as a **full reload on one rack only**, rebuilding that rack's leases. One
rack keeps the old binding, the other gets the new one → NAK war. Upstream's open question is why
only one rack takes the full-reload path.

**Code-level confirmation.** The defect is in the MAAS Go agent,
[`src/maasagent/internal/dhcp/service.go`](https://github.com/canonical/maas/blob/a05d630bfab2efde6c32d331ed51c4b1912f4862/src/maasagent/internal/dhcp/service.go),
in `configureViaOMAPI` — it is **add-only** and swallows the already-exists error, which is
literally the log line seen on infra2:

```go
err = clientV4.AddHost(host.IP, host.MAC)
if err != nil {
    if !errors.Is(err, omapi.ErrHostAlreadyExists) {
        return err
    }
    log.Warn(fmt.Sprintf("Ignoring already existing host: %s", host.MAC))  // new IP silently dropped
}
```
The OMAPI client (`src/maasagent/internal/dhcpd/omapi/client.go`) exposes only
`AddHost` / `GetHost` / `DeleteHost` — there is no update/upsert primitive, so a changed IP↔MAC
binding can never be applied to a rack that already has an entry for that MAC.

**Fix in flight:** [canonical/maas#556](https://github.com/canonical/maas/pull/556)
("fix(dhcp): restart DHCP on node release and allow OMAPI to update host lease", branch
`fix-lp-2164644`) — adds a `SyncHost` (delete-then-re-add) method, switches `configureViaOMAPI` to
use it, and passes `subnet_id` on the release path so a `None` IP triggers a full reload instead of
the crash-looping OMAPI partial update. **Open, not merged, no milestone as of 2026-08-28**, so no
MAAS release carries the fix yet and recurrences on this substrate should be expected.

**Verdict:** substrate/MAAS defect, not a canonical-k8s or test defect. Triage against
LP #2164644; do not re-investigate. Note which rack is stale — it varies per run (primary in some
occurrences, secondary in others), so don't assume it is always the secondary.

**Reference runs:** UUID `6f5d1e87-458b-4dc0-a7f3-95a65f961c8a` (run 33089595673, cluster_4,
node1/`pw3rsy`, MAC `52:54:56:58:03:01`, stale `.85` on infra2 vs new `.91` on infra1). Same
signature also in `8663c9cc-...`, `e03a1524-...`, `b9f1a7ca-...` (stale entry on *infra1* there),
`f38f0f58-...`.

Four further confirmed occurrences, all on 2026-08-27 branch `main`, all `tor3-sqa-virtual_maas`,
all step #100, all `TimeoutError: wait timed out after 2400s`:

| UUID | run_id | Cluster | Test | Node / MAC | New IP | Stale IP | Stale rack |
|---|---|---|---|---|---|---|---|
| `5dfef422-07b4-4029-889d-ade9cc63c6b8` | 33066050252 | cluster_7 | `test_remove_leader_control_plane` | node3 `c7rf8y` / `...05:01` | .92 | .86 | infra2 (secondary) |
| `e8ae140c-ee2f-4667-aa72-f91787e52973` | 33066045312 | cluster_6 | `test_remove_worker` | node4 `rckecg` / `...06:01` | .90 | .88 | infra2 (secondary) |
| `c710fc76-32ea-4bf0-bb78-f6c3b2bfb7a7` | 33066029910 | cluster_3 | `test_remove_non_leader_control_plane` | node4 `rckecg` / `...06:01` | .91 | .89 | infra2 (secondary) |
| `4cdceb79-d16b-49f6-8332-7db51dd32185` | 33066019503 | cluster_1 | `test_remove_leader_control_plane` | node5 `cdkx6k` / `...07:01` | .92 | .87 | **infra1 (primary)** |

Two further confirmed occurrences on 2026-08-28, branch `main`, `tor3-sqa-virtual_maas`, step #100,
both `TimeoutError: wait timed out after 2400s`:

| UUID | run_id | Cluster | Test | Node / MAC | New IP | Stale IP | Stale rack |
|---|---|---|---|---|---|---|---|
| `b0e87b46-921e-4017-a249-40c10ed7099a` | 33167245013 | cluster_3 | `test_remove_worker` | node5 `cdkx6k` / `...07:01` | .90 | .87 | **infra1 (primary)** |
| `afc9331e-1ebd-4930-bcdb-a703f7deb27a` | 33186521280 | cluster_2 | `test_remove_leader_control_plane` | node4 `rckecg` / `...06:01` | .92 | .86 | infra2 (secondary) |

`b0e87b46` is worth noting as a **more severe variant**: `10.241.144.90` issued *zero* HTTP requests
to `maas-http` — not even `ipxe.cfg`. The usual signature reaches `boot-kernel`/`boot-initrd` and
only then stalls on the missing `squashfs` (as `afc9331e` did). Absence of any HTTP fetch is still
this pattern, not a separate one.

All IPs `10.241.144.x`. The pattern is **not test-specific** — it fires on worker removal as well
as on leader and non-leader control-plane removal; anything that triggers a release + immediate
re-allocate can hit it.

---

_Add more patterns below as they are discovered._

## Notes

- The suite is long (~110 min for 12 tests). A `TimeoutError` from
  `helpers.py:wait_active` → `jubilant/_juju.py:wait` is the generic failure shape; **always read
  the `Status(...)` block printed with the exception** — it names the stuck machine and unit, which
  is what tells you whether the problem is the charm or the substrate.
- The teardown runs `juju-crashdump` and takes ~35 min after the failure; the many
  `command ['timeout', '45s', 'juju', 'ssh', ...] failed` lines near the end of the log are
  crashdump collection against the *already-broken* machine and are **noise**, not the root cause.
- On `virtual_maas`, the MAAS logs live on `infra1`/`infra2`/`infra3` = `10.241.144.2/.3/.4`.
  `infra1` is usually the primary rack, `infra2` the secondary; map the rack IDs in
  `WorkflowID=configure-dhcp:<id>` to primary/secondary rather than guessing.
- `libvirt/qemu/*.log` in the MAAS archive only covers the infra/juju/microk8s/vault VMs — there is
  **no console log for node1..node6**, so an initramfs hang cannot be observed directly; infer it
  from the missing `squashfs` fetch.
- The tests consume the whole node pool. Transient
  `failed to acquire node: No available machine matches constraints [... ('zone', ['default'])]`
  followed by a Juju AZ retry is normal and usually recovers — don't mistake it for the root cause.
- **Always check BOTH racks before describing the DHCP mechanism.** Looking at only one rack's
  `dhcpd` journal produces a plausible but wrong story — "the client kept requesting a stale cached
  address and the rack NAK'd it". The archive shows the truth is symmetric: each rack *offers and
  ACKs* its own address and NAKs the other's. Quick check:
  ```bash
  for d in $M/10.241.144.2 $M/10.241.144.3; do echo "== $(basename $d)"
    grep -ah "dhcpd\[" $d/var/log/syslog | grep -a "<MAC>" \
      | grep -aoP "DHCP(ACK|OFFER|NAK) on 10\.241\.144\.\d+" | sort | uniq -c; done
  ```
  A healthy rack shows offers/ACKs for the **new** IP only; the stale rack shows offers/ACKs for the
  **old** IP. The server-identifier in parentheses in `DHCPREQUEST for <ip> (<server>)` names the
  rack that handed out that address — follow it rather than assuming a client-side cache.
