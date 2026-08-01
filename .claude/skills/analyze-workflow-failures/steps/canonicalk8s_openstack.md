# Step Knowledge: canonicalk8s_openstack

## Step Overview

Deploys **Canonical K8s + OpenStack integration** on top of a Sunbeam-provided OpenStack
(`openstack_cloud/RegionOne`) using the **Terraform Juju provider** against the
`juju-sunbeam-controller` (a Juju controller Sunbeam bootstrapped **on** the deployed OpenStack
earlier in the run, in the `juju_sunbeam_controller` step).

Flow: `terraform apply` → create Juju model `canonicalk8s-openstack` → deploy `k8s`, `k8s-worker`,
`openstack-integrator`, `cinder-csi`, `openstack-cloud-controller` → wait with
`juju wait-for model canonicalk8s-openstack --query='forEach(units, unit => unit.workload-status=="active")' --timeout 60m`.

Because it runs on a Sunbeam OpenStack, its health depends on that OpenStack's Keystone/glance/
MetalLB VIPs, images-sync (simplestreams), and Juju controller — so failures here are often
**downstream of the Sunbeam layer**, not bugs in the k8s bundle itself.

## Swift Artifacts

| Path | Description | When to check |
|---|---|---|
| `generated/github-runner/run.log` | Full runner log incl. the `terraform apply` output and the exact `│ Error:` block | Always — first stop |
| `generated/github-runner/jobs.json` | Step names/conclusions; confirms step 97 `canonicalk8s_openstack` failed | Triage |
| `generated/sunbeam/juju_status_openstack.txt` | Sunbeam OpenStack app health + VIP→service mapping (`traefik-public`→public keystone VIP, `traefik`→internal) | Always |
| `generated/sunbeam/admin.openrc` | `OS_AUTH_URL` (which keystone VIP the credential uses), region | Auth/endpoint issues |
| `generated/sunbeam/pods_openstack_logs.tgz` | Per-pod logs for the openstack namespace (keystone, glance, nova, neutron, traefik…) — tarred with `--remove-files` | Deep dive |
| `generated/sunbeam/pods_metallb-system_logs.tgz` | MetalLB controller + speaker logs | VIP/`i/o timeout` issues |
| `generated/sunbeam/logs-openstack-images-sync-0.txt` (in openstack tgz) | images-sync (simplestreams) sync + http-mirror access log | Image/base "no metadata" issues |
| `generated/sunbeam/kubectl_get_pod.txt` / `kubectl_get_node.txt` | Pod restart counts/ages, node readiness | Confirm hang vs restart |
| `generated/sshuttle_metallb_range.txt` | Per-minute sshuttle tunnel health (`sshuttle UP`) | Rule out tunnel |

> **Substrate note:** commonly runs on `tor3-sqa-shared_maas` (no MAAS logs) or
> `tor3-sqa-testflinger` (no MAAS logs). Diagnosis relies on `run.log` + the `generated/sunbeam/`
> artifacts, **not** MAAS logs.

## Key Log Files (inside pods_*_logs.tgz)

| File | What it contains | When to use |
|---|---|---|
| `logs-openstack-keystone-{0,1,2}.txt` | wsgi-keystone access log + warnings | Prove keystone health / find (or miss) the failing token request |
| `logs-openstack-traefik-public-*.txt` | public ingress (in front of keystone public VIP `.132`) | Did the request reach the ingress? |
| `logs-metallb-system-metallb-speaker-*.txt` | MetalLB L2 speaker reconcile/announce log | VIP `i/o timeout` — check for the wedge signature |
| `logs-openstack-images-sync-0.txt` | simplestreams sync + `http-mirror` access log | image base "no metadata" |

## Grep Patterns

```bash
R=files/<uuid>/generated/github-runner/run.log

# 1) The exact failure block (terraform/juju):
grep -nE 'Error:|Unable to create model|open environ|dial tcp|reach goal state|timed out waiting|no metadata' "$R" | sed -E 's/\x1b\[[0-9;]*m//g'

# 2) VIP → service mapping and app health:
grep -iE 'Serving at|keystone |traefik' files/<uuid>/generated/sunbeam/juju_status_openstack.txt

# 3) MetalLB wedge check (last timestamp per pod; expect early stop if wedged):
for f in /tmp/mlb/*/logs-metallb-system-metallb-*; do echo -n "$f "; grep -oE '2026-[0-9T:]+' "$f" | tail -1; done

# 4) Compare openstack pods are current (rules out cluster-wide artifact):
for f in logs-openstack-keystone-0 logs-openstack-nova-0 logs-openstack-neutron-0; do echo -n "$f "; grep -oE '2026-[0-9T:]+' "$f".txt | tail -1; done

# 5) Images-sync: what Juju fetched from the product-streams endpoint:
grep -E '\[http-mirror\].*streams/v1' logs-openstack-images-sync-0.txt
```

## Known Failure Patterns

### Pattern 1: MetalLB speaker wedge → keystone VIP `i/o timeout`

**Symptom:**
```
│ Unable to create model "canonicalk8s-openstack", got error: failed to
│ open environ: authentication failed
│ caused by: Post ".../v3/auth/tokens": dial tcp <keystone-public-VIP>:443: i/o timeout
```

**Root cause:** MetalLB (layer2) speaker hangs — process stays `Running` (0 restarts) but its
reconcile loop dies and it stops answering ARP for its VIPs. After ARP-cache expiry the keystone
**public** VIP is unreachable from the off-cluster `juju-sunbeam-controller` VM, so model
creation's token request times out. Keystone itself is healthy (internal ClusterIP path).

**Evidence to look for:**
- `logs-metallb-system-metallb-speaker-*`: all stop at the same early time, mid-reconcile, no error/shutdown.
- `kubectl_get_pod.txt`: metallb pods `Running`, `0` restarts, multi-hour age; metallb log files small (not rotated).
- openstack-namespace pods (keystone/nova/neutron) collected with same method are **current** → anomaly isolated to MetalLB.
- keystone pod logs healthy 200s through window; failing request absent from keystone + traefik-public logs.
- `sshuttle_metallb_range.txt`: tunnel UP (rule out).

**See also:** `patterns/metallb-speaker-wedge.md` for full detail.

_Observed in: run 30245701655 / UUID `5e4b0726-0eed-4511-b55d-a4ba57b32679`._

---

### Pattern 2: `openstack-integrator` base `ubuntu@22.04` — "no metadata for images"

**Symptom:**
```
ERROR timed out waiting for "canonicalk8s-openstack" to reach goal state
# juju status:
Machine 6  down/pending  ubuntu@22.04  no metadata for "ubuntu@22.04" images in RegionOne with arch amd64
openstack-integrator/0  waiting/allocating  (waiting for machine)
```
The `juju wait-for ... --timeout 60m` then times out (exit 1) because `openstack-integrator`
never reaches `active`, blocking `cinder-csi` / `openstack-cloud-controller` (scale 0).

**Root cause:** The canonicalk8s terraform pins `openstack-integrator` to base `ubuntu@22.04`
while k8s/k8s-worker use `ubuntu@24.04`. A jammy 22.04 image **is present and active in glance**
(and images-sync reports syncing it), but Juju's lookup against the `images-sync` product-streams
endpoint did **not** resolve a usable 22.04 image record for RegionOne, so the integrator machine
can't be provisioned. This is an **image-metadata (simplestreams/product-streams) resolution
gap**, not a missing glance image, and not something a manual `juju metadata add-image` should be
required to fix.

**Evidence to look for:**
- `run.log` `openstack image list`: an active `auto-sync/ubuntu-jammy-22.04-...` image exists (so glance is fine).
- terraform apply: `openstack-integrator` created with `base = "ubuntu@22.04"`; other machines `ubuntu@24.04`.
- `logs-openstack-images-sync-0.txt` `http-mirror` access log: at deploy time Juju fetches
  `streams/v1/index.json` (200) then makes **no products-catalog request** → its 22.04 lookup
  came up empty. (The index/products JSON body is usually **not** captured — note this limitation.)
- Secondary: `k8s/*` units `waiting` on Unready Pods (rawfile-csi/coredns) — downstream of the
  missing cloud-controller/CSI, not the blocker.

**Fix directions:** inspect the images-sync `streams/v1/index.json`+products on a rerun; fix any
images-sync keystone TLS sync failure (`CERTIFICATE_VERIFY_FAILED`); or pin `openstack-integrator`
to `ubuntu@24.04` as a workaround.

_Observed in: run 30246045059 / UUID `fa380aa5-8be2-4310-99f2-52eff5d8a639` (substrate testflinger)._

---

_Add more patterns below as they are discovered._

## Notes

- **Two distinct failure timings:** Pattern 1 fails **fast** at model creation (`terraform apply`
  ~30s in); Pattern 2 fails **slow** after a full 60-minute `juju wait-for` timeout. Check the
  timestamp gap between step start and the error to tell them apart quickly.
- **VIP map (typical):** `traefik-public` → keystone **public** VIP (used by the credential
  `auth_url`); `traefik` → keystone **internal** VIP; `traefik-rgw` → RGW. Get the live mapping
  from `juju_status_openstack.txt` (`Serving at https://…`).
- The failing `juju-sunbeam-controller` is a nova VM on the OpenStack tenant network
  (`10.243.x.x`); its logs are usually **not** in the bundle. Diagnose its keystone reachability
  from the server-side (keystone/traefik/metallb pod logs), not the controller.
- `pods_openstack_logs.tgz` is tarred with `--remove-files`, so the individual
  `logs-openstack-*.txt` may be **empty on disk** until you extract them from the tgz.
