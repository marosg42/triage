# Step Knowledge: juju_microk8s_controller

## Step Overview

Bootstraps a Juju controller (`foundations-microk8s`) **inside** the MicroK8s cluster deployed by
the `microk8s` layer. FCE runs `juju_install`, `microk8s_setup_controller`
(`juju remove-k8s` / `juju add-k8s microk8s_cloud --client --storage ceph-rbd`) and then
`bootstrap` with `--config controller-service-type=loadbalancer --config bootstrap-timeout=1800`.
The controller StatefulSet needs a PVC from the `ceph-rbd` StorageClass
(provisioner `rook-ceph.rbd.csi.ceph.com`) and a MetalLB LoadBalancer IP (from `metallb_microk8s`).

## Swift Artifacts

| Path | Description | When to check |
|---|---|---|
| `generated/juju_microk8s_controller/log.txt` | FCE build log for this layer | Always — first stop |
| `generated/juju_microk8s_controller/model_defaults.yaml` | Model defaults passed to bootstrap | Proxy / config issues |
| `generated/microk8s/log.txt` | FCE log of the `microk8s` layer — **check `install_microceph`** | Any PVC / storage problem |
| `generated/microk8s/ceph-rbd.yaml` | The `ceph-rbd` StorageClass FCE re-created | Provisioner / pool name |
| `generated/microk8s/juju-crashdump-microk8s-*.tar.gz` | Per-node syslog of microk8s-1/2/3 (kubelite events) | PVC / pod events |
| `generated/metallb_microk8s/log.txt` | MetalLB layer log | LoadBalancer IP issues |
| `generated/maas/logs-*.tgz` | MAAS infra logs (dedicated/virtual_maas only) incl. bind `named` | DNS / proxy problems |

## Key Log Files

| File | What it contains | When to use |
|---|---|---|
| crashdump `<m>/baremetal/var/log/syslog` | `microk8s.daemon-kubelite` events (PVC `ExternalProvisioning`, pod scheduling) | Why controller-0 is unschedulable |
| MAAS tgz `<infra-ip>/var/log/syslog` | `named[...]` upstream resolution errors | DNS failures seen by nodes |

## Grep Patterns

```bash
# Bootstrap error
grep -n "failed to bootstrap\|pvc storage-controller-0\|ExternalProvisioning" files/run_<run_id>_failed.log

# Did the Rook operator / CSI actually install in the microk8s layer?
grep -n "enable rook-ceph\|INSTALLATION FAILED\|charts.rook.io\|connect-external-ceph" files/<UUID>/generated/microk8s/log.txt

# Any rook/CSI pods ever on the nodes?
grep -a -c "csi-rbdplugin\|rook-ceph-operator" files/<UUID>-mk8s-crashdump/*/*/baremetal/var/log/syslog

# Upstream DNS failures on MAAS bind (dedicated/virtual_maas)
grep -a "timed out resolving" files/<UUID>-maas-logs/*/var/log/syslog | grep -a "<HH:MM>"
```

## Known Failure Patterns

### Pattern 1: Controller PVC stuck `ExternalProvisioning` — Rook-Ceph operator never installed

**Symptom:**
```
ERROR failed to bootstrap model: creating controller stack: creating statefulset for controller:
timed out waiting for controller pod: unschedulable: pvc storage-controller-0 pending due to
ExternalProvisioning - Waiting for a volume to be created either by the external provisioner
'rook-ceph.rbd.csi.ceph.com' ...
root ERROR juju controller is bootstrapped but unreachable.
subprocess.CalledProcessError: Command '['juju', 'show-controller', 'foundations-microk8s']' ...
```
(fires exactly 1800 s after `Creating k8s resources for controller`).

**Root cause:** The failure originates in the **`microk8s` layer**, not here. Its
`install_microceph` step runs `microk8s enable rook-ceph`, which pulls the Rook Helm chart from
`https://charts.rook.io/release` directly (not via the MAAS squid proxy). If that fetch fails
(e.g. DNS `lookup charts.rook.io on 127.0.0.53:53 ... i/o timeout`), the operator and Ceph-CSI
provisioner are never installed, but the addon still prints a success banner and FCE does not
fail. `connect-external-ceph` still creates the `ceph-rbd` StorageClass, FCE's only check
(`kubectl get sc ceph-rbd`) passes, and `existing_juju_maas_controller_microk8s` /
`metallb_microk8s` succeed because they don't provision volumes. The first PVC consumer is the
controller bootstrap here, which waits 30 min and times out.

**Evidence to look for:**
- `generated/microk8s/log.txt`: `Error: INSTALLATION FAILED: failed to download "rook-release/rook-ceph"` after `microk8s enable rook-ceph`.
- Crashdump syslogs: no `rook-ceph-operator` / `csi-rbdplugin` lines on any node; repeated `ExternalProvisioning` events for `storage-controller-0`.
- MAAS bind (`grep -a`): `timed out resolving 'charts.rook.io/A/IN': <upstream_dns>#53` at the same minute (upstream from `config/project.yaml` `upstream_dns`).

**Observed:** tor3-sqa-dedicated_maas / dh1_j6 (2026-10-14) — upstream forwarder 10.239.8.11
timed out 12:01–12:04Z on all 3 MAAS bind servers.

**See also:** `steps/microk8s.md` Pattern 3.

---

_Add more patterns below as they are discovered._

## Notes

- `ERROR model foundations-maas:admin/openstack not found` from `juju export-bundle` at step start is expected/harmless.
- `juju remove-k8s microk8s_cloud --client` before `add-k8s` is normal cleanup.
