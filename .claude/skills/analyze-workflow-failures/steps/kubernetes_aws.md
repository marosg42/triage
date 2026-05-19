# Step Knowledge: kubernetes-aws

## Step Overview

This step deploys Charmed Kubernetes on AWS infrastructure using a Juju bundle. FCE runs
`fce build --layer kubernetes-aws`, which:
1. Creates a new Juju model `kubernetes-aws` on the `foundations-aws` controller (AWS IAAS cloud)
2. Deploys a full CK bundle including: aws-integrator, aws-cloud-provider, aws-k8s-storage,
   calico, containerd, easyrsa, etcd, kubeapi-load-balancer, kubernetes-control-plane,
   kubernetes-worker, mysql-innodb-cluster, ntp, vault, mysql-router
3. Runs `juju-wait -m foundations-aws:kubernetes-aws -t 3600` until all units are active/idle

The `aws-integrator` charm is a critical blocker: all networking and storage charms
(calico, kubernetes-control-plane, kubernetes-worker, aws-cloud-provider, aws-k8s-storage)
wait for it to grant IAM credentials before they can proceed.

Entry point: `fce build --layer kubernetes-aws` (via `.github/actions/builds/run-fce-build`)

This step runs on `ext-sqa-aws / useast1` (AWS us-east-1). No MAAS logs are available —
investigation is limited to GitHub Actions logs and Swift artifacts.

## Swift Artifacts

| Path | Description | When to check |
|---|---|---|
| `generated/kubernetes-aws/log.txt` | FCE layer log — model creation, deploy, wait steps | Always first |
| `generated/kubernetes-aws/bundle.yaml` | Deployed bundle (charms, channels, units) | Verify config |
| `generated/kubernetes-aws/model_config.yaml` | Juju model config | Config verification |
| `generated/kubernetes-aws/juju_status_foundations-aws_kubernetes-aws.json` | Juju status JSON at failure | Unit statuses and blocked messages |
| `generated/kubernetes-aws/juju_status_foundations-aws_kubernetes-aws.txt` | Juju status text at failure | Human-readable unit view |
| `generated/kubernetes-aws/juju-crashdump-kubernetes-aws-<ts>.tar.gz` | Per-unit Juju agent logs | Deep investigation — contains unit-aws-integrator-0.log |
| `generated/kubernetes-aws/overlay-charms-version.yaml` | Charm channel overrides | Verify charm channels |
| `generated/version_collector_kubernetes-aws.log` | Snap list, versions at failure | Version context |

## Key Log Files (inside crashdump tgz)

Extract: `tar -xzf juju-crashdump-kubernetes-aws-*.tar.gz -C /tmp/k8s-aws-crashdump`

| File | What it contains | When to use |
|---|---|---|
| `<uuid>/0/baremetal/var/log/juju/unit-aws-integrator-0.log` | aws-integrator charm events and errors | **Primary for all aws-integrator failures** |
| `<uuid>/0/baremetal/var/log/juju/unit-aws-cloud-provider-0.log` | aws-cloud-provider unit log | Secondary — if cloud-provider blocked |
| `<uuid>/0/baremetal/var/log/juju/unit-aws-k8s-storage-0.log` | aws-k8s-storage unit log | Secondary — if storage blocked |
| `<uuid>/N/baremetal/var/log/juju/unit-kubernetes-control-plane-N.log` | k8s control plane agent | If k8s-cp units are in error |
| `<uuid>/N/baremetal/var/log/juju/unit-calico-N.log` | Calico CNI agent | If calico units in error |

## Grep Patterns

```bash
# Find all errors in aws-integrator log
grep -E "ERROR|blocked|EntityAlreadyExists|TagLimitExceeded|AWSError" \
  /tmp/k8s-aws-crashdump/*/0/baremetal/var/log/juju/unit-aws-integrator-0.log

# Get the sequence of blocked messages with timestamps
grep "status-set: blocked\|status-set: maintenance\|Granting request" \
  /tmp/k8s-aws-crashdump/*/0/baremetal/var/log/juju/unit-aws-integrator-0.log | tail -40

# Check the aws-integrator blocked message from juju status JSON
python3 -c "
import json
data = json.load(open('generated/kubernetes-aws/juju_status_foundations-aws_kubernetes-aws.json'))
ai = data['applications']['aws-integrator']
print('Status:', json.dumps(ai.get('application-status', {}), indent=2))
"

# Find units stuck in waiting/blocked in GH failed log
grep "workload status is blocked\|workload status is waiting" /tmp/run_<run_id>_failed.log | tail -30

# Find the juju-wait timeout
grep "Not ready in\|CalledProcessError.*juju-wait" /tmp/run_<run_id>_failed.log

# Find subnet tagging errors (TagLimitExceeded)
grep "TagLimitExceeded\|CreateTags" \
  /tmp/k8s-aws-crashdump/*/0/baremetal/var/log/juju/unit-aws-integrator-0.log

# Find IAM role create-role errors
grep "EntityAlreadyExists\|create-role\|CreateRole" \
  /tmp/k8s-aws-crashdump/*/0/baremetal/var/log/juju/unit-aws-integrator-0.log
```

## Known Failure Patterns

### Pattern 1: Stale IAM role causes EntityAlreadyExists — aws-integrator permanently blocked

**Symptom (in GitHub Actions log, ~60 min after step starts):**
```
ERROR:root:Not ready in 3600s (max_wait)

subprocess.CalledProcessError: Command ['juju-wait', '-m', 'foundations-aws:kubernetes-aws',
  '-t', '3600', ...] returned non-zero exit status 44.

DEBUG:root:aws-integrator/0 workload status is blocked since 2026-03-31 07:33:38+00:00
DEBUG:root:calico/0..4 workload status is waiting since ...
DEBUG:root:kubernetes-control-plane/0..1 workload status is waiting since ...
DEBUG:root:kubernetes-worker/0..2 workload status is waiting since ...
```

**Root cause:** The `aws-integrator` charm's `_ensure_role` function calls `aws iam create-role`
unconditionally and crashes with `EntityAlreadyExists` when the role already exists. The charm is
not idempotent for this case — it immediately sets to `blocked` and retries the full grant
operation from scratch on every Juju hook retry (~every 4–6 minutes), never recovering.

This can be triggered in two ways:
- **Cross-run:** A stale IAM role from a previous pipeline run (same Juju model UUID) was not
  cleaned up in teardown.
- **Same-run (intra-run re-trigger):** aws-integrator successfully creates the role during the
  first juju-wait, but a subsequent `aws-relation-changed` event (e.g., fired by k8s-cp after a
  config update) triggers `handle_requests` again in the second juju-wait, hitting the role that
  was just created minutes earlier. The model is fresh (just `juju add-model`), so no cross-run
  stale state is involved.

Since `aws-integrator` cannot grant credentials, all dependent charms (calico, k8s-control-plane,
k8s-worker, aws-cloud-provider, aws-k8s-storage) remain stuck in `waiting`. The Kubernetes
cluster never deploys. juju-wait times out after 3600s with exit code 44.

**Evidence to look for:**
- `unit-aws-integrator-0.log`: Repeating pattern of:
  ```
  status-set: maintenance: Granting request for kubernetes-control-plane/0
  CalledProcessError: 'aws iam create-role --role-name charm.aws.<uuid>.kubernetes-control-plane' exit 254
  AWSError: EntityAlreadyExists ... Role with name charm.aws.<uuid>.kubernetes-control-plane already exists.
  status-set: blocked: Error while granting requests (unknown); check credentials and debug-log
  ```
- `juju_status_foundations-aws_kubernetes-aws.json`: `aws-integrator` application-status `blocked`
  with message `"Error while granting requests (unknown); check credentials and debug-log"`
- Retry cadence: every ~4–6 minutes, never recovers

**Secondary finding often present:**

Also at first block attempt, `TagLimitExceeded` may appear:
```
AWSError: aws: [ERROR]: An error occurred (TagLimitExceeded) when calling the CreateTags
  operation: The resultant tag set must not have more than 50 user tags.
```
This means VPC subnets have accumulated ≥50 user tags from prior Kubernetes cluster deployments
that weren't cleaned up. AWS has a hard limit of 50 user tags per resource.

**Cleanup investigation:**
The stale IAM role should have been deleted by either:
- The `Clean up public cloud` step (runs before `juju_aws_controller`), OR
- `juju destroy-model` (end-of-run teardown) triggering the aws-integrator `relation-broken` hook

Check whether `aws-nuke` or Terraform cleanup covers IAM roles matching `charm.aws.*`. The
`TagLimitExceeded` error similarly points to subnet tag cleanup being incomplete.

**Observed in:**
- Run 23782846040 (UUID: 07904a30-b935-44ff-9079-c2a0d29e6251, ext-sqa-aws useast1, 2026-03-31)
  - First block at 06:40:56 (TagLimitExceeded on subnet tagging)
  - EntityAlreadyExists first seen at 06:43:30, repeated every 4–6 min until juju-wait timeout
  - Stale role: `charm.aws.bc92c34d-8335-46d0-89ed-139288a6c0ba.kubernetes-control-plane`
  - Fresh controller bootstrap (juju_aws_controller ran at 06:18–06:20), so stale role was from prior run
- Run 24122087737 (UUID: c46a2ec4-7cf5-4237-ae1e-87444983a8c3, ext-sqa-aws useast1, 2026-04-08)
  - Same-run intra-run trigger (no cross-run stale state): role created successfully for
    `kubernetes-control-plane/0` at 07:14:49 during first juju-wait; second `aws-relation-changed`
    event at 07:19:21 triggered re-creation attempt → EntityAlreadyExists at 07:19:30
  - No TagLimitExceeded in this run; 11 retries from 07:19:30 to 08:15:49; juju-wait timeout at 08:17:54
  - aws-integrator: latest/beta rev 75 (26ad2b4)
- Run 26070574226 (UUID: 81d842aa-ca96-4860-9017-a0828575de33, ext-sqa-aws useast1, 2026-05-19)
  - **IAM propagation sub-variant:** role created (01:56:09Z), instance-profile created (01:56:10Z),
    role attached to profile (01:56:11Z); `AssociateIamInstanceProfile` → `InvalidParameterValue:
    Invalid IAM Instance Profile name` 1s after profile creation (EC2 not yet seen the new profile)
  - charm → `blocked` at 01:56:12Z; 17 EntityAlreadyExists retries (01:57:22Z → 02:51Z); juju-wait
    timeout at 02:54:58Z (exit code 44)
  - No TagLimitExceeded; no cross-run stale state (fresh model UUID 9ea5f188)
  - `_retry_for_entity_delay` does NOT catch `InvalidParameterValue`, only `NoSuchEntity`-type errors
  - aws-integrator: latest/beta rev 75

---

_Add more patterns below as they are discovered._

## Notes

- `ext-sqa-aws` is a public cloud substrate — **no MAAS logs available**. Investigation is
  limited to GitHub Actions logs and Swift artifacts (juju crashdump is the primary source).
- The `foundations-aws` controller may be freshly bootstrapped each run (as observed) or
  reused across runs depending on the pipeline configuration.
- IAM role names include the Juju model UUID: `charm.aws.<model-uuid>.<charm-name>`. These
  are created per-run and must be cleaned up as part of teardown.
- The juju-wait flags for this step: `-t 3600 --machine-error-timeout 1800 --retry_errors 0
  --workload -x graylog -x ubuntu-advantage -x etcd`. Exit code 44 = timeout.
- `--retry_errors 0` means juju-wait does NOT exit early on error state — it waits the full
  3600s even when units are blocked. This means a permanent blocker like `EntityAlreadyExists`
  will always exhaust the full timeout.

## Version History

- **v1.0** (2026-03-31): Initial version from analysis of run 23782846040 (UUID 07904a30,
  ext-sqa-aws useast1). EntityAlreadyExists IAM role + TagLimitExceeded subnet pattern documented.
- **v1.1** (2026-04-08): Extended Pattern A to cover same-run intra-run re-trigger (not only
  cross-run stale role). Confirmed via run 24122087737 (UUID c46a2ec4, useast1): fresh model,
  role created at 07:14:49, re-creation triggered at 07:19:21 by second aws-relation-changed event.
- **v1.2** (2026-05-19): Added IAM propagation sub-variant to Pattern A (run 26070574226, UUID
  81d842aa): `AssociateIamInstanceProfile` fails with `InvalidParameterValue` 1s after instance
  profile creation (EC2 propagation lag); `_retry_for_entity_delay` only catches `NoSuchEntity`,
  not `InvalidParameterValue`; leaves orphaned role; 17 EntityAlreadyExists retries until timeout.
