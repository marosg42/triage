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

## Notes

- For `tor3-sqa-testflinger`, MAAS logs and `sshtest.txt` are not expected; use Sunbeam,
  pod, Juju, and sosreport artifacts instead.
