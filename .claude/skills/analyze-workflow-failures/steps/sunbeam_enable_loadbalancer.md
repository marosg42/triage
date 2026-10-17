# Step Knowledge: sunbeam_enable_loadbalancer

## Step Overview

Runs `sunbeam enable -m manifest.yaml loadbalancer` on the Sunbeam bootstrap node (`node1`) over SSH
via `products/sunbeam/enable_features.py` (`enable_plugins` → `run_remote_command`). On
`tor3-sqa-virtual_maas`, the SSH goes through the runner's `sshuttle` tunnel to the virtual lab.
For the amphora variant, see `sunbeam_enable_loadbalancer-amphora.md`.

## Swift Artifacts

| Path | Description | When to check |
|---|---|---|
| `generated/github-runner/run.log` | Runner log incl. the remote command output and sshuttle journal lines | Always — first stop |
| `generated/sshtest.txt` | Per-minute SSH health probe through the tunnel (virtual_maas) | Rule out tunnel loss |
| `generated/sunbeam/output.log` | Sunbeam command output + log-collection scp/ssh output | Sunbeam-side errors |
| `generated/sunbeam/hosts` | Node name → IP pinning | Map `node1` to its IP |
| `generated/maas/logs-<ts>/<ip>.tgz` | MAAS infra logs | Only if collected while the tunnel was up |

## Key Log Files (inside tgz archive)

| File | What it contains | When to use |
|---|---|---|
| `var/log/syslog` | Infra host syslog | Check the hostname first — Pokémon names mean dedicated_maas, not this run |

## Grep Patterns

```bash
# Remote command failure
grep -n "command not found\|returned non-zero exit status" files/run_<run_id>_failed.log

# Tunnel loss
grep -n "REMOTE HOST IDENTIFICATION HAS CHANGED\|infra1" files/<UUID>/generated/sshtest.txt | tail
grep -an "sshuttle\[.*closed by remote host\|c : fatal" files/<UUID>/generated/github-runner/run.log
```

## Known Failure Patterns

### Pattern 1: Mid-run sshuttle tunnel drop → wrong-host SSH (`sunbeam: command not found`)

**Symptom:**
```
bash: line 1: sunbeam: command not found
subprocess.CalledProcessError: Command '['ssh', '-t', ..., 'node1.<domain>', '--', 'sunbeam',
  'enable', '-m', 'manifest.yaml', 'loadbalancer']' returned non-zero exit status 127.
```
The step fails within seconds, on a node where `sunbeam` worked minutes earlier (e.g. the
previous `sunbeam enable ... ldap`).

**Root cause:** the runner's `sshuttle` tunnel to the virtual_maas relay died shortly before
the step started. virtual_maas and dedicated_maas share the same IP range/DNS domain, so the SSH
to `node1` reached an unrelated dedicated_maas host without `sunbeam`. Node SSH uses
`StrictHostKeyChecking=no` / `UserKnownHostsFile=/dev/null`, so the host-key change is not
caught. This is an infrastructure failure, not a Sunbeam/Octavia one: the loadbalancer feature
was never attempted.

**Evidence to look for:**
- `generated/sshtest.txt`: the last good entry is the last `infra1`, followed by
  `REMOTE HOST IDENTIFICATION HAS CHANGED` for `10.241.144.2` (see SKILL.md 4a-i).
- `generated/github-runner/run.log`: `sshuttle[...]: Connection to <relay> closed by remote host`
  / `c : fatal`. These lines may only appear in the quarantined AI-analysis tail; read them only
  after Step 7.
- `generated/sunbeam/output.log` (log collection): `REMOTE HOST IDENTIFICATION HAS CHANGED` for
  `node1` and `scp: .local/share/juju: No such file or directory`.
- The later MAAS tgz files (Pokémon hostnames such as leafeon/mienfoo/swoobat) and
  `sosreport-juju-1-*` come from dedicated_maas. Ignore them.

**From run** 38672193053 (UUID 51c0e5a3, tor3-sqa-virtual_maas cluster_2).

---

_Add more patterns below as they are discovered._

## Notes

- The tunnel is set up in `setup_virtual_lab` and is not supervised or restarted. See also
  `juju_maas_controller.md` Patterns 10/12 for the same mechanism in other steps.
- The cause of the relay-side disconnect is usually not determinable from the bundle (no relay logs).
