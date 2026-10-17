# Step Knowledge: sunbeam_configure

## Step Overview

Runs `sunbeam configure -m manifest.yaml` on the Sunbeam bootstrap node. This applies the
demo-setup Terraform plan after deployment/resize, including Glance image upload/read,
Neutron demo networking, and other post-deploy cloud configuration.

## Swift Artifacts

| Path | Description | When to check |
|---|---|---|
| `generated/sunbeam/output.log` | FCE/Sunbeam layer log showing the remote `sunbeam configure` command and wrapper exception | Always |
| `generated/sunbeam/output.condensed.log` | Condensed layer log | Quick triage |
| `generated/sunbeam/sosreport-<bootstrap>-*.tar.xz` | Bootstrap-node sosreport; includes openstack snap Terraform logs | Terraform failures |
| `generated/sunbeam/pods_openstack_logs.tgz` | OpenStack namespace pod logs | API/charm/MySQL failures |
| `generated/sunbeam/juju_debug_log_openstack.txt` | Juju unit logs for OpenStack model | Charm hook failures |
| `generated/sunbeam/juju_status_openstack.txt` | Final OpenStack model status | Confirm units in error |

## Key Log Files (inside archives)

| File | What it contains | When to use |
|---|---|---|
| `home/ubuntu/snap/openstack/common/etc/<cloud>/demo-setup/terraform-apply-*.log` in bootstrap sosreport | Terraform provider diagnostics for configure | Primary source for empty `TerraformException()` |
| `generated/sunbeam/logs-openstack-glance-*.txt` in `pods_openstack_logs.tgz` | Glance API request/traceback logs | Image upload/read failures |
| `generated/sunbeam/logs-openstack-mysql-*.txt` in `pods_openstack_logs.tgz` | MySQL container stdout | DB startup/error-log wrapper output |
| `generated/sunbeam/logs-openstack-*-mysql-router-*.txt` in `pods_openstack_logs.tgz` | mysql-router charm/workload logs | Router hook failures and MySQL 1040 |

## Grep Patterns

```bash
# Find the wrapper failure in GitHub/Sunbeam logs
grep -RIna "TerraformException\|terraform command failed\|sunbeam configure" files/<uuid>/generated/sunbeam

# Inspect configure Terraform error from the bootstrap sosreport
tar -xOJf files/<uuid>/generated/sunbeam/sosreport-<bootstrap>-*.tar.xz \
  '*/home/ubuntu/snap/openstack/common/etc/*/demo-setup/terraform-apply-*.log' |
  grep -a -n -C 4 "Error waiting for Image\|Expected HTTP response\|500"

# Correlate Glance 500 with DB errors
tar -xOzf files/<uuid>/generated/sunbeam/pods_openstack_logs.tgz \
  generated/sunbeam/logs-openstack-glance-*.txt |
  grep -a -n -C 6 "Too many connections\|1040\|HTTP/1.1\" 500"

# Check mysql-router hook failures
grep -na "MySQL Error (1040).*Too many connections\|hook failed: \"update-status\"" \
  files/<uuid>/generated/sunbeam/juju_debug_log_openstack.txt \
  files/<uuid>/generated/sunbeam/juju_status_openstack.txt
```

## Known Failure Patterns

### Pattern 1: MySQL primary stale max_connections after resize

**Symptom:**
```
Error configuring cloud: TerraformException()
Error: terraform command failed: /snap/openstack/<rev>/bin/terraform apply -input=false -auto-approve -no-color -json
```

Terraform's demo-setup log shows:
```
Error waiting for Image: Expected HTTP response code [200] when accessing
[GET .../openstack-glance/v2/images/<uuid>], but got 500 instead
```

Glance or mysql-router logs show:
```
sqlalchemy.exc.OperationalError: (pymysql.err.OperationalError) (1040, 'Too many connections')
MySQL Error (1040): Dba.get_cluster_set: Too many connections
```

**Root cause:** After `sunbeam cluster resize`, OpenStack services and mysql-router units
scale out and substantially increase connection demand on the MySQL primary. If mysql-k8s
rewrites `max_connections` in `z-custom.cnf` but does not apply it to the already-running
primary mysqld, the primary can continue enforcing its old lower connection ceiling while
post-resize clients exceed it. New DB connections are refused with errno 1040; OpenStack
APIs such as Glance return HTTP 500, causing configure's Terraform plan to fail.

**Evidence to look for:**
- `demo-setup/terraform-apply-*.log`: Glance image GET returns HTTP 500.
- `logs-openstack-glance-*.txt`: traceback contains `(1040, 'Too many connections')`.
- `juju_debug_log_openstack.txt`: mysql-router `update-status` hooks fail with
  `Dba.get_cluster_set: Too many connections`.
- `juju_status_openstack.txt`: many `*-mysql-router` units in `error`.
- If live access or container file capture is available: compare `z-custom.cnf`
  `max_connections` and mtime with mysqld start time/runtime value; check MySQL audit logs
  for repeated `"status":1040`.

### Pattern 2: Deferred mysql-k8s primary restart lands during configure

**Symptom:** Same wrapper `TerraformException()` within ~15 s of configure start; the
demo-setup Terraform log shows Keystone `POST .../openstack-keystone/v3/auth/tokens` → `500`.
Keystone wsgi logs at that second show `(2013, 'Lost connection to MySQL server during query')`
and/or `(2003, "Can't connect to MySQL server on '<keystone>-mysql-router-service...' (Connection refused)")`.

**Root cause:** During `sunbeam cluster resize`, mysql-k8s logs `Configuration change requires restart`
on the (then single) primary and queues a rolling restart. `_restart` (`charm.py:690`
`self.peers.units.pop()`) fails with `KeyError: 'pop from an empty set'` → `retry-release`
until peers are usable, so the restart is deferred (seen: 25 attempts / 16 min). It finally
runs right after resize returns: pebble `Service "mysqld" stopped` → mysqld restart →
unit becomes SECONDARY. The short DB outage gives Keystone 500s, killing configure's first
auth. Transient: model is all-active minutes later.

**Evidence to look for:**
```bash
grep -an "Configuration change requires restart\|callback_id=restart\|pop from an empty set\|Service \"mysqld\" stopped\|result release" \
  <pods>/generated/sunbeam/logs-openstack-mysql-0.txt
grep -an "07:38:1.*\(2013\|2003\|HTTP/1.1\" 500\)" <pods>/generated/sunbeam/logs-openstack-keystone-*.txt
```
Correlate the mysqld stop time with the Terraform 500 timestamp. 1040 errors earlier
(before the restart) point to Pattern 1 being a contributing factor.

### Pattern 3: SSH to bootstrap node reset by runner egress proxy (aproxy 503)

**Symptom:** Step fails ~1 s after start, before `sunbeam configure` runs:
```
kex_exchange_identification: read: Connection reset by peer
Connection reset by <bootstrap-ip> port 22
CalledProcessError: ... 'sunbeam', 'configure', '-m', 'manifest.yaml' ... exit status 255
```

**Root cause:** The runner relays outbound TCP through `aproxy_nosni`, which sends it to the
egress squid (`egress.ps7.internal:3128`). A transient squid 503 on the CONNECT to `<ip>:22`
makes aproxy reset the client. `run_remote_command` has no retry. The cluster is healthy;
this is a CI infra flake.

**Evidence to look for:**
- Bootstrap sosreport `var/log/auth.log` has **no sshd line at all** at the failure second,
  so the connection never arrived. Rule out MaxStartups with `sos_commands/ssh/sshd_-T`.
- run.log shows `aproxy_nosni ... installed` early in setup. The `proxy return 503 response
  for connect request` line is only in the live runner journal (happy-bot may quote it).
- sshuttle routes only the MetalLB ranges, not the node IPs.

### Pattern 4: Neutron default-SG creation fails on resize-added units (ovn_revision_numbers 1062)

**Symptom:** Same wrapper `TerraformException()` about 5 min after configure starts. In the
demo-setup Terraform log, `data.openstack_networking_secgroup_v2.secgroup_default` gets
`GET .../security-groups?name=default&tenant_id=<demo>` → **500**, and/or
`openstack_networking_network_v2.user_network` gets `POST .../networks` → **504** (HTML).

**Root cause:** Neutron API units added by `sunbeam cluster resize` (seen on neutron/1 and
neutron/2, neutron-k8s `2026.1/beta` rev 376) fail every default security-group creation.
The OVN `_create_security_group_precommit` raises
`IntegrityError (1062, "Duplicate entry '<sg-uuid>-security_groups' for key 'ovn_revision_numbers.PRIMARY'")`.
The duplicated row is for the SG UUID that the same request just generated, so this is not
a race between requests. `oslo_db` retries until "DB exceeded retry limit" (about 2.5 min)
→ 500. A network POST that has to ensure the default SG loops through retry cycles until
mod_wsgi times out at 300 s (`Timeout when reading response headers from daemon process 'neutron-api'`)
→ 504. The original unit (neutron-0) creates default SGs normally. A
`Duplicate entry '...-mechanism_driver' for key 'ovn_hash_ring.PRIMARY'` on a new unit
suggests that OVN driver registration ran twice; this is unconfirmed. MySQL is healthy, with
no 1040, 2003 or 2013 errors in the window.

**Evidence to look for:**
```bash
P=<pods>/generated/sunbeam
grep -aho "for key '[^']*'" $P/logs-openstack-neutron-*.txt | sort | uniq -c
grep -an 'security-groups?name=default.*" 500\|networks HTTP/1.1" 504\|Timeout when reading response headers' $P/logs-openstack-neutron-*.txt
grep -ah "type: security_groups" $P/logs-openstack-neutron-*.txt   # successes only on the original unit
grep -a '^unit-neutron-[0-9]*: .*ran "install"' <bundle>/generated/sunbeam/juju_debug_log_openstack.txt
```

## Notes

- For `tor3-sqa-testflinger`, MAAS logs and `sshtest.txt` are not expected; use Sunbeam,
  pod, Juju, and sosreport artifacts instead.
