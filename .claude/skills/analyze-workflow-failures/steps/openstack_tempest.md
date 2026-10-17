# Step Knowledge: openstack_tempest

## Step Overview

`openstack_tempest` runs `fce validate` for the `openstack` layer with the tempest validator.
FCE (`layers/openstacklayer.py` → `initialize()` → `discover_tempest_config()`) first runs
`discover-tempest-config` (python-tempestconf, from the `tempest` snap) **on the GitHub runner**,
which walks the Keystone catalog and probes every service's **public** endpoint
(`https://<svc>.<cluster>.tor3-sqa-shared-maas.solutionsqa:<port>/`) to discover API versions.
Only after that does it run the tempest test list. Any unreachable public endpoint aborts the
step before a single test runs.

Runner traffic to the cloud is SNAT'd — on shared_maas it shows up in unit haproxy/apache
logs as source **`91.189.92.152`**. Use that to tell runner requests apart from in-cloud ones.
The cloud never sees the runner's private address (`10.152.185.x`), so always filter on the SNAT IP.

## Swift Artifacts

| Path | Description | When to check |
|---|---|---|
| `generated/openstack_tempest/log.txt` | FCE validate log incl. full discover-tempest-config DEBUG output | Always — first stop |
| `generated/openstack/tempest/etc/tempest.conf` | Generated tempest config (tiny if discovery aborted) | Check whether discovery completed |
| `generated/openstack/tempest.out` | Tempest run output (absent/stale if discovery failed) | Test failures |
| `generated/openstack/juju-crashdump-openstack-<ts>.tar.gz` | Crashdump taken AFTER the failure (during "Collect logs") | Per-unit haproxy/apache/service logs |
| `config/overlay-vips.yaml`, `config/overlay-hostnames.yaml` | VIPs and public/internal hostnames per service | Map endpoint hostname → VIP |

## Key Log Files (inside the openstack crashdump)

| File | What it contains | When to use |
|---|---|---|
| `<m>/lxd/<n>/var/log/haproxy.log` | TCP-mode haproxy session log per frontend (`tcp-in_<svc>...`) | Did the runner's connection reach the service at all? |
| `<m>/lxd/<n>/var/log/apache2/other_vhosts_access.log` | Apache TLS-terminating vhost access log (behind haproxy) | Confirm requests reached apache |
| `<m>/lxd/<n>/var/log/syslog` | pacemaker/IPaddr2 VIP moves | Which unit held the VIP |
| `<m>/lxd/<n>/var/log/juju/machine-lock.log` | Hooks executed per unit | Was a hook restarting services at failure time? |

## Grep Patterns

```bash
# Primary error and the endpoint that failed
grep -n "ERROR config_tempest\|CRITICAL tempest\|MaxRetryError\|Retrying" generated/openstack_tempest/log.txt | cut -c1-300

# Which endpoints were successfully contacted before the failure
grep -a "Request (main)" generated/openstack_tempest/log.txt | grep -oE "[0-9]{3} (GET|POST) https://[a-z-]+\.[^:]+:[0-9]+" | sort | uniq -c

# Runner traffic (SNAT source 91.189.92.152) reaching ANY haproxy at failure time
grep -a "14:0[78]:" <crashdump>/*/lxd/*/var/log/haproxy.log | grep 91.189.92.152

# VIP holder for a service
grep -a "IPaddr2(res_<svc>_.*_vip)" <crashdump>/*/lxd/*/var/log/syslog | grep "Adding inet"
```

## Known Failure Patterns

### Pattern 1: discover-tempest-config aborts — `Connection reset by peer` on manila public endpoint (port 8786) from the runner

**Symptom:**
```
WARNING urllib3.connectionpool Retrying (Retry(total=2...)) after connection broken by
  'ProtocolError('Connection aborted.', ConnectionResetError(104, 'Connection reset by peer'))': /
ERROR config_tempest.constants Request on service 'share' with url
  'https://manila.<cluster>.tor3-sqa-shared-maas.solutionsqa:8786/' failed: urllib3.exceptions.MaxRetryError
  File ".../config_tempest/services/share.py", line 29, in set_versions
  ... ConnectionResetError: [Errno 104] Connection reset by peer   (inside ssl do_handshake)
subprocess.CalledProcessError: Command '['discover-tempest-config', ...]' returned non-zero exit status 1.
```
The retry sequence may mix `ConnectionResetError(104)` with
`SSLEOFError(... UNEXPECTED_EOF_WHILE_READING)`.

**Root cause (as far as the archive can show):** The runner's TLS connections to manila's
public port 8786 are reset during the handshake **before reaching the manila haproxy**.
`share` is the first non-core service probed, and an unhandled `MaxRetryError` in
python-tempestconf's `share.set_versions()` kills discovery. Manila itself is healthy:
all units `active/idle`, VIP stable for hours, no hooks running, and in-cloud clients
(other units, `10.242.39.x`) get correct `300` responses via haproxy on 8786. Neither the
haproxy nor the apache logs of any manila unit show a single connection from the runner SNAT
IP (`91.189.92.152`) on 8786, while the same runner reaches keystone/nova/glance/neutron
haproxies (and even manila's apache directly on 8776) seconds before or after. Port 8786
appears to be filtered/reset on the runner → cloud network path (firewall/NAT allow-list),
not by the deployment.

**Most likely mechanism — runner egress proxy:** the runner's transparent egress proxy
`aproxy_nosni` (snap `aproxy` plus an nftables DNAT of all outbound TCP except 3128) resets
non-allowlisted ports such as 8786. Live evidence from one run showed spoofed SYN-ACK/RST on the
runner loopback and zero packets at the manila container. The archive alone cannot always
separate this from a gateway filter. Quick check:
`grep -a "aproxy_nosni.*installed" generated/github-runner/run.log`.
Fix: allowlist 8786 in aproxy, or exclude the lab ranges from the DNAT.

**Evidence to look for:**
- `openstack_tempest/log.txt`: 3 retries ~120 ms apart, all `ConnectionResetError(104)` in `ssl.do_handshake`.
- Crashdump `haproxy.log` on the VIP-holding manila unit: no entry at the failure second; entries from `91.189.92.152` present on keystone/nova/glance haproxies in the same minute.
- `apache2/other_vhosts_access.log` on manila units: `91.189.92.152` only when it connects to 8776 directly (post-failure probes), never via 8786.
- `generated/openstack/log.txt`: no earlier runner request to `:8786` — the manila public endpoint is first contacted from the runner by tempest.

**Red herrings (verified benign in this pattern):**
- `Not adding haproxy listen stanza for manila-api_int/_public port is already in use` in `unit-manila-N.log` — charm-openstack merges same-port endpoints into one `tcp-in_manila-api_admin` frontend with per-network backends; logged every update-status.
- `manila-api.log` "osapi_share listening on :::8786" — the eventlet manila-api is stopped (SIGTERM) seconds later at install when the charm switches to apache/WSGI.
- `\x16\x03\x01\x02" 400` on the `<host>.maas:80` vhost — TLS sent to an oam (10.241.x) address where apache has no TLS vhost; these come from live/AI probes, not tempest. If tempest hit this path, it would fail with `wrong version number`, not reset/EOF.

**From run:** 38543743514 (UUID b05e52d9, tor3-sqa-shared_maas dh1_j8_2, branch main, 2026-10-14; VIP 10.242.37.37 on manila/1 7/lxd/6).

**From run:** 38541565933 (UUID ea3d353c, tor3-sqa-shared_maas **dh1_j8_1**, branch main, 2026-10-14; runner
`amd64-jammy-small-solqa-ps7-*` with `aproxy_nosni`; VIP 10.242.36.37 on manila/0 6/lxd/6, stable since 11:25).
Same signature on a different cluster → points at a runner-side/egress path filter on 8786 rather than one lab.
- Manila haproxy on 6/lxd/6 logs every TCP session (11:56 gophercloud, 14:13+ probes) but has **zero** lines at 14:08:47.
- Runner (`91.189.92.152`) reached in the same run: `tcp-in_public-port` (keystone 5000) ×21, nova 8774 ×7,
  neutron 9696 ×8, glance 9292 ×4, radosgw 443 ×4, cinder 8776 ×2, placement 8778, octavia 9876, aodh 8042,
  barbican 9312 — placement succeeded at 14:08:47.04, <0.5 s before the first manila reset.
- Post-failure probes from `91.189.92.152` reached manila apache vhosts logged as `:443`/`:80` on all 3 units (not via haproxy/8786).

**From run:** 38596345988 (UUID 993608ae, tor3-sqa-shared_maas dh1_j8_2; VIP 10.242.37.37).

**From run:** 38596334380 (UUID df14694d, tor3-sqa-shared_maas dh1_j8_1; VIP 10.242.36.37 on manila/0 6/lxd/6).

**From run:** 38647939027 (UUID 8ce33ade, tor3-sqa-shared_maas dh1_j8_1).

**Quick check (one-liner):**
```bash
grep -ah "91.189.92.152:" <crashdump>/*/lxd/*/var/log/haproxy.log | awk '{print $8}' | sort | uniq -c   # manila frontend absent ⇒ this pattern
```

---

## Notes

- The crashdump for this step is taken during "Collect logs" (≈20 min after the failure); juju status in it is post-failure but services rarely change in between.
- If the pipeline ran an in-run "AI failure analysis" step, its live probes (curl/ssh) appear in the runner log and in unit apache/haproxy logs from ~14:10 onwards; do not mistake them for tempest traffic.
- discover-tempest-config has no per-service skip on network errors; a single unreachable optional service (share, object-store, etc.) aborts the whole step.
- Manila LXD units often have no `/var/log/haproxy.log` (rsyslog `Permission denied`). Use
  `journalctl -D <crashdump>/*/lxd/*/var/log/journal/* -t haproxy`, or apache `manila_access.log` /
  `other_vhosts_access.log`. No entries at the failure second means the traffic never reached manila.
