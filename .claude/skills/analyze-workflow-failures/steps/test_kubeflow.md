# Step Knowledge: test_kubeflow

## Step Overview

Runs Charmed Kubeflow UATs (User Acceptance Tests) against a deployed Kubeflow bundle on a
Juju-managed Kubernetes model (`foundations-kubernetes:kubeflow`). It clones
`canonical/charmed-kubeflow-uats` at the branch specified by `UATS_BRANCH` (e.g.
`track/1.10`), installs dependencies via `tox`, then executes:

```bash
timeout 4h tox -e ${TEST_OPTION}
```

where `TEST_OPTION` is typically `kubeflow-remote`. The test runner uses `lightkube` and
`pytest-asyncio` to interact with the Kubernetes API at the cluster's control-plane address.

**UATS test sequence (`kubeflow-remote`):**
1. `test_bundle_correctness` — verifies CRDs and versions.yaml
2. `test_create_profile` — creates a Kubeflow Profile (namespace), verifies PodDefaults appear, then deletes it
3. `test_kubeflow_workloads` — full workload tests (depends on `test_create_profile`)

**Important:** `track/1.10` UATs require Python 3.12 (installed from deadsnakes PPA at
step start). Earlier branches use the system `python3`.

## Swift Artifacts

| Path | Description | When to check |
|---|---|---|
| `generated/test_kubeflow/log/1-install_deps.log` | deadsnakes PPA + python3.12 install | If step fails before tests run |
| `generated/test_kubeflow/log/2-freeze.log` | pip freeze of installed packages | Version conflict investigation |
| `generated/test_kubeflow/log/3-commands[0].log` | `poetry install` / tox env setup | Dependency install failures |
| `generated/test_kubeflow/log/4-commands[1].log` | Full pytest output with live logs | **Primary investigation file** |
| `generated/kubeflow/juju_status_foundations-kubernetes_kubeflow.json` | Juju model status at teardown | Charm health at time of failure |
| `generated/kubeflow/juju-crashdump-kubeflow-<timestamp>.tar.gz` | Full Juju crashdump | Deep charm-level investigation |
| `generated/kubeflow/juju-status-verbose.txt` | Verbose juju status | Charm hook errors, relation data |

## Grep Patterns

```bash
# Find the primary test failure line
grep -E "PASSED|FAILED|ERROR|short test summary" generated/test_kubeflow/log/4-commands[1].log

# Check for API errors from lightkube
grep -E "ApiError|429|storage is|etcd" generated/test_kubeflow/log/4-commands[1].log

# Check Juju error logs (dumped at teardown inside tox log)
grep "ERROR juju.worker\|ERROR unit\." generated/test_kubeflow/log/4-commands[1].log | head -20

# Find etcd errors specifically
grep "etcdserver\|leader failure\|request timed out" generated/test_kubeflow/log/4-commands[1].log
```

## Known Failure Patterns

### Pattern 1: etcd Leader Election causes `storage is (re)initializing` / HTTP 429

**Symptom (GitHub Actions log / `4-commands[1].log`):**
```
INFO  httpx:_client.py GET https://<k8s-api>/apis/kubeflow.org/v1alpha1/namespaces/test-kubeflow/poddefaults
      "HTTP/1.1 429 Too Many Requests"
FAILED driver/test_kubeflow_workloads.py::test_create_profile
       - lightkube.core.exceptions.ApiError: storage is (re)initializing

ERROR  driver/test_kubeflow_workloads.py::test_kubeflow_workloads
       - AssertionError: Waited too long for Profile test-kubeflow to be deleted!

FAILED driver/test_kubeflow_workloads.py::test_create_profile
       - lightkube.core.exceptions.ApiError: storage is (re)initializing
1 failed, 1 passed, 1 skipped, 1 error in 198.41s (0:03:18)
```

**Root cause:**

The Kubernetes API server returns `HTTP 429 Too Many Requests` with message body
`storage is (re)initializing` when the underlying etcd cluster is recovering from a leader
election. During re-initialization, the API server rate-limits (or rejects) requests that
would hit etcd. The `lightkube` client converts this 429 into `ApiError: storage is
(re)initializing`.

The UAT test `test_create_profile` is not resilient to transient etcd unavailability: it
lists PodDefaults immediately after a 10-second sleep and raises on the first 429. There is
no retry logic.

Secondary effect: profile deletion is also stalled by the etcd disruption — the Profile
object's finalizers (handled by `kubeflow-profiles` controller) cannot reach etcd to
complete deletion. After ~80 seconds the `assert_profile_deleted` timeout fires.

**Evidence to look for:**
- `4-commands[1].log`: `HTTP 429 Too Many Requests` for a PodDefaults list/get
- `4-commands[1].log`: `etcdserver: request timed out, possibly due to previous leader failure` in the Juju error log section (printed during model status at teardown)
- `4-commands[1].log`: `LeaderElectedEvent` or `leader_elected` hooks firing (Juju leadership re-election, correlates with cluster disruption)
- `juju_status_foundations-kubernetes_kubeflow.json`: All applications `active` at teardown — confirms the etcd disruption was **transient** and recovered by the time of log collection

**Note on timing:** The `etcdserver: request timed out` message in the Juju error log carries
Juju model-internal timestamps (e.g. `11:39:18`), not UTC wall-clock times. These may appear
to predate the test by 20–40 minutes — this is expected, as the Juju agent error occurs when
`kfp-schedwf` tried to apply a resource and etcd was unavailable at that moment. The test
failure at ~12:16 UTC is consistent with a cluster that was intermittently unstable.

**Observed in:** run 24122089114 (UUID 40826399-eabc-43ea-846c-f4d4f795740f,
tor3-sqa-dedicated_maas dh1_j2, branch main, UATS track/1.10, 2026-04-08)

---

### Pattern 2: mlflow-server nodePort Conflict — HTTP 503 on all MLflow API calls

**Observed in:** run 24122085937 (UUID a6794ed1-aad3-4186-a4c6-14af4c9c761a,
ext-sqa-aks useast, branch main, UATS track/1.10, 2026-04-08)

**Symptom (GitHub Actions log / `4-commands[1].log`):**
```
FAILED test_notebooks.py::test_notebook[mlflow-integration]
  - MlflowException: API request to http://mlflow-server.kubeflow.svc.cluster.local:5000/api/2.0/mlflow/experiments/get-by-name
    failed with exception HTTPConnectionPool(...): Max retries exceeded
    (Caused by ResponseError('too many 503 error responses'))

FAILED test_notebooks.py::test_notebook[mlflow-kserve]
  - (same 503 error, different experiment name)

FAILED test_notebooks.py::test_notebook[e2e-wine-kfp-mlflow-kserve]
  - AssertionError: KFP run is in RUNNING state.
```
**Juju error log (in `4-commands[1].log`, repeated every ~4–5 min from 07:19 onward):**
```
unit-mlflow-server-0: 07:19:24 ERROR unit.mlflow-server/0.juju-log
  Kubernetes service patch failed: Service "mlflow-server" is invalid:
  spec.ports[0].nodePort: Invalid value: 31380: provided port is already allocated
```

**Root cause:**

The `mlflow-server` charm (v2.22/stable) repeatedly patches its Kubernetes Service
to assign nodePort `31380`. On the AKS cluster used for this run, nodePort `31380`
was already allocated by another service. The patch fails on every hook cycle
(~every 4–5 minutes) throughout the entire run, starting before tests begin.

Although the Juju unit ultimately settles to `active idle` (the charm does not treat
the patch failure as fatal), the persistent hook failures prevent the charm from
completing its configuration loop — in particular, `mlflow-server` never publishes
its SDI version data in the `object-storage` relation, so `mlflow-minio` cannot
configure its bucket. As a result, the `mlflow-server` application returns HTTP 503
to all callers.

The `e2e-wine-kfp-mlflow-kserve` failure is a cascade: the KFP pipeline contains a
step that contacts mlflow-server; when that step fails (503), the KFP run stalls in
`RUNNING` state until the notebook test assertion times out.

**Distinguishing features:**
- Only MLflow-specific notebooks fail; katib, kfp-v2, kserve, training notebooks all pass
- `mlflow-minio-integration` passes (connects to MinIO directly, not to mlflow-server)
- The nodePort error in the Juju log repeats on a regular ~4–5 min interval
- Juju shows `mlflow-server active idle` at teardown (misleading — charm is "active"
  but application is 503)

**Evidence to look for:**
- `4-commands[1].log`: `Kubernetes service patch failed: spec.ports[0].nodePort: Invalid value: 31380: provided port is already allocated` repeating every few minutes
- `4-commands[1].log`: `serialized_data_interface.errors.UnversionedRelation: versions not found for apps: mlflow-server` (mlflow-minio can't get mlflow-server's relation data)
- Test failures exclusively on notebooks that call `mlflow-server.kubeflow.svc.cluster.local:5000`

**Substrate note:** Applies to `ext-sqa-aks` (and potentially other AKS-backed
substrates). Not applicable to MAAS substrates.

---

## Notes

- The step uses `timeout 4h tox -e ${TEST_OPTION}` — exit code 124 means the 4-hour timeout fired; exit code 123 means tox reported a test failure.
- For `track/1.10` the tox env name is `kubeflow-mlflow-remote` (not `kubeflow-remote`) when the SKU is `aks_kubeflow_mlflow`; other SKUs may differ.
- The `test_kubeflow_workloads` test depends on `test_create_profile` via `pytest-dependency`; if `test_create_profile` fails, `test_kubeflow_workloads` is automatically skipped.
- Juju error logs shown in the pytest output are collected from the model at teardown; they reflect the state of charm units *at the moment of collection*, not necessarily at the moment of test failure.
- For the `kubeflow-mlflow-remote` tox env, `test_kubeflow_workloads` launches a Kubernetes Job that runs the notebook suite inside a pod (Python 3.11). Job logs are fetched and embedded in `4-commands[1].log` — the inner pytest session results appear from approximately line 379 onward.

## Version History

- **v1.0** (2026-04-08): Initial version — etcd leader election / `storage is (re)initializing` pattern from run 24122089114 (UUID 40826399, tor3-sqa-dedicated_maas dh1_j2)
- **v1.1** (2026-04-09): Added Pattern B — mlflow-server nodePort 31380 conflict on AKS causes persistent 503; 3 MLflow notebooks fail; e2e-wine KFP run stalls as cascade; from run 24122085937 (UUID a6794ed1, ext-sqa-aks useast, main, track/1.10, 2026-04-08)
