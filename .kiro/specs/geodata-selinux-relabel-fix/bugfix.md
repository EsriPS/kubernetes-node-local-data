# Bugfix Requirements Document

## Introduction

Publishing an ArcGIS geocode server that consumes the geocoding **local PersistentVolume** made
`/mnt/geodata` unreadable to every other consumer on the affected node. Routing map services (ArcGIS
**hostPath** folder data store), the Path Shim DaemonSet, and the sync agent DaemonSet all receive
`Permission denied` against a directory that is `drwxr-xr-x` 0755 root:root.

The mechanism is a volume-plugin asymmetry under enforcing SELinux on Bottlerocket. Each pod receives
a random MCS category pair. The in-tree `hostPath` plugin performs no SELinux relabeling, so routing
consumers never altered the directory label. The `local` plugin does relabel, so when the geocoding
pod mounted the local PV the runtime recursively relabeled the tree to that pod's categories
(`system_u:object_r:data_t:s0:c20,c476` on `ip-10-0-37-101`). Every consumer whose categories differ
is then denied; Unix mode bits do not override an MCS mismatch.

The latent defect is a path collapse introduced by the Helm conversion: `datasetRoot` = `/mnt/geodata`
is simultaneously the Bottlerocket instance-store bind target, the sync agent's write destination, the
Path Shim's path, the routing hostPath registration path, and the `local` PV path. Pre-Helm manifests
published a child directory (`/mnt/geodata/active`) while the agent wrote through `staging/` and
renamed into place, so no single directory carried both mount types. Publishing the geocode server is
the trigger, not the cause; no local-PV consumer had previously existed to relabel the tree.

Impact: routing deployments exceed their progress deadline with readiness 503, the sync agent is in
`CrashLoopBackOff` (269 restarts) so no mirror pass completes and node readiness taints and labels are
not reconciled, and the condition recurs on every geocoding pod restart or republication because
categories are reassigned each time.

Hard constraint: no fix may modify ArcGIS pods. Injecting `seLinuxOptions` into ArcGIS workloads, an
admission mutating webhook over the ArcGIS namespace, pinning a shared MCS level across ArcGIS pods,
disabling SELinux, `chmod 777`, and privileged consumers are all ruled out. The fix must live in this
project's own resources, paths, and documentation. A previous session shipped repository-only
shared-MCS-level changes that violate this constraint; they were never applied to the live cluster and
must be reverted.

Central tension the design must resolve: a `local` PV is recursively relabeled to the categories of
whichever pod currently mounts it, and those categories change on every new pod. Relocating the local
PV to a sibling directory therefore relocates the defect rather than removing it — whatever subtree the
local PV points at becomes unreadable and unwritable to the non-privileged, root-running sync agent,
and changes again on each geocoding restart. The requirements below constrain the ownership model for
geocoding data, not only its path.

## Bug Analysis

### Current Behavior (Defect)

What currently happens when the bug is triggered.

1.1 WHEN `localVolume.enabled` is true and the `local` PV path is the same directory that is shared to
ArcGIS through a non-relabeling `hostPath` registration THEN the chart renders and installs without
error or warning, permitting a relabeling and a non-relabeling mount type over one directory.

1.2 WHEN a geocoding pod mounts the `local` PV backed by `datasetRoot` on a node THEN the container
runtime recursively relabels that directory tree to the pod's randomly assigned MCS category pair, so
the on-disk label becomes specific to one pod instance.

1.3 WHEN an ArcGIS routing consumer reading `datasetRoot` through the `hostPath` folder data store runs
on a node whose tree has been relabeled by a geocoding pod THEN the system denies directory access with
`Permission denied` despite mode 0755, its readiness probe returns 503, and its deployment exceeds the
progress deadline.

1.4 WHEN the `geodata-path-shim` pod runs on an affected node THEN the system denies its `mkdir` and
`ls` of `datasetRoot` with `Permission denied` even though the container runs as uid 0.

1.5 WHEN the `geodata-sync-agent` DaemonSet pod runs on an affected node THEN rclone reports
`lstat /mnt/geodata/<dataset>: permission denied` for every dataset and refuses directory deletion due
to IO errors, `list_datasets()` in `agent/sync_agent.py` raises `PermissionError: [Errno 13]`, and the
pod enters `CrashLoopBackOff`, so no sync pass completes and node readiness taints and labels are never
reconciled.

1.6 WHEN a geocoding pod restarts or the geocode service is republished THEN the system relabels the
tree again with a newly assigned category pair, so denial recurs and any label repair performed in the
meantime is silently undone.

1.7 WHEN an operator inspects the affected node to diagnose the failure THEN the project provides no
documented, supported way to observe the relabel and the denial as first-hand evidence, so the cause
must be inferred from symptoms.

1.8 WHEN an operator consults `README.md`, `tuning/gotchas.md`, or the chart install notes THEN the
documentation attributes the denial to consumers leaving `seLinuxOptions` unset and prescribes one
shared MCS level across all consumers including ArcGIS workloads, which misidentifies the mechanism as
symmetric across volume types and prescribes an action that is not permitted in this environment.

1.9 WHEN the chart is rendered THEN it requires `selinux.sharedLevel`, validates its categories, and
injects pod-level `seLinuxOptions` into the sync agent and Path Shim, none of which prevents a
local-PV relabel, while making the chart's own pods depend on a value only ArcGIS-side changes could
honor.

1.10 WHEN an operator needs to restore service on a node that is already relabeled THEN the project
offers no recovery procedure that works without patching ArcGIS controllers, and no guidance on
ordering, blast radius, or reversibility of a label repair.

### Expected Behavior (Correct)

What should happen instead.

2.1 WHEN chart values place a relabeling volume path (`local` PV) on a path that equals or overlaps a
path shared through a non-relabeling `hostPath` registration THEN chart validation SHALL fail rendering
with a message naming both paths, so the overlap cannot be reintroduced by configuration.

2.2 WHEN geocoding data is served to its ArcGIS consumer THEN the system SHALL use an ownership model
in which no consumer mount can relabel a directory that other consumers or the sync agent depend on.
The design SHALL evaluate, and select from, at least these candidates without pre-committing to any:
(a) drop the `local` PV and register geocoding against the same non-relabeling `hostPath` folder data
store that routing uses; (b) keep the `local` PV on a dedicated path outside the routing tree and solve
agent write access to the relabeled subtree without touching ArcGIS, including an explicitly stated MCS
exemption for this project's own DaemonSet and its security tradeoff; (c) keep the `local` PV but
remove that subtree from agent management, with a defined out-of-band population and refresh procedure;
(d) reintroduce distinct publication child directories so the physical bind root, the agent write root,
and each consumer-facing path are separate concerns again. Each candidate SHALL be judged against
2.3 through 2.7 and 3.1 through 3.6.

2.3 WHEN a geocoding consumer is running, restarting, or being republished THEN every ArcGIS routing
consumer reading through the `hostPath` folder data store SHALL continue to read `datasetRoot`
successfully and SHALL pass its readiness probe.

2.4 WHEN the Path Shim pod runs on any node in the pool THEN it SHALL create and stat its configured
path successfully.

2.5 WHEN the sync agent pod runs on any node in the pool THEN it SHALL enumerate datasets, complete a
sync pass, and reconcile node readiness taints and labels without `PermissionError` or rclone IO
errors.

2.6 WHEN a geocoding pod is restarted or republished any number of times, and when pool nodes are
replaced independently of one another THEN 2.3 through 2.5 SHALL continue to hold without operator
intervention.

2.7 WHEN the fix is applied THEN it SHALL require no change to any ArcGIS pod specification, no
admission mutating webhook over the ArcGIS namespace, no shared or pinned MCS level for ArcGIS pods,
no relaxation of SELinux enforcement, no `chmod 777`, and no privileged ArcGIS consumer.

2.8 WHEN the cause is established THEN it SHALL be established from captured first-hand evidence rather
than inference, specifically: the geocodeserver pod's `spec.volumes` recorded to confirm local-PV
consumption via a PVC, with PVC, PV (`geodata-local-pv`), StorageClass (`geodata-local-sc`) names and
bind state; kernel AVC denials naming `/mnt/geodata` captured from the Bottlerocket host journal via
the admin container or `sudo sheltie` with `journalctl -k`, correlated by source and target context;
`ls -ldZ` on the tree root and each dataset child to determine whether the relabel is recursive or
confined to some inodes; a label comparison across every node in the pool, since each node relabels
independently; confirmation on this exact Kubernetes v1.34 / containerd 2.2.5 / Bottlerocket 1.64.0
combination of which volume plugins relabel; and a determination of whether a subsequent successful
sync pass repairs, partially repairs, or re-poisons labels, given that new inodes inherit the parent
directory label.

2.9 WHEN an operator needs to restore service on an already-affected node THEN the project SHALL provide
a recovery procedure that restores a usable label and unblocks routing and the sync agent, states its
blast radius and reversibility, states whether and in what order the geocoding data store must be
deregistered first to prevent immediate re-poisoning, and requires explicit operator confirmation
before execution because it changes node filesystem state.

2.10 WHEN an operator consults `README.md` and `tuning/gotchas.md` THEN the documentation SHALL explain
the volume-type asymmetry — `hostPath` is not relabeled, `local` PV is — as the durable lesson, and
SHALL replace the prior shared-MCS-level guidance rather than sit alongside it.

2.11 WHEN the repository is inspected after the fix THEN the previous session's rejected shared-level
changes SHALL be fully removed: the `selinux.sharedLevel` block and comment in
`charts/node-local-data/values.yaml`; the `selinux` property and its `required` entry in
`charts/node-local-data/values.schema.json`; the distinct-MCS-category check in
`charts/node-local-data/templates/_validate.tpl`; the pod-level `securityContext.seLinuxOptions` blocks
and comments in `charts/node-local-data/templates/07-sync-agent-daemonset.yaml` and
`charts/node-local-data/templates/08-path-shim.yaml`; the shared-level requirement text in
`charts/node-local-data/templates/NOTES.txt`; the `0.1.1` version bump in
`charts/node-local-data/Chart.yaml` back to `0.1.0` unless the new fix warrants its own bump; the
shared-level pod `securityContext` assertions, the two added NOTES assertions, and the three
`selinux.sharedLevel` schema and validation parametrize cases in `tests/chart/test_render.py`; the
"Required shared SELinux level" section, the `selinux.sharedLevel` defaults-table row, and the
shared-level paragraph under "Upgrade safely" in `README.md`; the shared-MCS-level paragraphs in
`tuning/gotchas.md`; and the `selinux.sharedLevel` block in `my-values.yaml`.

2.12 WHEN those removals are performed THEN they SHALL be done as targeted edits, because `charts/`,
`tests/`, `examples/`, and `my-values.yaml` are untracked and cannot be reverted with `git checkout`,
and `README.md` and `tuning/gotchas.md` carry large unrelated pre-existing Helm-conversion changes that
SHALL NOT be wholesale-reverted.

### Unchanged Behavior (Regression Prevention)

Existing behavior that must be preserved.

3.1 WHEN the fix is applied THEN the system SHALL CONTINUE TO leave the existing multi-hundred-GB
node-local mirror in place, with no deletion, relocation, or forced re-sync of mirrored data as part of
the fix.

3.2 WHEN `localVolume.enabled` is false THEN the chart SHALL CONTINUE TO render and install a
routing-only deployment with its current resource set and behavior.

3.3 WHEN the sync agent runs on a node with correct labels THEN it SHALL CONTINUE TO mirror by writing
through staging and renaming into place without `--inplace`, honor `syncAgent.excludes`, propagate
source deletions, and reconcile node readiness labels and taints through `sync_pass` and
`reconcile_node_state`.

3.4 WHEN ArcGIS workloads are deployed or replaced by ArcGIS Enterprise tooling THEN they SHALL
CONTINUE TO run with their current pod specifications, whose only securityContext is
`{"supplementalGroups":[117932853]}` and which set no `seLinuxOptions`.

3.5 WHEN ArcGIS resolves its registered folder data store path THEN that exact path SHALL CONTINUE TO
exist on every schedulable node, remain below `/mnt/` as Bottlerocket requires, and remain created by
the Path Shim and `DirectoryOrCreate` without uninstall deleting node filesystem content.

3.6 WHEN the cluster runs after the fix THEN SELinux SHALL CONTINUE TO be enforcing, shared directories
SHALL CONTINUE TO use mode 0755 root:root, and no consumer SHALL become privileged as a side effect,
except for an explicitly documented and justified MCS exemption limited to this project's own
DaemonSet if the selected design requires it.

3.7 WHEN the chart is upgraded THEN it SHALL CONTINUE TO avoid changes that mark NodeClaims Drifted,
since node replacement discards each node's local mirror; `aws.amiAlias`, the EC2NodeClass, and the
NodePool disruption settings SHALL remain unchanged unless the design states otherwise.

3.8 WHEN validation is run THEN `helm lint --strict`, `helm template`,
`./scripts/check-placeholders.sh`, `kubectl apply --dry-run=server`, and the existing chart and agent
test suites under `tests/chart` and `agent/tests` SHALL CONTINUE TO pass.

3.9 WHEN the Karpenter pool operates THEN warm capacity, the dedicated taint, the data-not-ready taint,
and the workload label contract SHALL CONTINUE TO behave as they do today.
