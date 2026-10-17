# Step Knowledge: sunbeam_bootstrap

## Step Overview

Bootstraps the first Sunbeam node (single-node or first node of a multi-node cluster). Runs
`products/sunbeam/deploy_sunbeam.py bootstrap` (`COMMAND: bootstrap`), which over SSH:
1. `sudo snap install openstack --channel <snap_channel>` + `sunbeam prepare-node-script --bootstrap | bash -x`
   (installs juju, lxd, bootstraps Juju onto LXD)
2. `sudo snap set openstack feature.*` / `ovn.provider=...`
3. `sunbeam cluster bootstrap -m manifest.yaml --role ...`

The `sunbeam` CLI drives a series of internal `terraform apply` plans (sunbeam-machine,
k8s, microceph, openstack, ...). When one fails, the CLI prints only
`Error: terraform command failed: ... terraform apply ...` with an **empty `stderr:`** —
the real error is only in the node's sosreport.

## Swift Artifacts

| Path | Description | When to check |
|---|---|---|
| `generated/sunbeam/output.log` | deploy_sunbeam.py output (same as GH log) | First stop — but terraform detail is missing |
| `generated/sunbeam/manifest.yaml` | Manifest passed with `-m` (pipeline-generated) | Check which charm channels are overridden |
| `generated/sunbeam/sosreport-<node>-*.tar.xz` | Node sosreport — **contains the real terraform error** | Always for `terraform command failed` |
| `config/sunbeam.yaml` | Pipeline config: `snap_channel`, manifest overrides | Channel/override questions |

## Key Log Files (inside the sosreport)

Extract with `tar -xJf ... -C files/<UUID>-sos` (mknod errors for `/dev/*` are harmless).

| File | What it contains | When to use |
|---|---|---|
| `home/ubuntu/snap/openstack/common/etc/<model>/<plan>/terraform-apply-<ts>.log` | Terraform TF_LOG output, provider diagnostics | Primary — find `[ERROR]` / `diagnostic_detail` |
| `home/ubuntu/snap/openstack/common/etc/<model>/<plan>/terraform.tfvars.json` | Effective tfvars for the failed plan | Verify channels/config actually used |
| `home/ubuntu/snap/openstack/common/logs/sunbeam-<ts>.log` | sunbeam CLI debug log (full effective manifest incl. snap defaults) | Which defaults were applied |
| `var/snap/openstack/common/hooks.log` | Snap hook output | Snap install/config issues |

## Grep Patterns

```bash
SOS=files/<UUID>-sos/sosreport-*/home/ubuntu/snap/openstack/common
grep -n "\[ERROR\]\|diagnostic_detail" -A12 $SOS/etc/*/*/terraform-apply-*.log
cat $SOS/etc/*/*/terraform.tfvars.json
grep -o "'<charm>': CharmManifest([^)]*)" $SOS/logs/sunbeam-*.log | sort -u
grep -n "apply_errored" $SOS/logs/sunbeam-*.log
```

## Known Failure Patterns

### Pattern 1: Charm channel not published (snap default channel for a charm doesn't exist)

**Symptom (GitHub Actions / output.log), ~1 min into `sunbeam cluster bootstrap`:**
```
Error: terraform command failed: /snap/openstack/<rev>/bin/terraform apply -input=false -auto-approve -no-color -json
stderr:
subprocess.CalledProcessError: Command '[... 'sunbeam', 'cluster', 'bootstrap', ...]' returned non-zero exit status 1.
```

**In sosreport `etc/<model>/deploy-sunbeam-machine/terraform-apply-*.log`:**
```
Unable to create application, got error: selecting releases: charm or bundle not found for
channel "2026.1/stable", base "amd64/ubuntu/24.04/stable"
available releases are: channel "2026.1/beta" ... channel "2026.1/edge" ...
vertex "juju_application.epa_orchestrator" error: Client Error
```

**Root cause:** The `openstack` snap installed from a pre-stable channel (e.g. `2026.1/beta`)
says "Snap has been automatically configured to deploy from beta channel", but this rewrite
did not cover every charm: `epa-orchestrator` kept its built-in default `2026.1/stable`
(tfvars `charm_epa_orchestrator_channel: 2026.1/stable`, `snap-channel: 2026.1/stable`).
That channel had no release on Charmhub yet, so the very first plan (`deploy-sunbeam-machine`)
failed. The pipeline manifest does not override `epa-orchestrator`, so it did not mask it.
Product/packaging issue (snap defaults vs. Charmhub publication), not infra.

**Evidence to look for:**
- tfvars: `"charm_epa_orchestrator_channel": "2026.1/stable"` while `charm_channel` is `.../beta`
- sunbeam log: `'epa-orchestrator': CharmManifest(channel='2026.1/stable', ...)`
- `generated/sunbeam/manifest.yaml` has no `epa-orchestrator` entry

**Also seen on:** `tor3-sqa-testflinger` (openstack snap rev 1140, `2026.1-5e8c5f1b`) — substrate-independent.
`prepare_env` builds the manifest from `/snap/openstack/current/etc/manifests/2026.1/beta.yml`
(see `output.log` "Reading software manifest from ..."), which has no `epa-orchestrator` entry.
Post-failure log collection then fails with `juju models ... ERROR api connection open timed out`,
so no juju status/debug-log artifacts exist — the sosreport is the only source.

**Workaround:** add `epa-orchestrator: {channel: <track>/beta, config: {snap-channel: <track>/beta}}`
under `manifest.core.software.charms` in `config/sunbeam.yaml`.

---

### Pattern 2: Control-plane wait timeout from slow Charmhub OCI image pulls

**Symptom (~1h25m into `sunbeam cluster bootstrap`):**
```
Timed out waiting to deploy OpenStack control plane: TimeoutError("wait timed out after 3599.99s ...
'cinder'/'glance': current='waiting', message='(workload) Payload container not ready'
```

**Root cause:** Workload images from `registry.jujucharms.com` download extremely slowly (no
registry mirror/proxy on testflinger), so some pods only start after the fixed 3600 s wait.
Seen: cinder-api 68 min, glance-api 73 min; apps went `active` minutes after the abort. Infra.

**Evidence (cajal sosreport):**
- `sos_commands/kubernetes/journalctl*`: `PullImage "...-image"` → `... returns image reference` gap;
  `pod_startup_latency_tracker ... podStartE2EDuration="1h9m..."`; `dial tcp 185.125.188.x:443: i/o timeout`.
  Transient `insufficient_scope: authorization failed` retries are noise.
- `sos_commands/sar/sar16` (`grep -a eno5`): rx kB/s in the hundreds, `%ifutil` < 5 % → upstream bottleneck.
- `var/log/pods/openstack_<app>-0_*/` dir mtime ≈ image arrival; `juju_status` in sos shows them `active`.
- Check which apps the wait monitors: `grep "monitored for readiness" logs/sunbeam-*.log`.

**Also seen:** with `feature.microovn-sdn=true`, `neutron` blocked `healthcheck failed: online`
(`SSL_connect ... certificate verify failed` to `ssl:<node>:6641`; MicroOVN own CA, deploy-microovn
tfvars `ca-offer-url: null`). neutron is NOT in the bootstrap wait's monitored list — secondary,
not the cause of this timeout.

**Variant (no network errors, steady slow throughput; also seen on the `jasperoid` bootstrap node):** the k8s journal had no
`i/o timeout`/`ErrImagePull`, only slow pulls (keystone 43 min, horizon 61, neutron 63, nova 75,
cinder/glance >75). sar showed `ens14f0np0` at about 0.9 MB/s with `%ifutil` <1 % and the CPU idle.
At the deadline the apps reported `Payload container not ready`, and earlier keystone sat at
`Payload container not ready` for ~40 min. Quick pull-duration table:
```bash
grep -a 'PullImage' $J | grep -ao '^Oct [0-9]* [0-9:]*\|PullImage \\*"[^"\\]*\|returns image reference' | paste -sd' ' | sed 's/Oct /\nOct /g'
```
In that run, the late-starting neutron showed a different MicroOVN TLS signature:
`Unable to open stream to ssl:<node>:6641 to retrieve schema: Operation not permitted`.

---

## Notes

- The `node1.dh1-j6.tor3-sqa-dedicated-maas.solutionsqa` hostname is normal for
  `virtual_maas` (shared DNS domain) — trust `jobs.json` for the substrate.
- Early `503 Service Unavailable` / `account.yaml FileNotFoundError` tracebacks at the top of
  `sunbeam-*.log` are startup noise while clusterd comes up.
- `version_collector_sunbeam_bootstrap.log` `juju: command not found` on infra nodes is post-failure noise.
