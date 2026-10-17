# Pattern: Juju k8s controller pod flaps NotReady → MetalLB withdraws controller VIP → `juju show-user` timeout

## Summary

On Sunbeam, the Juju controller (`sunbeam-controller`) runs as pod `controller-0` in namespace
`controller-sunbeam-controller` and is exposed by the LoadBalancer service `controller-service`
(port 17070, MetalLB L2 VIP). When the pod's readiness flips to NotReady, its endpoint is removed,
so **both** the ClusterIP and the VIP stop working, and MetalLB withdraws the VIP with
`reason: notOwner` (no node has an eligible endpoint — no other speaker takes over).
Any `sunbeam` CLI command during that window fails, because sunbeam first runs
`juju show-user` under pexpect with a **30 s** timeout.

Distinct from `metallb-speaker-wedge.md`: here MetalLB is healthy and logging; it is correctly
reacting to the endpoint disappearing.

## Symptom (GitHub log)

```
ssh <node> -- sunbeam tls ca list_outstanding_csrs --format yaml     (or any sunbeam command)
Error: Timeout exceeded.
<pexpect.pty_spawn.spawn object ...>
command: /snap/openstack/<rev>/juju/bin/juju
args: ['/snap/openstack/<rev>/juju/bin/juju', 'show-user']
buffer (last 100 chars): b''
timeout: 30
subprocess.CalledProcessError: ... returned non-zero exit status 1.
```

## Diagnostics

```bash
# 1. Controller VIP
grep controller-service generated/sunbeam/kubectl_get_svc.txt          # EXTERNAL-IP = VIP

# 2. Announce/withdraw timeline for the VIP across all speakers
mkdir -p files/<short-uuid>-pods && tar -xzf generated/sunbeam/pods_metallb-system_logs.tgz -C files/<short-uuid>-pods
grep -h '<VIP>' files/<short-uuid>-pods/generated/sunbeam/logs-metallb-system-metallb-speaker-*.txt \
  | grep -E 'serviceAnnounced|serviceWithdrawn' | grep -oE '"event":"[a-zA-Z]+"|"reason":"[a-zA-Z]+"|"ts":"[^"]+"' | paste - - - 

# 3. Pod readiness: Ready.lastTransitionTime should equal the last re-announce; restartCount 0
python3 -c "import yaml;d=yaml.safe_load(open('generated/sunbeam/kubectl_get_pod_detailed.txt'));[print(p['status']['conditions']) for p in d['items'] if p['metadata']['name']=='controller-0']"

# 4. Controller debug-log silence + reconnect burst
grep -oE ' [0-9]{2}:[0-9]{2}:' generated/sunbeam/juju_debug_log_controller.txt | sort | uniq -c   # look for a ~0 minute
grep 'connection reset by peer' generated/sunbeam/juju_debug_log_controller.txt | awk '{print $2}' | cut -c1-5 | uniq -c
```

Signature: a `serviceWithdrawn … notOwner` → `serviceAnnounced` gap on a single speaker that
brackets the failing command, no other speaker announcing the VIP, the controller debug log
going silent for the same interval, then a burst of `error serving RPCs: … connection reset by
peer` + mass `agent disconnected`/`agent login` from the machine hosts at the re-announce second.

## Probe config (Juju 3.6.29 controller pod)

- `mongodb` readiness: exec `mongo --tls … --eval db.adminCommand('ping')`,
  `timeoutSeconds: 1`, `periodSeconds: 10`, `failureThreshold: 3` — very tight for a TLS mongo
  shell on a busy node.
- `api-server` readiness: `GET :38811/v1/health?level=ready`, `timeoutSeconds: 3`,
  `periodSeconds: 5`, `failureThreshold: 2`.

The bundle has no kubelet events, so which probe failed cannot be proven post-mortem; liveness
did not fail (restartCount 0). In UUID b53d2eac the live triage by happy-bot captured the probe event:
`Readiness probe failed: mongo --port=37017 … ping … timed out after 1s`. It also saw the control
node at ~99 % CPU (`kubectl top nodes`). The mongodb probe is therefore the confirmed trigger there.

## Observed

- run 38924413922 (UUID b53d2eac-c9a0-45f1-84a1-a400bfb8c48a, tor3-sqa-shared_maas dh1_j8_2,
  branch less_sshuttle, 2026-10-16), `sunbeam_enable_tls`: VIP `10.241.37.130` dark
  19:52:02–19:53:27Z; `list_outstanding_csrs` failed 19:53:14Z. Shorter (1–21 s) flaps also
  during `sunbeam_resize` and earlier in the same step were survived.

## Recommendations

- Retry sunbeam CLI calls on `Timeout exceeded` + `show-user` in `enable_features.py`.
- Collect `kubectl get events -n controller-sunbeam-controller` and node load in log collection.
- Raise the 1 s mongodb readiness timeout / 30 s show-user pexpect timeout with Sunbeam/Juju.
