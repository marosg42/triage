# Step Knowledge: sunbeam_upgrade_mysql_k8s

## Step Overview

Upgrades the `mysql-k8s` charm (application name `mysql`) in an existing Sunbeam deployment.
Driven by `products/sunbeam/upgrade_charm.py` in `sqa-cloud-deployment-pipeline`, executed over
SSH against the Sunbeam control node (e.g. `solqa-shared-maas-server-31.maas`).

Flow:
1. `juju models` / `juju status -m <owner>/openstack` to locate the app.
2. `juju info mysql-k8s` to resolve the target channel's revision.
3. `sunbeam manifest show latest` → copy locally → set `mysql-k8s.channel` → scp back.
4. `sunbeam cluster refresh -m <manifest>` (generic refresh).
5. Wait for model clean (up to 3600s, polling 30s).
6. `verify_channel(...)` then `verify_revision(...)` against fresh `juju status`.

Sibling steps `sunbeam_upgrade_mysql_router_k8s` and `sunbeam_upgrade_traefik_k8s` use the same script.

## Swift Artifacts

| Path | Description | When to check |
|---|---|---|
| `generated/github-runner/run.log` | Full runner log — contains every ssh command and the traceback | Always — first stop |
| `generated/sunbeam/manifest-<ts>-<charm>-original.yaml` | Manifest as read from clusterd before edit | Confirm pre-refresh channels |
| `generated/sunbeam/manifest-<ts>-<charm>.yaml` | Edited manifest actually applied | `diff` against original to confirm the edit |
| `generated/sunbeam/juju_status_openstack.txt` | Post-failure app table: channel + `charm-rev` columns | Always — shows channel/revision mismatch |
| `generated/sunbeam/juju_debug_log_openstack.txt` | Unit hook activity during the refresh window | Check which apps actually ran `upgrade-charm` |
| `generated/sunbeam/terraform.tfstate`, `generated.tf` | Terraform state for the control plane | See whether only the channel attribute changed |
| `generated/happy-bot/findings.md`, `bug_report.txt` | In-run AI analysis with on-cluster evidence (control node is gone afterwards) | Very useful — it had live cluster access |

`sunbeam_upgrade_*` runs are typically on `tor3-sqa-shared_maas` → **no MAAS logs**; skip the
tgz-extraction steps.

## Grep Patterns

```bash
UUID=<uuid>; D=files/$UUID/generated

# The traceback / assertion
grep -n "was not refreshed to revision\|Traceback" $D/github-runner/run.log

# What the step decided to do
grep -n "points to revision\|Refreshing\|Set .* to channel\|Refresh complete\|is clean" $D/github-runner/run.log

# Did the edit land?
diff $D/sunbeam/manifest-*-original.yaml $D/sunbeam/manifest-*[!l].yaml

# Actual post-state channel + revision
grep -E "^mysql\s" $D/sunbeam/juju_status_openstack.txt

# Which units genuinely upgraded
grep -oiP "unit-[a-z0-9-]+-\d+: .*upgrade-charm" $D/sunbeam/juju_debug_log_openstack.txt \
  | grep -oP "unit-[a-z0-9-]+-\d" | sort -u
```

## Known Failure Patterns

### Pattern 1: Generic `sunbeam cluster refresh` silently skips infra apps

**Symptom:**
```
Exception: mysql-k8s was not refreshed to revision 431: mysql runs revision 423
  File ".../products/sunbeam/upgrade_charm.py", line 401, in verify_revision
```
The refresh reports "Refresh complete." and the model goes clean — only the revision check fails.

**Root cause:** Sunbeam's intra-channel upgrade excludes infra apps
(`INFRA_APPS = ["mysql-k8s", "vault-k8s", "k8s"]` in `sunbeam/steps/upgrades/intra_channel.py`,
comment: *"Skip infra apps, they are refreshed via `sunbeam cluster refresh <app>`"*). The
pipeline invokes the **generic** `sunbeam cluster refresh -m <manifest>` instead of the dedicated
`sunbeam cluster refresh mysql -m <manifest>` (`MySQLInChannelUpgradeCoordinator` →
`MySQLCharmUpgradeStep` + `ReapplyMySQLTerraformPlanStep`). No `juju refresh mysql --channel ...`
is ever issued; the Control-plane Terraform apply only updates the application's channel
attribute in place, so the app ends up on the new channel with the **old revision**.
`verify_channel` passes, `verify_revision` fails.

**Evidence to look for:**
- `juju_status_openstack.txt`: `mysql ... mysql-k8s  8.0/candidate  423` (new channel, old rev).
- `juju_debug_log_openstack.txt`: `upgrade-charm` hooks for other apps (e.g. `horizon/0-2`) but
  none for `mysql/*`; mysql units only ran `update-status` during the refresh window.
- Terraform apply line `module.single-mysql[0].juju_application.mysql: update complete (2s)` —
  far too fast for a real charm upgrade.
- Manifest diff shows the channel edit was correct, so the bug is in the *mechanism*, not the input.
- Cloud otherwise healthy: model clean, mysql active with a Primary, all non-infra charms refreshed.

**Distinguishing note:** if instead *both* channel and revision are unchanged, suspect the
manifest edit/scp failing or clusterd rejecting the manifest — check the manifest diff first.

---

_Add more patterns below as they are discovered._

## Notes

- This is a **pipeline-side** defect, not an infrastructure failure — expect a clean, healthy cloud
  in all artifacts. Do not go hunting in sosreports.
- The same class of bug applies to `vault-k8s` and `k8s`, so a `sunbeam_upgrade_vault_k8s` /
  `k8s` layer would fail identically; `mysql-router-k8s` and `traefik-k8s` are *not* infra apps
  and refresh fine through the generic path.
- The step can burn a long time before failing (refresh ~8 min + model-clean wait) — the failure
  timestamp is well after the refresh actually stopped doing anything.
