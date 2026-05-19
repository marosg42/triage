# Step Knowledge: juju_k8s_controller

## Step Overview

Bootstraps a Juju controller onto a managed Kubernetes cluster (AKS, EKS, etc.) that was
provisioned by a preceding `k8s_cloud` step. The step:
1. Optionally upgrades Juju to the channel specified in `extra_args.CHANNEL`
2. Calls `juju add-k8s ${CLOUD}-cloud --client` to register the cluster
3. Calls `juju bootstrap ${CLOUD}-cloud ${CLOUD}-controller`
4. Adds the controller API endpoint to the runner's no-proxy list

**Substrates:** `ext-sqa-aks` (Azure), `ext-sqa-eks` (AWS). No MAAS logs; no SSH tunnel.

## Swift Artifacts

| Path | Description | When to check |
|---|---|---|
| `generated/version_collector_juju_k8s_controller.log` | Snap list, juju controllers, git refs after the step | Check snap versions |
| `generated/versions.yaml` | Collected versions including `juju_k8s_controller` key | Quick version overview |
| `generated/foundation.log` | FCE high-level command log | Timeline orientation |

No MAAS logs. No nested tgz. This step produces minimal Swift artifacts because bootstrap
failure is fast (Kubernetes API rejection) and infra is cloud-managed.

## Key Log Files

No tgz archive for this substrate. All diagnostic information comes from GitHub Actions logs.

## Grep Patterns

```bash
# Find the bootstrap error in GitHub Actions failed log
grep "invalid reference\|bootstrap.*error\|ERROR.*bootstrap\|failed to bootstrap" /tmp/run_<run_id>_failed.log

# Find the Juju upgrade section
grep "Switching juju\|switched to the\|refreshed\|add-k8s\|bootstrap" /tmp/run_<run_id>_failed.log | grep -v "##\[group\]\|SUBSTRATE\|TWC\|VAULT"

# Check Kubernetes version and AKS cluster name from k8s_cloud output
grep "current_kubernetes_version\|cluster-\|Creation complete" /tmp/run_<run_id>_failed.log
```

## Known Failure Patterns

### Pattern 1: `invalid reference format` — `JujudOCINamespace` baked as empty string in snap build

**Symptom:**
```
Creating Juju controller "aks-controller" on aks-cloud/eastus
Bootstrap to Kubernetes cluster identified as azure/eastus
Creating k8s resources for controller "controller-aks-controller"
ERROR failed to bootstrap model: creating controller stack: creating statefulset for controller: invalid reference format
WARNING k8s cluster is not accessible: custom resource definition volumesnapshotclasses.snapshot.storage.k8s.io v1beta1 is not a supported and served version
Process completed with exit code 1.
```

**Full stack trace (from `--debug`):**
```
failed to bootstrap model
  → environs/bootstrap.Bootstrap:710
  → environs/bootstrap.bootstrapCAAS:295
  → provider/kubernetes.(*kubernetesClient).Bootstrap.func1:495: creating controller stack
  → provider/kubernetes.(*controllerStack).Deploy:466: creating statefulset for controller
  → provider/kubernetes.(*controllerStack).createControllerStatefulset:851
  → provider/kubernetes.(*controllerStack).buildContainerSpecForCommands:1324
  → internal/cloudconfig/podcfg.tagImagePath:91
  → invalid reference format
```

**Root cause:** A snap build defect in commit `29278b68a4` (2026-04-16) introduced into
`4.0/candidate`. The commit changed `JujudOCINamespace` in `internal/cloudconfig/podcfg/image.go`
from a Go **constant** (`"ghcr.io/juju"`) to a **variable** intended to be injected by the
linker at build time via `PULL_OCI_REGISTRY`. The `snapcraft.yaml` was updated to pass the
linker flag, but with an **empty value**:

```
-X github.com/juju/juju/internal/cloudconfig/podcfg.JujudOCINamespace=
```

The `PULL_OCI_REGISTRY` env var is not set in the snap build environment, so the linker
overrides the Go default `"ghcr.io/juju"` with `""`. At runtime, `imageRepoToPath` in
`image.go` falls back to `JujudOCINamespace` (also `""`), producing the path `/jujud-operator`
(leading slash). `reference.Parse("/jujud-operator")` rejects it immediately with
`invalid reference format`. **The failure is entirely local** — no StatefulSet is ever
submitted to the Kubernetes API.

The relevant code path in `image.go`:
```go
func imageRepoToPath(imageRepo, tag string) (string, error) {
    if imageRepo == "" {
        imageRepo = JujudOCINamespace          // "" — overridden by linker to empty
    }
    path := fmt.Sprintf("%s/%s", imageRepo, JujudOCIName)  // "/jujud-operator"
    return tagImagePath(path, tag)             // reference.Parse("/jujud-operator") → error
}
```

`4.0/stable` was built before this commit — `JujudOCINamespace` was still a constant
`"ghcr.io/juju"`, so bootstrap always succeeded.

NOTE: `jujud-controller-snap-source:legacy` visible in the `--debug` controller config dump
was an early red herring — it is not the cause.

**The fix** (one line in `snapcraft.yaml`): either remove the `-X` flag entirely to let
the Go default stand, or set it to the explicit value:
```
-X github.com/juju/juju/internal/cloudconfig/podcfg.JujudOCINamespace=ghcr.io/juju
```

**Evidence confirming local failure (not k8s API rejection):**
- Bootstrap completes service creation, 3 DNS polling attempts, configmap and secret updates
  before hitting the error — all earlier k8s API operations succeed.
- The ~11s gap between "Creating k8s resources" and the error is the DNS polling wait, not
  a network timeout.
- `--debug` log never prints any image reference string before the error — `tagImagePath`
  fails before any string can be logged.
- `volumesnapshotclasses v1beta1` warning is a post-failure diagnostic (k8s 1.32+ removed
  v1beta1) and is NOT the cause.

**Observed environment:**
- Juju 4.0.9 from `4.0/candidate` (snap rev 34852, Go 1.26.2), AKS Kubernetes 1.34.4, Azure East US
- SKU: `fkb-juju4-aks`, substrate `ext-sqa-aks`, `juju bootstrap aks-cloud aks-controller` (no flags)
- Two confirmed occurrences: runs 25087533337 (UUID b4c9324a) and 25098199361, 2026-04-29
- Introduced by: juju/juju commit `29278b68a4544a1fe3213a98eb2ad5d7f5434000`

---

_Add more patterns below as they are discovered._

## Notes

- This step runs on `ext-sqa-aks` substrate — no MAAS, no virtual infra, no SSH tunnels.
- The `volumesnapshotclasses.snapshot.storage.k8s.io v1beta1` CRD was removed in Kubernetes
  1.32+; Juju may print it as a post-failure cluster health warning. Do not treat it as the
  root cause unless bootstrap fails before the StatefulSet step.
- `juju add-k8s` success does NOT guarantee bootstrap will succeed — the cluster may be
  reachable but the image reference Juju generates for the target cloud type may be invalid.

## Version History

- **v1.0** (2026-04-29): Initial version — Pattern A: `invalid reference format` during
  Juju 4.0.9 bootstrap on AKS Kubernetes 1.34.4, run 25087533337 (UUID b4c9324a).
- **v1.1** (2026-04-29): Corrected root cause — not `jujud-controller-snap-source:legacy`
  but a snap build defect: `JujudOCINamespace` overridden to empty string by linker flag in
  `snapcraft.yaml` (commit `29278b68a4`, 2026-04-16). Confirmed via juju/juju source diff.
