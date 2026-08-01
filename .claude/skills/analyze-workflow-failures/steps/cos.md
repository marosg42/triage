# Step Knowledge: cos

## Step Overview

The `cos` layer deploys the **COS Lite** observability stack (`ch:cos-lite` bundle) onto the
MicroK8s cluster provisioned by earlier layers. It runs `juju deploy -m foundations-microk8s:cos
--channel=stable ... --trust ch:cos-lite` with several overlays (the generated `bundle.yaml`
and `offer_overlay.yaml`, plus `config/storage-small-overlay.yaml` and `config/tls-overlay.yaml`).

The bundle deploys: prometheus-k8s, grafana-k8s, alertmanager-k8s, loki-k8s, catalogue-k8s,
traefik-k8s, and self-signed-certificates (as `ca` / `external-ca`). Charms are pulled from
Charmhub, so this step depends on Charmhub API availability. The model runs on
`microk8s_cloud/localhost` under the `foundations-microk8s` controller.

## Swift Artifacts

Objects stored under `<uuid>/generated/cos/`:

| Path | Description | When to check |
|---|---|---|
| `generated/cos/log.txt` | FCE build log for this layer (juju deploy output) | Always — first stop |
| `generated/cos/bundle.yaml` | Generated overlay bundle (often nearly empty: `applications: {}`) | Verify overlay content |
| `generated/cos/offer_overlay.yaml` | Cross-model offer overlay | Offer/relation issues |
| `generated/cos/model_config.yaml` | Juju model config for the cos model | Model settings |
| `generated/cos/juju_status_foundations-microk8s_cos.txt` / `.json` | cos model status snapshot | See which apps deployed before failure |
| `generated/cos/juju_status_foundations-microk8s_controller.txt` | microk8s controller status | Controller health |
| `generated/cos/juju_status_foundations-maas_microk8s.txt` | Underlying microk8s (MAAS) status | K8s node health |
| `generated/cos/juju-crashdump-cos-<timestamp>.tar.gz` | Juju crashdump (agent logs, k8s pod logs) | Deep investigation |
| `generated/cos/juju-dump-db-cos-<timestamp>.tar.gz` | Juju DB dump | Rarely needed |

## Key Log Files

- Primary: `generated/cos/log.txt` — full `juju deploy --debug` output. Most cos failures are
  visible here directly (charm resolution, bundle errors, hook failures).
- `generated/cos/juju_status_foundations-microk8s_cos.txt` — shows which apps were created
  before an abort (apps added before the failing charm sit in `waiting/allocating`).
- Crashdump (`juju-crashdump-cos-*.tar.gz`) — per-unit agent logs and k8s pod logs for
  charm/workload hook failures.

Substrate is typically `virtual_maas`; check `generated/sshtest.txt` for tunnel health, but
cos failures are usually k8s/Charmhub-side and unrelated to the MAAS infra.

## Grep Patterns

```bash
# Primary error in the FCE deploy log
grep -n "ERROR\|cannot deploy bundle\|cannot resolve\|content-type\|Command failed" generated/cos/log.txt | head -30

# Charmhub resolution / API problems
grep -n "resolving with preferred channel\|unexpected content-type\|Located charm\|Located bundle" generated/cos/log.txt

# Which apps came up before an abort
cat generated/cos/juju_status_foundations-microk8s_cos.txt

# Charm/workload hook errors (if deploy succeeded but juju-wait failed)
grep -n "hook failed\|error\b" generated/cos/log.txt | tail -30
```

## Known Failure Patterns

### Pattern 1: Charmhub returns HTML instead of JSON — `unexpected content-type from server "text/html"`

**Symptom:**
```
ERROR cannot deploy bundle: cannot resolve "<charm>": resolving with preferred channel:
      unexpected content-type from server "text/html"
  github.com/juju/juju/cmd/juju/application/store.(*CharmAdaptor).ResolveCharm:68
  github.com/juju/juju/cmd/juju/application/deployer.(*bundleHandler).addCharm:643:
        cannot resolve "<charm>"

subprocess.CalledProcessError: Command '['juju', 'deploy', '-m', 'foundations-microk8s:cos',
  ... '--trust', 'ch:cos-lite']' returned non-zero exit status 1.
```

**Root cause:** Transient Charmhub (api.charmhub.io) failure. During the bundle's `addCharm`
pass, Juju re-queries Charmhub to resolve a charm with its preferred channel and the store
returns an HTML page (a CDN/proxy 5xx or maintenance page) instead of the expected JSON.
Juju's `CharmAdaptor.ResolveCharm` rejects the non-JSON `Content-Type: text/html` and aborts
the whole bundle deploy. Note the charm is usually `Located` fine in the earlier resolution
pass (e.g. "Located charm grafana-k8s in charm-hub, channel 1/stable") and only fails on the
second `addCharm` resolution — proof the charm exists and this is intermittent, not a
bad/missing charm. FCE runs `juju deploy` once with no retry, so a single blip fails the step.
The layer fails fast (~25s), not on a timeout.

**Evidence to look for:**
- `generated/cos/log.txt`: `Located charm "<charm>"` succeeds earlier, then
  `unexpected content-type from server "text/html"` on the `addCharm` pass.
- `generated/cos/juju_status_foundations-microk8s_cos.txt`: apps listed *before* the failing
  charm in bundle order are stuck `waiting / allocating "installing agent"` (deploy aborted
  mid-bundle).
- `duration_ms` on the `fce_build` action is small (tens of seconds) — a resolution abort,
  not a wait/timeout.
- `generated/sshtest.txt`: tunnel healthy (`infra1`, no errors) — infra not implicated.

**Recommendation:** Transient external dependency — re-run usually succeeds. A durable fix
would be to add retry-on-transient around store-backed `juju deploy` in FCE
(`juju_cli.py` `deploy()`), retrying when stderr matches `unexpected content-type` /
`resolving with preferred channel`.

**From run:** 30590337777 (UUID 91ab01c1, tor3-sqa-virtual_maas, branch main, 2026-07-31;
`cannot resolve "grafana-k8s"` during `ch:cos-lite` rev 26 deploy).

---

_Add more patterns below as they are discovered._

## Notes

- The generated `bundle.yaml` for cos is frequently near-empty (`applications: {}`); the real
  content comes from `ch:cos-lite` plus the offer/storage/tls overlays.
- cos runs on MicroK8s (`microk8s_cloud/localhost`), so failures are typically Charmhub-side
  or k8s pod/charm-hook issues — not MAAS bare-metal. Only dig into MAAS logs if the microk8s
  cluster itself is unhealthy.
- Charm resolution failures abort the entire bundle deploy atomically; partially-added apps in
  the status snapshot are an artifact of the abort, not the root cause.
