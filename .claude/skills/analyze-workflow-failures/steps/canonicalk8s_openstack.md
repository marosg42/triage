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

### Pattern 3: terraform-provider-juju `juju_application` create never returns → 4h `timeout` kill (exit 124)

**Symptom:**
```
module.k8s.juju_application.k8s: Still creating... [239m52s elapsed]
module.k8s_worker["k8s-worker"].juju_application.k8s_worker: Still creating... [239m52s elapsed]
Interrupt received. / Stopping operation...
│ Error: Plugin did not respond      (or: │ Error: operation canceled)
##[error]Process completed with exit code 124
##[end-action id=…deploy_solution.apply;outcome=failure;duration_ms=14400263]
```
`duration_ms=14400263` (exactly 240m) + exit **124** ⇒ the `timeout 4h` wrapper around
`terraform apply` fired; it is **not** a provider or Juju timeout.

**Root cause:** the provider's post-deploy read (`ReadApplicationWithRetryOnNotFound`) is a
`retry.Call{Attempts: 30, Delay: 1s, BackoffFunc: DoubleDelay}` with **no MaxDelay/MaxDuration**
and no user-visible logging. If its readiness predicate is never satisfied, Create silently
retries with doubling delays (worst case ~2^30 s). The Juju deploy itself succeeds: the model,
all machines, charms and unit agents come up within minutes. Because `juju_application` Create
never returns, **no downstream resource is ever attempted** — the
`k8s:cluster ↔ k8s-worker:cluster` integration and the whole `module.openstack`
(openstack-integrator, cinder-csi, openstack-cloud-controller) are never created, so
`k8s-worker` units sit in `blocked: Missing cluster integration` for the whole window. That
blocked state is a **consequence**, not the cause.

**Evidence to look for:**
- `run.log`: only `Still creating...` for `juju_application.k8s` / `.k8s_worker` up to ~`239m5Xs`,
  then interrupt + exit 124. No `Creation complete` for either app.
- **Controller audit log is the decisive artifact.** Extract from the
  `juju_openstack_controller` crashdump and filter to the provider's conversations:
  ```bash
  tar xzf files/<uuid>/generated/juju_openstack_controller/juju-crashdump-controller-*.tar.gz \
      -C /tmp/cd --wildcards '*/var/log/juju/audit.log' '*/var/log/juju/logsink.log' \
                 '*/var/log/juju/models/*'
  python3 -c "
  import json,glob
  conv={};rows=[]
  for f in glob.glob('/tmp/cd/*/*/baremetal/var/log/juju/audit.log'):
    for line in open(f):
      try: d=json.loads(line)
      except: continue
      if 'conversation' in d: conv[d['conversation']['conversation-id']]=d['conversation'].get('what','')
      if 'request' in d:
        r=d['request']
        if 'terraform' in conv.get(r['conversation-id'],''): rows.append((r['when'],r['facade'],r['method']))
  for r in sorted(rows): print(*r)"
  ```
  Expect: `ModelManager.CreateModel`, 2× `Application.DeployFromRepository`, then repeating
  triples of `Application.ApplicationsInfo` + `Application.Get` + `Client.FullStatus`,
  **14 attempts per app**, with gaps doubling 1s → 2s → … → 34m → **68m**, and
  `errors: [null]` on every request (no API-level failure ⇒ provider-side predicate failure).
- The last ~1h43m before the kill has **no provider traffic at all**. This is the backoff
  sleeping (attempt 15 is due 136 min after attempt 14), *not* a stalled RPC — do not
  misreport it as a hang.
- Model log `generated/…/var/log/juju/models/admin-canonicalk8s-openstack-*.log` stops a few
  minutes after provisioning finishes and stays silent for the rest of the run.
- `logsink.log` (controller machine-0) is the only source of unit-level logs — the
  canonicalk8s model is **not** crashdumped. Filter by model uuid prefix:
  `grep -ah "^<modeluuid>" */baremetal/var/log/juju/logsink.log | grep -a "Status context closed with: \[[A-Z]"`

**Distinguishing factor to check:** `k8s` and `k8s-worker` are the only applications pinned to
base `ubuntu@26.04` / channel `1.36/candidate` in `config/manifest_canonicalk8s_openstack.yaml`;
every other planned app is `ubuntu@22.04` and is never attempted. Unconfirmed but worth an A/B
against `ubuntu@24.04`.

**Fix directions:** capture `TF_LOG_PROVIDER=DEBUG` on the apply (the bundle currently contains
nothing about *why* the provider retries); add `timeouts { create = … }` to `juju_application`;
upgrade past terraform-provider-juju 0.23.3.

_Observed in: runs 33527877332 / `c4601c94-764d-4aee-85f4-2a854c4c09e7`,
33686835617 / `17ba6020-5c2a-4c51-80e4-0c93548b95ee`,
33713186585 / `41d68b33-c37b-4063-ba39-85f24c3927cc` (all substrate `ext-sqa-ps6_openstack`)._

---

### Pattern 4: `terraform init` fails in <10s — `Unsupported argument: model` from unpinned `?ref=main` charm modules

**Symptom:**
```
Downloading git::https://github.com/charmed-kubernetes/charm-aws-integrator?ref=main for aws.aws_integrator...
...
│ Error: Unsupported argument
│   on aws/applications.tf line 40, in module "aws_integrator":
│   40:   model       = var.model
│ An argument named "model" is not expected here.
(×4: aws_integrator, aws_k8s_storage, aws_cloud_provider, ceph_csi)
##[error]Process completed with exit code 1.
##[end-action id=…deploy_solution.init;outcome=failure;duration_ms=9762]
##[end-action id=…deploy_solution.tf-validate;outcome=skipped]
```

**Root cause:** `k8s-bundles@release-1.35` pins most charm terraform modules
(`k8s-operator?ref=release-1.35`, `openstack_*?ref=release_1.34`,
`ceph-charms?ref=stable/squid-jammy`) but pulls **four modules at the floating `?ref=main`**:
`charm-aws-integrator`, `aws-k8s-storage`, `charm-aws-cloud-provider`, `ceph-csi-operator`.
Those `main` branches made a breaking change to their variable interface (dropped/renamed
`model`), while the release-1.35 caller still passes `model = var.model`.

Terraform type-checks **every module block in the root configuration tree at init**, so the
`aws/` and `ceph/` configs break the run even though `cloud_integration = "openstack"` means
they are never instantiated. Nothing is deployed — no model, no machines.

**Evidence to look for:**
- The set of erroring modules maps **1:1 onto the `?ref=main` downloads** in the init log; no
  pinned module errors. Grep the init section for `?ref=` to build the pinned/unpinned map.
- `duration_ms` ≈ 10 000 on `deploy_solution.init` and `tf-validate;outcome=skipped` — the
  failure is at *init*, not plan/apply.
- `generated/version_collector_canonicalk8s_openstack.log` contains only controller-state
  collection; there is no deploy activity at all for the step.
- Beware: happy-bot may mis-attribute this step to earlier tempest failures. Check
  `LAYERS_TO_BUILD` for `IGNORE_TEMPEST_FAILURE: true` and the step conclusions in
  `jobs.json` — validation failures in `sunbeam_test_with_validation_plugin` can be
  deliberately ignored and are unrelated.

**Fix directions:** pin the four `?ref=main` module sources to a release branch/tag in
k8s-bundles; add a `terraform init -backend=false && terraform validate` pre-flight so
interface drift is caught in seconds instead of after a 7-hour Sunbeam deploy.

_Observed in: run 36023061228 / UUID `d5ababe6-6e80-4866-8cc2-dc1860234634`
(substrate `tor3-sqa-testflinger`, branch `release-1.35`); run 36023039991 / UUID
`aa78fbde-a12a-4788-b2f0-6b44e1b122d4` (substrate `tor3-sqa-testflinger`, workflow
branch `main`, k8s-bundles branch `release-1.35`); run 36029770355 / UUID
`4c47da04-e4d7-407f-8072-dae811dac93d` (substrate `tor3-sqa-shared_maas`,
workflow branch `main`, k8s-bundles branch `release-1.35`)._

---

_Add more patterns below as they are discovered._

## Notes

- **Substrate note:** `ext-sqa-ps6_openstack` (Canonical PS6 tenant) is also common for this
  step, alongside `shared_maas`/`testflinger`. In that case there is no `generated/sunbeam/`
  directory at all — the OpenStack layer artifacts live under `generated/openstack/` and the
  Juju controller artifacts (crashdump, dump-db, image streams) under
  `generated/juju_openstack_controller/`.
- **Three distinct failure timings:** Pattern 1 fails **fast** at model creation
  (`terraform apply` ~30s in); Pattern 2 fails **slow** after a full 60-minute `juju wait-for`
  timeout; Pattern 3 fails after exactly **240 minutes** with exit code 124 and both
  `juju_application` resources still `Still creating...`; Pattern 4 fails in **<10 seconds** at
  `terraform init` with exit code 1 and `tf-validate` skipped. Check the elapsed time and the
  exit code first — they identify the pattern immediately.
- **VIP map (typical):** `traefik-public` → keystone **public** VIP (used by the credential
  `auth_url`); `traefik` → keystone **internal** VIP; `traefik-rgw` → RGW. Get the live mapping
  from `juju_status_openstack.txt` (`Serving at https://…`).
- The failing `juju-sunbeam-controller` is a nova VM on the OpenStack tenant network
  (`10.243.x.x`); its logs are usually **not** in the bundle. Diagnose its keystone reachability
  from the server-side (keystone/traefik/metallb pod logs), not the controller.
- `pods_openstack_logs.tgz` is tarred with `--remove-files`, so the individual
  `logs-openstack-*.txt` may be **empty on disk** until you extract them from the tgz.
