# Pattern: MetalLB Speaker Wedge → VIP i/o timeout

## Summary

On a Sunbeam OpenStack deployed on Canonical K8s, service VIPs (Keystone public/internal,
RGW, etc.) are exposed by **MetalLB in layer2 mode**. If the MetalLB `speaker` process
**hangs/wedges** (process stays `Running`, but its controller-runtime reconcile loop stops and
it stops answering ARP for its announced VIPs), the affected VIP becomes unreachable from
outside the cluster after the ARP cache expires — even though the backing service pods
(e.g. Keystone) remain perfectly healthy on their internal ClusterIP path.

Any later step that must reach a MetalLB-announced VIP then fails with a TCP-level
`i/o timeout`, typically surfaced far downstream (e.g. Juju/Terraform "failed to open environ").

## Symptom (in GitHub Actions logs)

A `terraform apply` in `canonicalk8s_openstack` (or any OpenStack-consuming step) fails while
creating/using the Juju model, with a keystone token request timing out:

```
│ Unable to create model "canonicalk8s-openstack", got error: failed to
│ open environ: authentication failed.: authentication failed
│ caused by: requesting token: failed executing the request
│ https://<keystone-public-VIP>/openstack-keystone/v3/auth/tokens
│ caused by: Post ".../v3/auth/tokens": dial tcp <keystone-public-VIP>:443: i/o timeout
```

The tell-tale is `dial tcp <VIP>:443: i/o timeout` to an OpenStack **VIP** (not a pod IP).

## The key diagnostic: MetalLB logs go silent while pods stay "Running"

MetalLB pod logs are collected **post-failure** (via
`juju ssh -m admin/openstack-machines k8s/leader -- sudo k8s kubectl logs <pod> -n metallb-system --all-containers`)
into `generated/sunbeam/`:

```
generated/sunbeam/logs-metallb-system-metallb-controller-*.txt
generated/sunbeam/logs-metallb-system-metallb-speaker-*.txt
generated/sunbeam/pods_metallb-system_logs.tgz   # if not already extracted
```

A wedged speaker shows this signature:

- **All** `metallb-speaker` logs (and often the `metallb-controller`) **stop at the same time**,
  well before the failure, ending **mid-normal `ServiceReconciler`** with **no error / panic /
  SIGTERM / shutdown** line.
- `kubectl_get_pod.txt` shows the metallb pods `Running 1/1` with **0 restarts** and multi-hour
  ages → they did **not** restart or reschedule.
- The log files are **small** (well under k8s' 10 MiB rotation limit) → the silence is **not**
  log rotation and **not** a collection cut-off.
- Therefore the process produced **zero stdout for tens of minutes while still "Running"** =
  hung/wedged reconcile loop.

### How to prove it's MetalLB-specific (not a cluster-wide artifact)

Compare against the **openstack-namespace** pod logs, which are collected minutes later with the
identical `kubectl logs` method:

```bash
# metallb: last timestamps (expect them to stop early, e.g. ~12:41)
for f in /path/logs-metallb-system-metallb-*; do echo -n "$f "; grep -oE '2026-[0-9T:]+' "$f" | tail -1; done

# openstack pods: last timestamps (expect them CURRENT, up to collection ~13:5x)
for f in logs-openstack-keystone-0 logs-openstack-nova-0 logs-openstack-neutron-0; do
  echo -n "$f "; grep -oE '2026-[0-9T:]+' "$f".txt | tail -1; done
```

If keystone/nova/neutron logs are current but metallb stops early → the anomaly is **isolated to
MetalLB**, ruling out a kube-apiserver / node / log-pipeline outage.

## Why Keystone still looks healthy

The Keystone pod logs (`logs-openstack-keystone-{0,1,2}.txt`) show continuous `/healthcheck` 200s
and `auth/tokens` 200/201 straight through the failure window — because health checks and
internal traffic use the **ClusterIP**, which does not depend on MetalLB. Only the **external
VIP** path (used by the off-cluster Juju controller VM) is broken. So "keystone is fine" and
"the VIP times out" are both true simultaneously.

## Rule-outs to do first (so you don't misattribute)

- **sshuttle** (`generated/sshuttle_metallb_range.txt`): usually shows `sshuttle UP` every minute
  across the window. sshuttle only routes the runner→controller-API path; the controller-VM →
  Keystone-VIP connection rides OpenStack's own networking and does **not** traverse sshuttle.
- **Keystone/traefik health**: confirm the failing request never appears in keystone **or**
  traefik-public logs → the packet was dropped before the ingress (consistent with L2/VIP loss).

## Evidence checklist

- [ ] `run.log`: `dial tcp <VIP>:443: i/o timeout` to a keystone VIP during `terraform apply`.
- [ ] `logs-metallb-system-metallb-speaker-*`: all stop at the same early time, mid-reconcile,
      no error/shutdown.
- [ ] `kubectl_get_pod.txt`: metallb pods `Running`, `0` restarts, multi-hour age.
- [ ] metallb log files are small (not rotated).
- [ ] openstack-namespace pods (keystone/nova/neutron) collected with same method are **current**.
- [ ] keystone pod logs show healthy 200s through the window; failing request absent from
      keystone + traefik-public logs.
- [ ] sshuttle monitor shows tunnel UP (rule it out).

## Caveats (state these honestly)

- Because the speaker logs stop before the failure, you cannot **directly** observe the speaker's
  L2/ARP behaviour at failure time. The wedge is inferred from `Running` + prolonged zero output
  + L2-mode mechanics — not from tcpdump/ARP capture.
- The **trigger** of the wedge is usually not visible (no error precedes the silence). Candidates:
  Go deadlock, controller-runtime informer stall, memberlist issue, or a transient kube-apiserver
  watch disruption that only MetalLB failed to recover from.

## Recommendations

1. Investigate why the speakers wedged (goroutine dump / `kubectl describe` on rerun) and add
   **liveness/readiness probes** so a stalled speaker is killed and rescheduled instead of
   silently staying `Running`.
2. Make the consuming step resilient: pre-flight `openstack token issue` (or TCP probe) against
   the credential `auth_url`, and **retry on transient `i/o timeout`** so a VIP blip doesn't
   discard a multi-hour run at the final layer.
3. On reruns, capture MetalLB speaker logs covering the failure window to confirm the mechanism.

## Observed in

- Run 30245701655 / UUID `5e4b0726-0eed-4511-b55d-a4ba57b32679` (substrate `tor3-sqa-shared_maas`,
  cluster `dh1_j8_1`): metallb-controller silent from 12:37:11, all 4 speakers from 12:41:02;
  failure at 13:19:58 with `dial tcp 10.241.36.132:443: i/o timeout`. Keystone healthy throughout;
  openstack pods logged normally to 13:56–13:59; sshuttle UP. Surfaced in step `canonicalk8s_openstack`.
- Run 30214148446 / UUID `054a43a2-9a10-449a-b216-99ff66a3b8d7` (`tor3-sqa-shared_maas`): all 4
  speakers silent from 02:58:48 (controller 02:52:42); failure at 03:17:40 with
  `dial tcp 10.241.38.132:443: i/o timeout`. Openstack pods current to 03:52–03:55; metallb pods
  `Running` 0 restarts. Surfaced in step `juju_sunbeam_controller` (Juju bootstrap).
- Run 30196037796 / UUID `4f55d457-b9f9-4887-a8b6-31f0233d8064` (`tor3-sqa-shared_maas`): all 4
  speakers silent from 20:46:03 (controller 20:42:18); failure at 21:09:35 with
  `dial tcp 10.241.38.134:443: i/o timeout`. Openstack pods current to 21:45–21:48; metallb pods
  `Running` 0 restarts. Surfaced in step `juju_sunbeam_controller` (Juju bootstrap).
