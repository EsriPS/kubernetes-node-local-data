# Option 1 operational sequence — task 4.7

The one remaining mutating step of the fix: `helm upgrade` to the chart version that no longer renders
`geodata-local-sc` and `geodata-local-pv`, followed by the prescribed PV absence check and the end-state
verification.

**[MUTATES — OPERATOR CONFIRMATION REQUIRED]** — confirmation recorded in
[Operator confirmation](#operator-confirmation).

_Requirements: 2.2, 2.3, 2.6, 2.7, 2.9, 3.1_

## Run context

| Item | Value |
|---|---|
| Timestamp (UTC) | 2026-09-03T18:32:42Z (stage 1) → 2026-09-03T18:42:44Z (stage 4) |
| Working directory | `/Users/cars3788/Development/carstenpiepel/kubernetes-node-local-data` |
| Git HEAD | `1e915d4` |
| Helm | `v4.2.4+g3900f43` (KubeClientVersion v1.36) |
| kubectl | client `v1.32.4-dispatcher`, server `v1.34.9-eks-bca9cf6` |
| Cluster | `arn:aws:eks:us-west-2:<account-id>:cluster/<cluster>` |
| Python / pytest | `3.14.7` (`.venv/bin/python`) / `8.3.5` |
| Chart version applied | `0.2.0` (`charts/node-local-data/Chart.yaml` line 5), appVersion `0.5.0` |
| Values file | `my-values.yaml` — the live operator values file, matching the release's user-supplied values |

Captured artifacts under `/tmp/nld-47/`: `stored-manifest.yaml`, `new-manifest.yaml`, `manifest.diff`,
`semantic.diff`, `upgrade.txt`, `stored-manifest-after.yaml`, and the before/after pod, node, and label
snapshots.

## Result summary

| # | Check | Result |
|---|---|---|
| 1 | Release identified: `node-local-data` in `default`, revision 3, chart `0.1.0` | PASS |
| 2 | Stored manifest still listed `geodata-local-sc` **and** `geodata-local-pv` | CONFIRMED |
| 3 | Live cluster: `geodata-local-pv` already absent, `geodata-local-sc` still present | CONFIRMED |
| 4 | No PV, PVC, or pod anywhere referenced `geodata-local*` | PASS |
| 5 | Dry-run delta is exactly two removals + NOTES text; zero objects changed | PASS |
| 6 | `helm upgrade` — revision 4, `deployed`, chart `0.2.0`, 5 s | PASS |
| 7 | `kubectl get pv geodata-local-pv` → `absent` (NotFound); no explicit delete needed | PASS |
| 8 | `geodata-local-sc` → NotFound | PASS |
| 9 | New stored manifest contains zero `geodata-local` references; 12 objects | PASS |
| 10 | Sync-agent, path-shim, warm-capacity pods: same names, same 0 restarts, no restart | PASS |
| 11 | All three workloads still at `generation: 1` / `observedGeneration: 1` | PASS |
| 12 | 37 ArcGIS pods on the `/mnt/geodata` hostPath: identical set, all Ready, 0 restarts | PASS |
| 13 | 35 ArcGIS Deployments consuming the hostPath: all Available, none under-replicated | PASS |
| 14 | Both pool nodes: `dataset-generation=g57a70ff5b429`, `dataset-count=3`, no `data-not-ready` taint | PASS |
| 15 | Neither NodeClaim carries a `Drifted` condition | PASS |
| 16 | All 8 nodes: `/mnt/geodata` = `system_u:object_r:local_t:s0`, mode 0755 root:root, read succeeds | PASS |
| 17 | Post-upgrade sync pass completed on **both** pool nodes, 0 bytes transferred | PASS |
| 18 | `.venv/bin/python -m pytest -q` → 93 passed, 1 skipped | PASS |
| 19 | `git diff --check` → no output, exit 0 | PASS |

**Expected outcome — the PV check returns `absent` and the upgrade removes only the StorageClass. Met.**

## Stage 1 — release state before any change

`helm list --all-namespaces` (chart rows only):

```
NAME             NAMESPACE  REVISION  STATUS    CHART                  APP VERSION
node-local-data  default    3         deployed  node-local-data-0.1.0  0.5.0
```

Release name `node-local-data`, namespace `default`. Revisions 1–3 all pre-date the rejected
shared-MCS-level session, so no revision on record ever carried `seLinuxOptions`.

### The stored manifest still carried both objects

`helm get manifest node-local-data -n default` — 530 lines, 14 objects, including:

```
# Source: node-local-data/templates/09-local-pv.yaml
kind: StorageClass
  name: geodata-local-sc
# Source: node-local-data/templates/09-local-pv.yaml
kind: PersistentVolume
  name: geodata-local-pv
```

Both were still in Helm's record of the release even though the PV object itself had been deleted by
hand during recovery. That divergence is exactly what this upgrade reconciles.

### Live resource inventory

| Resource | State before upgrade |
|---|---|
| `PersistentVolume/geodata-local-pv` | **ABSENT** — deleted by hand during recovery (F9) |
| `StorageClass/geodata-local-sc` | **PRESENT**, age 2d15h, `Retain`, `WaitForFirstConsumer` |
| `PersistentVolume/geodata-source-pv` | `Bound` to `geodata-sync/geodata-source-pvc`, 1536Gi RWX, `Retain` |
| `PersistentVolumeClaim/geodata-sync/geodata-source-pvc` | `Bound`, 1536Gi |
| `StorageClass/geodata-source-sc` | PRESENT |
| `DaemonSet/geodata-path-shim` | 8/8 ready |
| `DaemonSet/geodata-sync-agent` | 2/2 ready |
| `Deployment/geodata-warm-capacity` | 2/2 ready |
| `NodePool/geodata-nodes` | Ready, 2 nodes |
| `EC2NodeClass/geodata-nodes` | Ready |
| `PriorityClass/geodata-warm-capacity` | PRESENT |
| NodeClaims | `geodata-nodes-8pwk7` → `ip-10-0-37-12`, `geodata-nodes-qft4c` → `ip-10-0-39-129`, both Ready |

**No claim on the removed PV, and nothing left pointing at it.** A scan of every PV, PVC, and pod in the
cluster for the string `geodata-local` returned no hits. All 37 ArcGIS pods that mount `/mnt/geodata`
do so as a `hostPath` volume; the only PVC any of them carries is `arcgis/ontap-gisdata-pvc`, which is
unrelated to this chart. This is the direct confirmation that the deregistration and PVC deletion
recorded as already-complete really did land, and therefore that deleting the PV object could not leave
a claim unbound.

### Pod baseline

`geodata-sync` namespace — 12 pods, every one `1/1 Running` with **0 restarts**:

| Pod | Node | Age |
|---|---|---|
| `geodata-path-shim-2s52b` | `ip-10-0-37-244` | 2d17h |
| `geodata-path-shim-856v2` | `ip-10-0-35-13` | 2d17h |
| `geodata-path-shim-rwprs` | `ip-10-0-37-12` | 2d17h |
| `geodata-path-shim-sjwqd` | `ip-10-0-39-88` | 2d17h |
| `geodata-path-shim-skbnh` | `ip-10-0-39-129` | 28h |
| `geodata-path-shim-vs2qh` | `ip-10-0-34-202` | 2d17h |
| `geodata-path-shim-zdm52` | `ip-10-0-33-215` | 2d14h |
| `geodata-path-shim-zfh82` | `ip-10-0-33-6` | 2d17h |
| `geodata-sync-agent-mdjd8` | `ip-10-0-37-12` | 2d16h |
| `geodata-sync-agent-2285m` | `ip-10-0-39-129` | 28h |
| `geodata-warm-capacity-56d4cf85fd-8mhf6` | `ip-10-0-37-12` | 2d17h |
| `geodata-warm-capacity-56d4cf85fd-57qq6` | `ip-10-0-39-129` | 28h |

ArcGIS side: 37 pods carry a `/mnt/geodata` hostPath volume, spread over 7 nodes, all fully Ready with
0 restarts. Five are `geocodeserver` and three are `mapserver`; the remainder are gp/sync/utility
servers that also read the mirror.

Pool node labels and taints:

```
ip-10-0-37-12   dataset-count=3  dataset-generation=g57a70ff5b429  workload=geodata  taint dedicated=geodata:NoSchedule  Ready=True
ip-10-0-39-129  dataset-count=3  dataset-generation=g57a70ff5b429  workload=geodata  taint dedicated=geodata:NoSchedule  Ready=True
```

`data-not-ready` is absent on both. The only taint is the dedicated taint, which is intended.

### Values file confirmation

`helm get values` for revision 3 carried `localVolume: {capacity: 1536Gi, enabled: true}`.
`my-values.yaml` no longer carries a `localVolume` block, so passing it with `-f` (and **not**
`--reuse-values`) drops those keys instead of feeding them back into the new chart, which would have
tripped the task 4.2 legacy-key rejection. Every other key in `my-values.yaml` matches revision 3's
user-supplied values exactly.

## Stage 2 — dry run and the delta

```
helm upgrade node-local-data charts/node-local-data -n default -f my-values.yaml --dry-run
```

Exit 0, `STATUS: pending-upgrade`, `REVISION: 4`, `DESCRIPTION: Dry run complete`. The only warning is
that `--dry-run` is deprecated in favor of `--dry-run=client`.

The `helm diff` plugin is not installed (`helm plugin list` is empty), so the delta was taken manually:
the rendered manifest was extracted from the dry-run output and compared against `helm get manifest`
both textually and semantically.

### Textual diff

91 lines. Two substantive hunks, both pure deletions:

```diff
-# Source: node-local-data/templates/09-local-pv.yaml
-kind: StorageClass
-  name: geodata-local-sc
-provisioner: kubernetes.io/no-provisioner
-volumeBindingMode: WaitForFirstConsumer
-reclaimPolicy: Retain
```

```diff
-# Source: node-local-data/templates/09-local-pv.yaml
-kind: PersistentVolume
-  name: geodata-local-pv
-  ...
-  storageClassName: geodata-local-sc
-  local:
-    path: "/mnt/geodata"
```

The `local.path: "/mnt/geodata"` line in that second hunk is the defect itself leaving the chart: the
relabeling mount pointed at the same directory the routing `hostPath` store registers.

The remaining five hunks are blank lines only — four added before a `---` separator and two removed at
end of file. They are an artifact of how Helm serializes a stored manifest versus how it prints one in
dry-run output, not a content change, which the semantic comparison below establishes.

### Semantic diff — the load-bearing check

Both manifests parsed into objects keyed by apiVersion/kind/namespace/name and compared field by field:

```
REMOVED objects (2):
   storage.k8s.io/v1/StorageClass/geodata-local-sc
   v1/PersistentVolume/geodata-local-pv
ADDED objects (0):
CHANGED objects (0):
UNCHANGED objects: 12
   = apps/v1/DaemonSet/geodata-sync/geodata-path-shim
   = apps/v1/DaemonSet/geodata-sync/geodata-sync-agent
   = apps/v1/Deployment/geodata-sync/geodata-warm-capacity
   = karpenter.k8s.aws/v1/EC2NodeClass/geodata-nodes
   = karpenter.sh/v1/NodePool/geodata-nodes
   = rbac.authorization.k8s.io/v1/ClusterRole/geodata-sync-agent
   = rbac.authorization.k8s.io/v1/ClusterRoleBinding/geodata-sync-agent
   = scheduling.k8s.io/v1/PriorityClass/geodata-warm-capacity
   = storage.k8s.io/v1/StorageClass/geodata-source-sc
   = v1/PersistentVolume/geodata-source-pv
   = v1/PersistentVolumeClaim/geodata-sync/geodata-source-pvc
   = v1/ServiceAccount/geodata-sync/geodata-sync-agent
```

**Zero changed objects** is what made this safe to apply:

- Neither DaemonSet pod template differs, so **no pod is recreated** (3.3, 3.5).
- `NodePool` and `EC2NodeClass` are byte-identical, so **no NodeClaim can be marked Drifted** and no node
  can lose its mirror as a side effect (3.7, 3.9).
- `geodata-source-pv` and `geodata-source-pvc` are untouched, so the NFS source binding is not disturbed.
- `reclaimPolicy: Retain` on the removed PV means deleting the object touches no node filesystem content;
  the mirror is not deleted, moved, or re-synced (3.1).

### NOTES delta

The one intended non-resource change. Removed: the whole "Local-volume registration values for ArcGIS
Enterprise Manager" block naming `geodata-local-sc`, `geodata-local-pv`, `ReadOnlyMany`, and `1536Gi`,
plus the two-stage registration paragraph. Added: a single "Shared hostPath registration for ArcGIS"
block that registers one folder data store at `/mnt/geodata` for both routing and geocoding, states that
a hostPath store is never relabeled, and warns that such a store resolves to an empty directory rather
than failing to mount if published before the first sync completes. Neither the old nor the new notes
mention an SELinux level, and the new notes direct no action at ArcGIS workloads (2.7, 2.10, 2.11).

**Delta verdict: clean.** Exactly (a) removal of `geodata-local-sc` and `geodata-local-pv`, (b) NOTES
text, (c) the chart version metadata `0.1.0` → `0.2.0`. Nothing recreates the local PV, nothing restarts
a DaemonSet or Deployment pod, nothing touches the source NFS PV or PVC.

## Operator confirmation

The user explicitly authorized this live-cluster upgrade before stage 3, on the condition that it be run
in the staged order above and stopped if the stage 2 delta showed anything beyond the two removals and
the NOTES text. The delta was clean, so the upgrade proceeded.

`--rollback-on-failure` (Helm 4's replacement for `--atomic`) was deliberately **not** used. On failure
it rolls the release back to revision 3, which would **recreate `geodata-local-pv` and
`geodata-local-sc`** — reinstating the only object in the chart able to relabel the shared tree, which is
the opposite of the intended end state. Since the upgrade changes no workload, its failure modes are API
errors on two deletions rather than a partially rolled-out workload, so automatic rollback buys nothing
and risks reintroducing the defect. `--wait` (watcher strategy) was used instead: it confirms every
workload is still ready without arming a rollback.

## Stage 3 — the upgrade

```
helm upgrade node-local-data charts/node-local-data -n default -f my-values.yaml --wait
```

Started 2026-09-03T18:36:33Z, returned 18:36:38Z — **5 seconds**, consistent with two deletions and no
workload rollout.

```
Release "node-local-data" has been upgraded. Happy Helming!
NAME: node-local-data
NAMESPACE: default
STATUS: deployed
REVISION: 4
DESCRIPTION: Upgrade complete
```

`helm history`:

```
REVISION  UPDATED                   STATUS      CHART                  DESCRIPTION
1         Mon Aug 31 21:30:00 2026  superseded  node-local-data-0.1.0  Install complete
2         Mon Aug 31 21:34:49 2026  superseded  node-local-data-0.1.0  Upgrade complete
3         Mon Aug 31 23:25:31 2026  superseded  node-local-data-0.1.0  Upgrade complete
4         Thu Sep  3 14:36:34 2026  deployed    node-local-data-0.2.0  Upgrade complete
```

`helm list -n default` reports chart `node-local-data-0.2.0`, status `deployed`. The release's
user-supplied values no longer contain a `localVolume` block.

## Stage 4 — verification

### The prescribed PV absence check

```
$ kubectl get pv geodata-local-pv -o jsonpath='{.status.phase}{"\n"}' 2>/dev/null || echo absent
absent

$ kubectl get pv geodata-local-pv
Error from server (NotFound): persistentvolumes "geodata-local-pv" not found
```

`absent`, as the task entry predicted, because the PV had already been removed by hand. **`kubectl delete
pv geodata-local-pv` was therefore not run** — the conditional in the task entry applies only if the PV
survived the upgrade, and it did not. No surviving `Released` PV with a stale `claimRef`, so the sequence
holds.

The StorageClass, which *was* still live, is now gone:

```
$ kubectl get storageclass geodata-local-sc
Error from server (NotFound): storageclasses.storage.k8s.io "geodata-local-sc" not found
```

`geodata-source-sc` is the only `geodata` StorageClass left.

### Nothing references the local PV or SC

The new stored manifest (`helm get manifest`, 12 objects) contains **zero** occurrences of
`geodata-local`. A scan of every PV, PVC, StorageClass, DaemonSet, Deployment, StatefulSet, and pod in
every namespace for the string `geodata-local` returns `NONE`.

**The bug condition is now unrepresentable in the cluster, not merely unrepresented in the chart**: no
object exists that could create a relabeling mount over `/mnt/geodata`, and the chart can no longer
render one.

### No unexpected restarts

The `geodata-sync` pod list after the upgrade is identical to the stage 1 baseline — same 12 pod names,
same nodes, all `1/1 Running`, all **0 restarts**. The single difference in the whole comparison is
`geodata-sync-agent-mdjd8` displaying age `2d17h` instead of `2d16h`, which is the age counter advancing
during the run, on the same pod name with the same restart count.

Controller-side confirmation that no rollout was even attempted:

| Workload | generation | observedGeneration | desired/ready |
|---|---|---|---|
| `DaemonSet/geodata-path-shim` | 1 | 1 | 8/8, 8 updated, 8 available |
| `DaemonSet/geodata-sync-agent` | 1 | 1 | 2/2, 2 updated, 2 available |
| `Deployment/geodata-warm-capacity` | 1 | 1 | 2 ready |

All three still at `generation: 1`, unchanged since the original install. A pod-template edit would have
incremented it.

### ArcGIS consumers still healthy

The set of 37 ArcGIS pods mounting `/mnt/geodata` is **identical to the baseline** by name and node.
All are fully Ready and **none has any restart**. The 35 Deployments behind them are all `Available=True`
and `Progressing=True` with no under-replication — so routing map services and the geocode servers both
still pass readiness, and neither was disturbed (2.3, 2.7, 3.4).

### Node readiness contract intact

```
ip-10-0-37-12   dataset-count=3  dataset-generation=g57a70ff5b429  workload=geodata  taint dedicated=geodata:NoSchedule  Ready=True
ip-10-0-39-129  dataset-count=3  dataset-generation=g57a70ff5b429  workload=geodata  taint dedicated=geodata:NoSchedule  Ready=True
```

Both labels still populated at the same generation digest and count as the stage 1 baseline, and
`data-not-ready` is still absent. Neither NodeClaim carries a `Drifted` condition (3.7, 3.9).

### A sync pass completed on both nodes after the upgrade

The upgrade landed at 18:36:38Z. Both agents ran a pass afterwards:

```
ip-10-0-37-12   18:37:00 Syncing source directly to /mnt/geodata: 8 transfers x 8 streams
                18:37:01 Pass complete: direct mirror has 3 dataset(s), 0 bytes transferred,
                         901830754662 bytes on disk, 0.1s total
ip-10-0-39-129  18:39:33 Syncing source directly to /mnt/geodata: 8 transfers x 8 streams
                18:39:33 Pass complete: direct mirror has 3 dataset(s), 0 bytes transferred,
                         901830754662 bytes on disk, 0.1s total
```

3 datasets and **0 bytes transferred** on both nodes, with ~840 GiB already on disk. No `PermissionError`
and no rclone IO error (2.5). The zero-byte transfer is the direct evidence that the mirror was not
deleted, relocated, or re-synced by this task (3.1).

### SELinux label still category-free on every node

Read-only `ls -ldZ` through the **existing** `geodata-path-shim` pod on each node — no privileged debug
pod was created:

| Node | `/mnt/geodata` label | Mode / owner | Read |
|---|---|---|---|
| `ip-10-0-33-6` | `system_u:object_r:local_t:s0` | `drwxr-xr-x. root root` | READ_OK |
| `ip-10-0-33-215` | `system_u:object_r:local_t:s0` | `drwxr-xr-x. root root` | READ_OK |
| `ip-10-0-34-202` | `system_u:object_r:local_t:s0` | `drwxr-xr-x. root root` | READ_OK |
| `ip-10-0-35-13` | `system_u:object_r:local_t:s0` | `drwxr-xr-x. root root` | READ_OK |
| `ip-10-0-37-12` | `system_u:object_r:local_t:s0` | `drwxr-xr-x. root root` | READ_OK |
| `ip-10-0-37-244` | `system_u:object_r:local_t:s0` | `drwxr-xr-x. root root` | READ_OK |
| `ip-10-0-39-88` | `system_u:object_r:local_t:s0` | `drwxr-xr-x. root root` | READ_OK |
| `ip-10-0-39-129` | `system_u:object_r:local_t:s0` | `drwxr-xr-x. root root` | READ_OK |

All 8 nodes, **no MCS categories anywhere**. Dataset children on both pool nodes carry the same label:

```
ip-10-0-37-12:
  drwxr-xr-x. 5 root root       system_u:object_r:local_t:s0   80 Sep  1 01:51 /mnt/geodata
  drwxr-xr-x. 3 4294967294 1000 system_u:object_r:local_t:s0   28 Aug  7 14:00 /mnt/geodata/HERE2026Q2
  drwxr-xr-x. 3 4294967294 1000 system_u:object_r:local_t:s0   20 Aug 20 19:53 /mnt/geodata/HERE_Routing_Quarterly_MGDB
  drwxr-xr-x. 2 4294967294 1000 system_u:object_r:local_t:s0 4096 Sep  3 00:43 /mnt/geodata/Locators_HGDS
ip-10-0-39-129: identical labels on the same four paths
```

`Locators_HGDS` is the geocoding dataset. It carries the plain reference label with no categories while
geocode servers on both pool nodes are actively Ready against it — which is the observable difference
Option 1 makes. Under the old arrangement that subtree was `data_t:s0:c20,c476`, specific to one pod
instance. Serving it through the non-relabeling `hostPath` store leaves it category-free.

## Repository validation after the upgrade

| Command | Result |
|---|---|
| `.venv/bin/python -m pytest -q` | **93 passed, 1 skipped** in 5.29 s |
| `git diff --check` | no output, exit 0 |

Matches the expected state, so nothing in the repository drifted as a result of the operational work.

## Scope limits of this evidence

- **The geocode service was verified at the Kubernetes level, not the ArcGIS service level.** Readiness
  probes passing on all five geocode servers, Deployment `Available=True`, zero restarts, and a
  category-free `Locators_HGDS` are the signals available without credentials. A REST query against the
  published geocode endpoint was not made.
- **The label check is a point-in-time observation.** It is strong because the mechanism is now absent by
  construction — no object in the cluster can create a relabeling mount over the tree — rather than
  because repeated sampling found nothing.
- **The overlap guard added in task 4.3 is lexical and inert today.** The relabeling-mount set is empty,
  so it rejected nothing during this upgrade. It is a forward guard for a future relabeling volume, and
  it cannot see symlinks, bind mounts, or hard links.
- **Nothing here proves the relabel is fixed in the general case.** It records that the object able to
  cause it is gone from both the chart and the cluster, and that every consumer is healthy after the
  removal.

## Conclusion

Release `node-local-data` in `default` is at chart `0.2.0`, revision 4, `deployed`. The upgrade removed
`StorageClass/geodata-local-sc` and reconciled Helm's record of `PersistentVolume/geodata-local-pv`, which
recovery had already deleted; the prescribed absence check returned `absent`, so no explicit delete was
needed. Nothing else in the release changed: zero objects differed in the dry run, all three workloads
remain at `generation: 1`, no pod restarted, and no NodeClaim drifted. Both pool nodes completed a sync
pass afterwards transferring 0 bytes, all 37 ArcGIS hostPath consumers stayed Ready with no restarts,
node readiness labels and taints are unchanged, and `/mnt/geodata` is `system_u:object_r:local_t:s0` with
a successful read on all 8 nodes.

The mirror was not deleted, moved, or re-synced. No node, NodeClaim, or data was removed. No ArcGIS pod
spec was modified and no `seLinuxOptions` was injected anywhere.
