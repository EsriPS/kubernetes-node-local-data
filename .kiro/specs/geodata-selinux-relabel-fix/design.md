# geodata-selinux-relabel-fix Bugfix Design

## Overview

Publishing an ArcGIS geocode server that consumed the geocoding `local` PersistentVolume made
`/mnt/geodata` inaccessible to every other consumer on one node. The root cause is confirmed: the
`local` volume plugin applies SELinux labeling to the volume it mounts, while the in-tree `hostPath`
plugin does not. `datasetRoot` is simultaneously the `local` PV path, the routing `hostPath`
registration path, the Path Shim path, the sync agent's write root, and the Bottlerocket
instance-store bind target, so the single relabeling consumer redefined the label of the one directory
every other consumer depends on.

The property that made this work for months is that the shared tree carried **no MCS categories**. MCS
permits any `container_t` process to access a category-free object, so category-free is the shareable
state. The fix therefore aims to **leave the shared tree at the label a healthy node carries, by
ensuring nothing relabels it**, not to make every consumer match a relabel. Matching a relabel is
impossible inside the hard constraint that no ArcGIS pod specification may change, and it is doubly
impossible now that categories are confirmed to be per pod sandbox.

**Verification gate V1 is resolved and Option 1 is selected.** A geocoding service was published
against the routing `hostPath` folder data store on the live cluster, it works, and it introduces no
relabel. The design is therefore a single branch: **remove the relabeling volume type from the chart
entirely** — drop the geocoding `local` PV, its StorageClass, and its template, and serve geocoding
from the same non-relabeling `hostPath` folder data store that routing already uses. That leaves one
mount type over the tree, so nothing can relabel it, no MCS exemption exists anywhere, the sync agent's
write root and the registered routing path are untouched, and the chart loses a component. The
fallback that was previously carried in full (a `local` PV isolated on a sibling path, with an MCS
exemption on a dedicated agent container) is now moot and is recorded only as a rejected alternative.

Live cluster work resolved the remaining mechanism questions and changed the recovery procedure. The
findings, recorded in full in the sections below:

| # | Finding | Where it lands |
|---|---|---|
| F1 | Relabel repair is **denied** on Bottlerocket; **node replacement is the primary recovery** | [Recovery](#recovery-for-already-affected-nodes) |
| F2 | The **`seclabel` rule** replaces "local PV bad, hostPath good" | [Root cause 7](#hypothesized-root-cause) |
| F3 | PV-based data stores are **remapped in-pod**; `hostPath` stores are not | [Root cause 9](#hypothesized-root-cause), [Documentation](#documentation-changes) |
| F4 | **No category inheritance** — a clean tree stays clean (D5 resolved) | [Root cause 8](#hypothesized-root-cause), [Evidence 4b](#evidence-capture) |
| F5 | Repair cost tracks **inodes, not bytes** — one rejection ground withdrawn | [Rejected: a label-reset reconciler](#a-label-reset-reconciler) |
| F6 | **Readiness signals cannot detect this condition** — four confirmations | [Root cause 10](#hypothesized-root-cause), [Agent behavior](#agent-behavior-when-a-managed-root-is-not-enumerable) |
| F7 | Observability gaps: rclone output is swallowed, `du -sh` lies | [Agent behavior](#agent-behavior-when-a-managed-root-is-not-enumerable) |
| F8 | First-pass timing versus `RESULTS.md` | [Documentation](#documentation-changes) |
| F9 | Released PV with a stale `claimRef` | [Current cluster state](#current-cluster-state), [Operational sequence](#operational-sequence) |

The design also specifies render-time chart validation that keeps the overlap unrepresentable and
guards any future relabeling volume, the evidence record, the recovery procedure, the documentation
rewrite around volume-type asymmetry, and the complete rollback of the previous session's rejected
shared-MCS-level changes.

No code changes are made in this phase.

## Glossary

- **Bug_Condition (C)**: A node state in which a relabeling volume mount covers a path that equals,
  contains, or is contained by a path shared to other consumers through a non-relabeling `hostPath`
  registration.
- **Property (P)**: The desired behavior. The shared tree's object label matches the reference label a
  healthy node carries — type and level, so no MCS categories — and every routing consumer, the Path Shim,
  and the sync agent can each perform their required operations on the paths they are configured for.
- **Preservation**: Behavior that must be unchanged — the existing multi-hundred-GB mirror, the
  routing-only deployment shape, direct-mirror sync semantics, ArcGIS pod specifications, the exact
  registered routing path, enforcing SELinux with 0755 root:root directories, NodeClaim stability, and
  the passing validation suite.
- **MCS categories**: The `cN,cM` component of an SELinux level. A **category-free** object (level `s0`
  with no categories) is accessible to any `container_t` process regardless of that process's
  categories; a categorized object is not. The pair is assigned **per pod sandbox**, confirmed on this
  cluster: two pods of the same ReplicaSet on the same node hold different pairs. It is not a property of
  the Deployment, the ReplicaSet, or the ArcGIS service.
- **Relabeling volume mount**: A mount that gets its content's SELinux context set to the consuming
  pod's context. Relabeling requires **both** conditions of the `seclabel` rule below. Resolved (D1):
  where it happens on this stack it is a recursive on-disk `setxattr` on per-inode labels, not a
  `context=` mount option, so the label persists after the mount, the claim, and the consuming pod are
  all gone.
- **The `seclabel` rule (F2)**: A volume is relabeled only when the volume plugin reports SELinux
  support **and** the backing filesystem carries the `seclabel` mount option. `hostPath` fails the first
  test by design. NFSv4.1 mounted without `security_label` fails the second. This supersedes the earlier
  shorthand "local PV bad, hostPath good", which happened to be true here but describes the wrong
  mechanism.
- **Reference label**: The label a healthy pool node carries on the shared tree, captured as a histogram
  across the tree rather than read from the root inode alone. Captured on `ip-10-0-37-12`:
  **154 files and 49 directories, all `system_u:object_r:local_t:s0`** — one uniform label.
- **`datasetRoot`**: `/mnt/geodata`. The bind target, the agent write root, the Path Shim path, and the
  routing registration path. Under the selected option it is no longer any `local` PV's path.
- **Routing path**: The path registered with ArcGIS as an organization-wide `hostPath` folder data
  store. Remains exactly `/mnt/geodata`, so no ArcGIS re-registration and no data migration is forced.
- **Agent write root**: The path the sync agent passes as `--dest-root` and mounts by `hostPath`.
- **`geodata-path-shim`**: The DaemonSet in `charts/node-local-data/templates/08-path-shim.yaml` that
  guarantees the routing path exists on every schedulable node.
- **`geodata-sync-agent`**: The DaemonSet running `agent/sync_agent.py`, uid 0, non-privileged, whose
  `list_datasets` and `sync_pass` produce the readiness taint and label reconciliation.
- **Node replacement**: The recovery procedure for a node whose tree carries a poisoned label. Cordon,
  relocate its ArcGIS workloads, let Karpenter provision a replacement, wait for the replacement's first
  sync, delete the old NodeClaim. Verified end to end on this cluster (F1). It is the primary recovery
  because relabeling is **denied** on Bottlerocket.

## Bug Details

### Bug Condition

The bug manifests when a relabeling volume mount and a non-relabeling `hostPath` share one directory
tree on a node. The relabeling mount sets the tree's SELinux label to the consuming pod's context,
including its randomly assigned MCS category pair. Every other process on that node whose categories
differ — routing map service pods reading through the organization-wide `hostPath` mount, the Path Shim,
and the uid 0 sync agent — is then denied, because MCS is evaluated independently of the Unix mode bits
and the directory is `drwxr-xr-x` root:root either way.

**Formal Specification:**

```
FUNCTION isBugCondition(input)
  INPUT: input of type NodeState = {
           mounts       : set of Mount,     -- every pod mount of a host directory on this node
           sharedRoots  : set of Path       -- paths shared through non-relabeling hostPath
                                            -- registration or chart hostPath volumes
         }
         WHERE Mount = { volumeType : {"hostPath", "local", "csi"},
                         hostPath   : Path,
                         fsOptions  : set of String,   -- from /proc/mounts
                         podLevel   : SELinuxLevel }
  OUTPUT: boolean

  relabelers := { m IN input.mounts | relabelsOnMount(m) }

  FOR EACH m IN relabelers DO
    FOR EACH r IN input.sharedRoots DO
      IF pathsOverlap(m.hostPath, r) THEN
        RETURN true
      END IF
    END FOR
  END FOR
  RETURN false
END FUNCTION

FUNCTION relabelsOnMount(m)
  -- The seclabel rule (F2). BOTH conditions are required.
  --   1. the plugin reports SELinux support: hostPath does not; local does;
  --      csi depends on CSIDriver.seLinuxMount
  --   2. the backing filesystem carries seclabel: instance-store xfs does;
  --      NFSv4.1 without security_label does not
  RETURN pluginSupportsSELinux(m.volumeType)
     AND "seclabel" IN m.fsOptions
END FUNCTION

FUNCTION pathsOverlap(a, b)
  -- Equality or containment in either direction, after lexical normalization.
  -- Symlink and bind-mount aliasing is NOT detected lexically.
  a' := clean(a)
  b' := clean(b)
  RETURN a' = b'
      OR hasPrefix(a' + "/", b')
      OR hasPrefix(b' + "/", a')
END FUNCTION
```

`clean` is lexical normalization: it collapses `//`, resolves `.` and `..`, and strips a trailing
separator, so `/mnt/geodata/`, `/mnt/geodata`, and `/mnt/geodata/x/..` are one path. `hasPrefix` takes
the prefix first, matching the convention already used in `_validate.tpl`.

### Examples

- `localVolume.enabled: true` with `localVolume` path `/mnt/geodata` and the routing `hostPath` data
  store registered at `/mnt/geodata`. Expected: the chart refuses to render, naming both paths.
  Actual: it renders and installs silently, and the first geocoding pod breaks the node.
- A geocode server pod starts on `ip-10-0-37-101` consuming `geodata-local-pv`. Expected: the tree's
  label is unchanged, still `system_u:object_r:local_t:s0` as on every healthy node. Actual: the tree's
  label becomes `system_u:object_r:data_t:s0:c20,c476` — the pod's categories **and** a different type.
- The geocoding data store is deregistered, the ArcGIS-owned PVC `arcgis/geodata-local-pvc` is deleted,
  and no pod anywhere references it. Expected under a mount-scoped mechanism: the label reverts with the
  mount. Actual: the label persisted, the Path Shim pod (`container_t:s0:c160,c744`) stayed denied
  `ls /mnt/geodata`, and the sync agent stayed in `CrashLoopBackOff`. The label is on disk.
- A routing map service pod with categories `c428,c743` reads `/mnt/geodata` on an unaffected node.
  Expected and actual: success, because the directory has no categories. The same pod on
  `ip-10-0-37-101`: `Permission denied`, readiness 503, deployment past its progress deadline.
- The uid 0 `geodata-sync-agent` pod on `ip-10-0-37-101`. Expected: enumerate datasets and complete a
  pass. Actual: `lstat /mnt/geodata/<dataset>: permission denied` from rclone,
  `PermissionError: [Errno 13]` out of `list_datasets`, `CrashLoopBackOff` — 269 restarts when first
  observed, 379 after the consumer was removed — so no pass completed and no taint or label
  reconciliation happened. The node did not self-heal.
- **A geocoding service published against the `hostPath` folder data store.** Expected and actual: it
  publishes, serves, reads the mirror as `container_t:s0:c40,c511`, and `/mnt/geodata` stays
  `system_u:object_r:local_t:s0` on every node with its mtime unchanged. This is the V1 result and the
  selected design ([V1 resolved](#verification-gate-v1--resolved-option-1-selected)).
- Edge case: no relabeling mount exists at all. Expected and actual: the tree stays category-free and
  every consumer works. This is the permanent state under the selected option.

### Current cluster state

The end state after V1, the recovery, and the republication. This is the starting point for the
operational sequence below.

**Pool nodes.** Two: `ip-10-0-37-12` (the node that was always healthy) and `ip-10-0-39-129` (the
Karpenter-provisioned replacement for the poisoned `ip-10-0-37-101`). Both advertise
`gen=g57a70ff5b429` and `count=3`, both have completed a first sync, and neither carries
`data-not-ready`.

**Labels.** `/mnt/geodata` is `system_u:object_r:local_t:s0` on **all 8 nodes**, with a successful read
on each. No node carries a categorized or `data_t` label.

**The relabeling volume is gone from the cluster.** `arcgis/geodata-local-pvc` no longer exists, and
`geodata-local-pv` has been deleted manually (F9, below). The only geodata-related claim still in use is
`geodata-source-pvc`, the NFS source, held by the sync-agent pods.

**Geocoding is served from the `hostPath` store.** The geocode service published during V1 is running
and healthy, reading `/mnt/geodata` through the organization-wide `hostPath` folder data store. Routing
has been republished and is healthy.

**F9 — the released PV had a stale `claimRef`, and it had to be deleted by hand.** After the
ArcGIS-owned PVC was deleted, the PV sat in this state:

```
geodata-local-pv   phase=Released
                   claimRef=arcgis/geodata-local-pvc   (the claim no longer existed)
                   path=/mnt/geodata
                   persistentVolumeReclaimPolicy=Retain
                   accessModes=[ReadOnlyMany]
```

A `Released` PV with a stale `claimRef` never rebinds, which `tuning/gotchas.md` already records, and
`helm upgrade` does not reconcile it. It was deleted explicitly. The operational sequence below
therefore **verifies the PV is actually absent after the upgrade** and deletes it if it survives.
`reclaimPolicy: Retain` means deleting the PV object touches no node filesystem content, so this does
not affect the mirror (3.1).

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**

- ArcGIS routing consumers continue to read the registered `hostPath` path and pass readiness, during
  and after any geocoding pod start, restart, or republication (2.3).
- The Path Shim continues to create and stat its configured path on every node in the cluster (2.4).
- The sync agent continues to enumerate datasets, complete a pass, and reconcile the readiness taint
  and the dataset-generation and dataset-count labels (2.5, 3.3).
- The registered routing path stays exactly `/mnt/geodata`, below `/mnt/` as Bottlerocket requires, so
  no ArcGIS re-registration and no data migration is forced (3.5).
- Direct-mirror semantics are unchanged: one recursive `rclone sync`, no `--inplace`, `syncAgent.excludes`
  honored, source deletions propagated, per-file rename replacement (3.3).
- The existing multi-hundred-GB mirror stays in place. Nothing in the fix deletes, moves, or forces a
  re-sync of mirrored data (3.1). Node replacement as a recovery action does discard one node's mirror
  and re-derive it from the authoritative source, which is the node pool's own premise and is the
  documented behavior of any replacement.
- ArcGIS pod specifications are untouched. Their only `securityContext` remains
  `{"supplementalGroups":[117932853]}` with no `seLinuxOptions` (3.4, 2.7).
- SELinux stays enforcing, shared directories stay 0755 root:root, and no consumer becomes privileged
  as a side effect (3.6). Under the selected option there is **no** MCS exemption anywhere, so 3.6 holds
  without invoking its documented-exception clause.
- A routing-only deployment still renders and installs with its current resource set, subject to the
  removal of the `localVolume` value itself (3.2).
- `aws.amiAlias`, the EC2NodeClass, and the NodePool disruption settings are unchanged, so no NodeClaim
  is marked Drifted and no node loses its mirror as a side effect of the upgrade (3.7).
- Warm capacity, the dedicated taint, the data-not-ready taint, and the workload label contract behave
  as they do today (3.9). Warm capacity is load-bearing in the recovery procedure: evicting the
  placeholder is what forces Karpenter to provision the replacement node.
- `helm lint --strict`, `helm template`, `./scripts/check-placeholders.sh`,
  `kubectl apply --dry-run=server`, and the suites under `tests/chart` and `agent/tests` keep passing
  (3.8).

**Scope:**

Every node state in which no relabeling volume mount overlaps a shared path is completely unaffected by
this fix. That is now every node in the cluster: all 8 carry `system_u:object_r:local_t:s0` on
`/mnt/geodata` with a successful read. It also includes:

- Every ArcGIS interaction through the organization-wide `hostPath` mount — publication, query, and
  placement — on all 21-plus service pods, including the geocode service published during V1.
- Every consumer of the NFS source through `ontap-gisdata-pvc`, which the `seclabel` rule shows cannot
  be relabeled (F2).
- Node provisioning, the bind at boot, first-sync gating, and node replacement.

The expected correct behavior for the bug condition itself is Property 1 in
[Correctness Properties](#correctness-properties).

## Hypothesized Root Cause

The root cause is **confirmed**, not hypothesized: a relabeling volume type was pointed at the same
directory that a non-relabeling registration shares to every other consumer. Every determination that
gated the design (D1, D2, D5) is now resolved from live inspection. D4, which existed only to price
Option 2's MCS exemption, is withdrawn along with Option 2.

1. **Confirmed — volume-plugin asymmetry.** The in-tree `hostPath` plugin performs no SELinux
   labeling. The `local` plugin applies the consuming pod's context to the volume. This is why routing
   consumers never disturbed the label in months of operation and one geocoding pod did so immediately.
   Superseded in generality by the `seclabel` rule in item 7, which explains *why* the asymmetry exists
   and which other volumes are and are not exposed.

2. **Confirmed — path collapse is the latent defect, publication is the trigger.** The Helm conversion
   collapsed five distinct concerns onto `datasetRoot`. The pre-Helm manifests (recoverable at
   `HEAD:manifests/`) kept them apart: the bind and the agent's `hostPath` mount were `/mnt/geodata`,
   the agent wrote through `/mnt/geodata/staging/<dataset>` and renamed into
   `/mnt/geodata/active/<dataset>`, and both data stores were registered against
   `/mnt/geodata/active`. No local-PV consumer had existed, so nothing had ever exercised the collapse.

3. **Confirmed — category-free is the shareable state, and it is what was lost.** The
   cross-namespace survey shows ArcGIS pods with `c428,c743`, `c912,c1018`, `c683,c944` and other
   distinct pairs all reading `/mnt/geodata` successfully on unaffected nodes. That is only possible
   against a category-free object. On the affected node the only successful reader held exactly the
   directory's categories. The design goal follows directly: preserve category-free, do not chase
   matching.

4. **Resolved (D1) — the labeling is a recursive on-disk relabel, not mount-scoped.** `/proc/mounts`
   showed the geodata mount as
   `/dev/nvme2n1 /mnt/geodata xfs rw,seclabel,noatime,attr2,inode64,logbufs=8,logbsize=32k,noquota 0 0`
   — `seclabel`, no `context=` — while the same pod's own mounts (overlay root, `/dev` tmpfs, devpts) do
   carry `context=`. Mount-scoped labeling exists on the node and is not what labeled the tree. The
   label then survived deregistration of the data store, deletion of the PVC, and the disappearance of
   every pod referencing it. A mount-scoped label cannot survive its mount.

   The consequence is structural: removing the mount, deleting the PVC, and deregistering the data store
   are **not** a repair. The affected node did not self-heal, and the sync agent went from 269 to 379
   restarts across the removal. The recorded discriminator, kept for future diagnosis, is `seclabel`
   versus `context=` in `/proc/mounts`.

5. **Confirmed — the relabel changed the SELinux type as well as the level.** Healthy nodes carried
   `system_u:object_r:local_t:s0`; the affected node carried `system_u:object_r:data_t:s0:c20,c476`.
   Both components moved, so any label-based repair target must be captured from a healthy node rather
   than derived by clearing categories.

6. **Confirmed — MCS categories are assigned per pod sandbox, not per service.** Three pods of the one
   ReplicaSet `arcgis-system-publishingtools-gpsyncserver-dgo59-b5cfc74f9`:

   ```
   ...b5cfc74f922t7q   ip-10-0-37-12    system_u:system_r:container_t:s0:c372,c821
   ...b5cfc74f9rrkkw   ip-10-0-37-12    system_u:system_r:container_t:s0:c388,c1019
   ...b5cfc74f9xtll4   ip-10-0-37-101   system_u:system_r:container_t:s0:c100,c973
   ```

   Two pods of the same ReplicaSet on the same node hold different category pairs. Assignment is a
   property of the pod sandbox, unrelated to the Deployment, the ReplicaSet, or the ArcGIS service
   identity. This is why a pinned shared level had no reachable success state, and it is one of the three
   reasons the Option 2 fallback is moot.

7. **Resolved (D2), and generalized — the `seclabel` rule (F2).** A volume is relabeled only when
   **both** the plugin reports SELinux support **and** the backing filesystem carries `seclabel`.
   Observed on one node:

   ```
   /dev/nvme2n1 /mnt/geodata xfs  rw,seclabel,noatime,attr2,inode64,...          ← relabelable
   svm-...fsx.us-west-2.amazonaws.com:/gisdata /mnt/source nfs4 ro,relatime,vers=4.1,
       rsize=65536,wsize=65536,hard,proto=tcp,nconnect=16,sec=sys,...            ← no seclabel
   ```

   `hostPath` fails the first test by design. NFSv4.1 without `security_label` fails the second: the
   kernel presents one fixed context for the whole mount, observed as `local_t:s0` uniformly on the root
   and every child, so there are no per-file tags to fight over and a relabel attempt would fail rather
   than succeed destructively. Belt and braces on the mount-option path: `CSIDriver.seLinuxMount` is
   `false` for `nfs.csi.k8s.io`, `ebs.csi.aws.com`, `efs.csi.aws.com`, and `smb.csi.k8s.io`, so
   `-o context=` is not available to those drivers either.

   Empirical proof that the NFS source is safe: six ArcGIS pods with six distinct MCS pairs —
   `c726,c972`, `c122,c402`, `c151,c210`, `c115,c464`, `c550,c797`, `c896,c990` — two of them co-located
   on one node, all sharing `ontap-gisdata-pvc` (`ReadOnlyMany`, NFS), all healthy. That is the exact
   sharing pattern that broke on the local PV, working on a filesystem without `seclabel`.

   **UNVERIFIED:** EBS-backed PVs probably pass both tests and therefore probably are relabeled. It does
   not bite here because they are `ReadWriteOnce` with a single consumer. Anyone who shares one should
   check for `seclabel` first. This has not been tested on this cluster and should not be stated more
   strongly than that.

8. **Resolved (D5) — no category inheritance, so a clean tree stays clean (F4).** The sync agent on the
   healthy node runs as `container_t:s0:c750,c987`, yet every file it wrote is
   `system_u:object_r:local_t:s0` with no categories, including recent output
   (`.HGDS_NA_Q2_2026.geodatabase-conch`, mtime `Sep 1 18:03`). New inodes do not take the writing
   process's categories on this filesystem. Two consequences: this is why the tree stayed shareable for
   months, and a repaired or freshly synced tree does not drift. The recovery procedure therefore needs
   no recurring post-pass label re-check — one post-sync confirmation is enough.

   Reference label, captured from `ip-10-0-37-12`, the only healthy node holding the mirror:
   **154 files and 49 directories, all `system_u:object_r:local_t:s0`.** One uniform label across both
   filetypes, so no repair needs to split by filetype. `ip-10-0-35-13` is a non-pool node with an empty
   Path Shim directory and is **not** a valid reference for file labels; the earlier per-node table
   treated it as one.

9. **Confirmed — PV-based data stores are remapped in-pod, `hostPath` stores are not (F3).** For a
   PV-based folder data store ArcGIS mounts the volume at
   `/arcgis/volumes/pvc/<derived>/<registered-path>`, where `<derived>` comes from the CSI volume handle
   — observed as `svm-….fsx.us-west-2.amazonaws.com` for the ONTAP
   volume, and the literal `noid` when there is no CSI handle, which is the local-PV case. So a local PV
   registered with volume path `/mnt/geodata` appears in-pod at `/arcgis/volumes/pvc/noid/mnt/geodata`.
   A `hostPath` store is not remapped: it appears at the host path itself. This resolved a live
   diagnostic confusion during V1 — looking for `/mnt/geodata` in a PV-backed pod and concluding the
   mount was missing — and belongs in the troubleshooting documentation.

10. **Confirmed — readiness signals cannot detect this condition (F6).** Four independent
    confirmations, all on `ip-10-0-37-101` while its tree was completely unreadable:

    - It advertised `gen=g57a70ff5b429` and `count=3` with no `data-not-ready` taint. Its labels were
      correct as an *inventory of on-disk names* and useless as a health signal.
    - ArcGIS pods on it reported `3/3` ready.
    - A geocode pod with fresh categories reported `3/3` ready on a node it could not read.
    - Both pool nodes looked byte-identical by label while one was broken, so a republished service had
      roughly even odds of landing on the broken one — and PublishingTools had pods on both.

    This is the justification for agent-side detection: readiness must rest on an explicit access check
    by the consuming identity, not on a directory inventory.

11. **Confirmed — relabel repair is denied on Bottlerocket (F1).** See
    [Recovery](#recovery-for-already-affected-nodes). Reading a labeled object and relabeling it are
    governed by different permissions, and Bottlerocket's policy grants the first and not the second to a
    privileged debug container's context. This is what demotes label repair to an unverified fast path
    and makes node replacement the primary recovery.

## Correctness Properties

Property 1: Bug Condition - No relabeling mount overlaps a shared tree

_For any_ node state where the bug condition holds (`isBugCondition` returns true), the fixed
configuration SHALL make that state unreachable: chart rendering fails before install when the values
express it, and the selected ownership model contains no relabeling mount over any shared path, so the
shared tree's object label is left as the healthy reference label — type and level both, carrying no MCS
categories — and every routing consumer, the Path Shim, and the sync agent each succeed at their required
operation on the paths they are configured for, across any number of geocoding pod restarts or
republications and any independent node replacement.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7**

Property 2: Preservation - Non-overlapping node states and the routing-only deployment

_For any_ node state where the bug condition does NOT hold (`isBugCondition` returns false), and for
any chart configuration that does not place a relabeling volume over a shared path, the fixed
configuration SHALL produce the same observable result as the original: the same rendered
routing-only resource set, the same registered routing path, the same direct-mirror sync semantics and
readiness reconciliation, unchanged ArcGIS pod specifications, enforcing SELinux with 0755 root:root
directories, the mirror left in place, and unchanged NodePool and EC2NodeClass identity.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9**

## Fix Implementation

### Candidate evaluation

Requirement 2.2 names four candidates. The evaluation stands as the record of why (a) was selected; the
detail of the rejected branches is in [Rejected alternatives](#rejected-alternatives).

| Candidate | Relabel still possible? | Agent write access | MCS exemption | Extra disk | Geocoding scale-out | Verdict |
|---|---|---|---|---|---|---|
| (a) Drop the `local` PV; register geocoding against the existing `hostPath` store | No — one non-relabeling mount type only | Unchanged | None | None | Unconstrained | **Selected**, V1 confirmed |
| (b) `local` PV on a dedicated path outside the routing tree | Yes, confined to a subtree nothing else reads | Denied without an exemption | Required, on one container of this project's DaemonSet | Yes — a second full copy of geocoding data | One pod per node | Rejected |
| (c) `local` PV kept, its subtree removed from agent management | Yes, confined | Populated out of band | None | Yes | One pod per node | Rejected — same limits, plus loss of automatic refresh |
| (d) Reintroduce distinct publication child directories | Only if a child is still shared with a relabeler | Rename-into-place is denied the same way a write is | Depends | Depends | One pod per node | Not a fix on its own |

Candidate (d) is the pre-Helm structure. It is worth recording why it is not a fix by itself: pre-Helm,
the local PV and the `hostPath` store both pointed at `/mnt/geodata/active`, so a local-PV consumer
would have relabeled `active/` and the pre-Helm agent's `os.rename(stage, dest)` would have failed,
because `rename(2)` requires write permission on the destination directory. Separating paths is
necessary but not sufficient.

### Verification gate V1 — RESOLVED, Option 1 selected

**Question:** may an ArcGIS geocoding service use a `hostPath` folder data store? The repository's
pre-Helm notes stated that local PersistentVolumes are supported for geocoding and not for routing, and
never asserted the converse.

**Answer: yes.** The operator published a geocoding service against the routing `hostPath` folder data
store and confirmed it works. All three confirm signals hold.

**Confirm signal 1 — it publishes and stays up.** Service
`arcgis-kop3sru8xckzfezwj2i12-geocodeserver-pxw9n` published successfully. Deployment `1/1`, one
ReplicaSet, one pod, `Ready=True since 2026-09-02T23:03:30Z` uninterrupted, 0 restarts on both
containers. The only `Unhealthy` event had `count=2`, between `23:02:59Z` and `23:03:00Z` — two probe
failures ten seconds after container start, during ArcGIS initialisation, and none since.

**Confirm signal 2 — it reads the mirror with arbitrary categories.** The pod reads the node-local
mirror as `container_t:s0:c40,c511` — an arbitrary category pair, no special configuration — and
enumerates the contents of `Locators_HGDS`, including `HGDS_Locator_APAC_Q2_2026.loc` and `.loz`, the
`EU` locators, and so on.

**Confirm signal 3, the decisive one — no relabel.** `/mnt/geodata` remained
`system_u:object_r:local_t:s0` on **all 8 nodes**, with mtime unchanged at `Sep 2 14:08`, after that
geocoding pod had been running against it. Its pod spec carries
`datastore-bf148f2c3a1a4b7984d894e14ff001d3 → /mnt/geodata (type: Directory)` and no PV mount, which is
consistent with F3: a `hostPath` store is not remapped, so it appears at the host path itself.

**Decision: Option 1.** Serve geocoding from the shared `hostPath` folder data store and remove the
relabeling volume type from the chart. The registration shape already exists organization-wide, so no
`registerItem` call and therefore no `allowServicesRestart` service restart is incurred by the fix.
Registration is data-store configuration, not a change to any ArcGIS pod specification, so this stays
inside constraint 2.7 and preserves 3.4.

### Selected design — remove the relabeling volume type

Serve both workloads from the one non-relabeling `hostPath` folder data store. Nothing relabels the
tree, so it stays category-free permanently. The sync agent, its write root, the Path Shim, the bind
target, and the registered routing path are all unchanged, which makes 3.1–3.5, 3.7, and 3.9 hold by
construction. No MCS exemption exists anywhere, so 3.6 holds without its exception clause.

**Changes required**

`charts/node-local-data/templates/09-local-pv.yaml`

1. Delete the file. Removes the `geodata-local-sc` StorageClass and the `geodata-local-pv`
   PersistentVolume — the only relabeling mount the project can create.

`charts/node-local-data/values.yaml`

2. Remove the `localVolume` block and its comment.

`charts/node-local-data/values.schema.json`

3. Remove the `localVolume` property and its `required` entry. Top-level `additionalProperties: false`
   then makes any leftover `localVolume` key in an operator's values file a hard render failure naming
   the key, rather than a silently ignored setting.

`charts/node-local-data/templates/_validate.tpl`

4. Add a legacy-key rejection so the failure explains itself rather than only naming an unknown key:
   fail when `.Values.localVolume` is present at all, with a message stating that the geocoding local
   PV was removed because it relabels the shared tree, and that geocoding is now served by the routing
   `hostPath` registration. This is how requirement 2.1 is satisfied — the overlap is unrepresentable
   rather than merely validated.
5. Add the `pathsOverlap` helper and the overlap check described in
   [Render-time overlap validation](#render-time-overlap-validation). It has no relabeling path to check
   today; it is retained as a forward guard so that any future relabeling volume added to the chart is
   checked against the shared roots on the day it is added.

`charts/node-local-data/templates/_helpers.tpl`

6. Remove `node-local-data.localStorageClassName` and `node-local-data.localPersistentVolumeName`.

`charts/node-local-data/templates/NOTES.txt`

7. Remove both branches of the `localVolume.enabled` conditional and the two-stage install guidance
   that exists only to sequence it. Replace with a single geocoding registration note stating that
   geocoding and routing share the one `hostPath` folder data store at the rendered path, and that the
   readiness command must show a completed first sync before publishing against it.

`charts/node-local-data/Chart.yaml`

8. `version: 0.2.0`. This removes rendered resources and changes the install workflow, so it is not a
   patch. See the rollback note about the prior `0.1.1` bump.

`agent/sync_agent.py`

9. Optional, and out of the critical path. See
   [Agent behavior](#agent-behavior-when-a-managed-root-is-not-enumerable).

`my-values.yaml`, `README.md`, `tuning/gotchas.md`, `tuning/nvme.md`, `RESULTS.md`,
`tests/chart/test_render.py`

10. See [Documentation changes](#documentation-changes), [Rollback of the prior session's
    shared-MCS-level changes](#rollback-of-the-prior-sessions-shared-mcs-level-changes), and
    [Testing Strategy](#testing-strategy).

#### Operational sequence

Most of this is already done on the live cluster; the remaining work is the chart upgrade and the PV
verification. Steps recorded as done are kept so the order is legible and so the task list does not
repeat them.

1. Run the V1 verification and record its output. **Done** —
   [V1 resolved](#verification-gate-v1--resolved-option-1-selected).
2. Republish the production locator against the `hostPath` folder data store and confirm it serves.
   **Done** — the geocode service is healthy and routing has been republished.
3. Deregister the geocoding local-PV folder data store in ArcGIS Enterprise Manager. **Done.**
4. Confirm the ArcGIS-owned PVC is deleted and `geodata-local-pv` is not `Bound`. **Done** — the PVC is
   gone and the PV has since been deleted by hand (F9).
5. Recover any already-affected node ([Recovery](#recovery-for-already-affected-nodes)). **Done** —
   `ip-10-0-37-101` was replaced by `ip-10-0-39-129`. Deregistration alone does not restore access; the
   label is on disk (D1), and relabeling it is denied (F1).
6. `helm upgrade` to the version that no longer renders the StorageClass and PV. **Outstanding.**
7. **Confirm `geodata-local-pv` is actually absent, and delete it explicitly if it survives.** This is
   F9's requirement, not a formality: a `Released` PV with a stale `claimRef` never rebinds and never
   reconciles itself, and `helm upgrade` will not clean it up for you. On this cluster it was deleted
   manually before the upgrade, so the expected post-upgrade result is `absent`.

   ```bash
   kubectl get pv geodata-local-pv -o jsonpath='{.status.phase}{"\n"}' 2>/dev/null || echo absent
   kubectl delete pv geodata-local-pv     # only if it is still present after the upgrade
   ```

   `persistentVolumeReclaimPolicy: Retain` means deleting the PV object does not touch node filesystem
   content, so this does not affect the mirror (3.1).
8. Verify the end state: routing readiness green, the geocode service still serving, an agent pass in
   the log on both pool nodes, and `/mnt/geodata` still `local_t:s0` cluster-wide.

Steps 3 and 4 had to precede step 6, because Helm deletes the PV on upgrade and deleting a PV with a
live claim leaves the claim unbound and reports only as a binding timeout. Step 5 had to follow step 3,
because recovering a node while a local-PV consumer can still start means the recovery is undone on the
next start.

**What this gives up.** The geocoding data store loses PV-based registration through Enterprise Manager
and moves to the Admin REST API model with organization-wide mount scope. That scope is already in force
for routing, so it is not a new exposure. It also loses the loud `MountVolume.SetUp` failure a `local`
volume gives on a node without the data, replaced by a path-present-but-empty failure at query time —
the same trade the Path Shim already makes for routing, and the reason the first-sync gate and node
affinity still matter. F6 sharpens this: an empty or unreadable path is not visible in any readiness
signal the project publishes today, which is what the optional agent hardening addresses.

**`localVolume.enabled` default.** The question resolves as *removed*. There is no `localVolume` value,
so there is no default to keep false. The stronger guarantee replaces the weaker one: the chart cannot
express a relabeling volume at all.

### Rejected alternatives

#### The isolated-`local`-PV fallback (former Option 2)

The design previously carried a fully specified fallback in case V1 refuted Option 1: keep the `local`
PV, give it a dedicated path outside the routing tree (a sibling under `/mnt/`, never a child), add a
second Bottlerocket bind target, split the sync agent into a non-privileged routing container and an
MCS-exempt geocoding container, and accept the security tradeoff of that exemption. It was the fallback
because it was the only model that preserved PV-based geocoding registration while keeping a relabel
away from routing data.

It is now moot, for three reasons: V1 passed, so no fallback is needed; SELinux labels live on inodes,
so a bind mount, hard link, or symlink of the geocoding data into the sibling path would expose the same
inodes to the same relabel, meaning the option required a genuinely **second full copy** of the
geocoding datasets and the disk to hold it; and MCS categories are assigned per pod sandbox (root cause
6), so each geocoding pod's mount would restamp the shared geocoding directory with its own pair and
cap geocoding at **one pod per node**, with the failure signature of replicas succeeding or failing by
start order. The whole apparatus — the two bind roots, the path restructure, the duplicate copy, the
split agent containers, the MCS exemption mechanisms and the determination that priced them, the
out-of-band population variant, and the scale-out limitation — is removed from this document rather than
kept as dead weight.

#### A label-reset reconciler

A controller or loop that resets the tree to category-free after each relabel is rejected on two
grounds:

1. **Racy.** The relabel happens during volume setup, before the container starts. A reconciler that
   observes and repairs afterwards competes with the runtime; between the relabel and the repair every
   other consumer is denied, and a repair landing mid-setup can leave a partially relabeled tree.
   Repairing to category-free also breaks the geocoding pod that just relabeled, if the plugin's
   labeling was the thing granting it access.
2. **Unbounded.** Categories are reassigned on every geocoding pod start and every republication, so
   the loop never converges and there is no state in which the reconciler is done. It converts a
   configuration defect into permanent background work whose failure mode is a silent outage.

**Withdrawn: the cost argument (F5).** This document previously argued the repair would be "expensive on
hundreds of GB". That is wrong and is retracted. `df -i` on the instance store reports `IUsed 174` for
840 GB of data, because the payload is a small number of very large files. A recursive relabel is
roughly 174 `setxattr` calls — effectively instant. Cost tracks inodes, not bytes. The racy and
unbounded grounds stand unchanged and are sufficient on their own.

A separate point, established after the fact by F1: on Bottlerocket such a reconciler could not perform
the relabel anyway, because `setxattr` is denied even from a privileged debug container.

### Render-time overlap validation

Satisfies requirement 2.1 together with the legacy-key rejection, and stands as a forward guard. Add to
`_validate.tpl`, invoked from the templates that already call `node-local-data.validate`:

```
{{- define "node-local-data.pathsOverlap" -}}
{{-   $a := clean (index . 0) -}}
{{-   $b := clean (index . 1) -}}
{{-   if or (eq $a $b) (hasPrefix (printf "%s/" $a) $b) (hasPrefix (printf "%s/" $b) $a) -}}
true
{{-   end -}}
{{- end -}}
```

Rules:

- Build the set of **shared roots**: the routing registration path, which is also the Path Shim
  `hostPath` and the agent's routing `hostPath` mount. Build the set of **relabeling paths**: empty
  today, because the chart renders no relabeling volume. Any future volume that could be relabeled under
  the `seclabel` rule (F2) must be added to that set when it is added to the chart.
- Fail rendering when any relabeling path overlaps any shared root.
- **Legacy-key rejection.** Independently of the overlap check, fail when `.Values.localVolume` is
  present at all. This is what actually prevents the bug being reintroduced by configuration today; the
  overlap check is what prevents a *different* relabeling volume reintroducing it tomorrow.
- **Nesting after normalization.** Compare `clean`-normalized paths only. `clean` collapses duplicate
  separators, resolves `.` and `..` lexically, and strips a trailing separator, so
  `/mnt/geodata/geocoding/`, `/mnt/geodata//geocoding`, and `/mnt/geodata/routing/../geocoding` all
  normalize to `/mnt/geodata/geocoding` and are all caught. Containment is tested in both directions
  with an explicit trailing separator on the prefix, so `/mnt/geodata` does **not** falsely match the
  unrelated sibling `/mnt/geodata-geocoding`, while it does match the true child
  `/mnt/geodata/geocoding`.
- **Stated limitation.** This is lexical. `helm template` cannot resolve symlinks, bind mounts, or hard
  links on a node, so two distinct paths that alias the same inodes pass validation and still break at
  runtime. Record this in `tuning/gotchas.md`: because labels live on inodes, distinct paths are
  necessary but the operator must also ensure they are distinct inodes.

Failure message for the overlap check, naming both paths:

```
<relabeling path> "/mnt/geodata/geocoding" overlaps the shared routing path "/mnt/geodata".
A volume that is SELinux-relabeled on mount restamps its whole tree with the consuming pod's
MCS categories, while the routing hostPath registration does not. Sharing one tree between a
relabeling and a non-relabeling mount makes it unreadable to every other consumer. Choose a
path that neither equals, contains, nor is contained by the routing path, and keep it below /mnt/.
```

### Agent behavior when a managed root is not enumerable

**Optional, not required.** With one managed root that nothing can relabel, the selected design does not
depend on this. It is kept anyway, and it must not change observable behavior for a single readable
root — that is exactly what the preservation tests assert.

Why keep it. Two findings justify the work independently of the bug:

- **F7 — the agent is nearly silent while it works.** `run()` in `agent/sync_agent.py` uses
  `subprocess.run(..., capture_output=True)`, so `--stats=60s --stats-one-line -v` produces no output at
  all until the pass completes. The 59-minute first sync on the replacement node logged nothing after
  its startup banner, and progress had to be inferred from the filesystem. Stream rclone's stderr to the
  pod log instead of buffering it.
- **F6 — readiness signals cannot detect an unreadable root.** A node advertised a correct generation
  and count, with no taint, while its tree was completely unreadable, and every pod on it reported
  ready. Directory inventory is not a health signal. Readiness should rest on an explicit access check
  by the consuming identity.

Design:

- **Distinguish unreadable from empty.** `list_datasets` calls `directory.iterdir()`, which raises
  `PermissionError` on a denied directory, and `main()` calls it outside any handler, so the process
  exits and the pod enters `CrashLoopBackOff` — after which `sync_pass` never runs,
  `reconcile_node_state` never runs, and the node's taint and labels freeze at whatever they were. An
  empty directory means "this node holds no datasets" and legitimately re-applies the bootstrap taint; a
  denied directory means "unknown", and treating unknown as empty would re-taint a node whose mirror is
  complete. Return an explicit outcome — a `(status, names)` pair or a small result type — rather than a
  bare list, and do not let a denial kill the process.
- **Readiness stays derived from the routing root, bit for bit.** The dataset-generation digest and the
  dataset-count label keep their current derivation and their current values, so 3.3 is preserved
  exactly and no label churn is introduced.
- **Surface an unreadable root without requiring a log read.** Record the root's last outcome in a
  separate chart-owned label so a denial is observable by query, kept strictly separate from the
  readiness taint so it cannot gate routing scheduling. This is the F6 lesson in the smallest form that
  does not change existing signals.
- **Stream rclone output.** Replace the buffered `capture_output=True` call for the sync invocation with
  streamed stderr, so `--stats=60s` lands in the pod log as the pass runs. The parsing that consumes
  rclone's one-line stats must keep working.
- **No behavior change for the healthy single-root case.** A readable root produces the same
  enumeration, the same digest, the same count, the same taint transitions, and the same exit status as
  today.

### Evidence capture

Satisfies requirement 2.8. Every item is now captured. The commands are retained in re-run form for
future diagnosis, not as outstanding work.

| # | Item | Status |
|---|---|---|
| 1 | Geocodeserver pod `spec.volumes`, local-PV consumption via PVC | **Captured** — consumer since removed; V1's replacement pod uses `hostPath` with no PV mount |
| 2 | PVC, PV, StorageClass bind state | **Captured** — PV was `Released` with a stale `claimRef`, since deleted by hand (F9) |
| 3 | Kernel AVC denials correlated by context | **Captured in effect** — denial confirmed from the consuming side on every consumer; superseded as a diagnostic by the direct label and access observations |
| 4 | Recursion scope on the affected node — label histogram | **Captured** — uniform `data_t:s0:c20,c476` across the tree; full recursive relabel |
| 4b | Reference label histogram from a **healthy** node | **Captured** — `ip-10-0-37-12`: 154 files, 49 directories, all `system_u:object_r:local_t:s0` (F4) |
| 5 | Mount-scoped or on-disk (D1) | **Captured — on-disk.** `seclabel`, no `context=` |
| 6 | Per-node label comparison across the pool | **Captured** — all 8 nodes `local_t:s0` with successful reads at end state |
| 7 | Which volumes relabel on this stack (D2) | **Captured — the `seclabel` rule** (F2), plus `CSIDriver.seLinuxMount=false` for the four drivers in use |
| 8 | Whether a pass repairs or re-poisons labels (D5) | **Captured — no category inheritance** (F4) |
| 9 | Whether a relabel repair is possible on Bottlerocket | **Captured — denied** (F1) |
| 10 | In-pod path of a PV-based versus `hostPath` data store | **Captured** (F3) |
| 11 | First-pass timing on a fresh node | **Captured** (F8) |

Host-level observations were taken through `kubectl debug node/<node> --profile=sysadmin` with an
`amazonlinux:2023` image, and pod-level labels through each node's Path Shim pod where the tree was
readable. Tooling limits in that debug image are recorded under F1 and matter for anyone repeating this.

**Re-run forms.**

```bash
# Label histogram and inode count for a tree (host, via kubectl debug node/... --profile=sysadmin)
find /host/mnt/geodata -printf '%Z %y\n' | sort | uniq -c | sort -rn
df -i /host/mnt/geodata

# Per-node label, from each Path Shim pod where the tree is readable
for pod in $(kubectl get pods -n geodata-sync -l app=geodata-path-shim \
               -o jsonpath='{range .items[*]}{.metadata.name}{" "}{end}'); do
  node=$(kubectl get pod -n geodata-sync "$pod" -o jsonpath='{.spec.nodeName}')
  printf '%s\t' "$node"
  kubectl exec -n geodata-sync "$pod" -- ls -ldZ /mnt/geodata 2>&1 | tail -1
done

# The seclabel discriminator (F2) — per mount, from a pod or the host
grep -E 'geodata|gisdata' /proc/mounts

# Whether a CSI driver can even take -o context=
kubectl get csidriver -o custom-columns='NAME:.metadata.name,SELINUXMOUNT:.spec.seLinuxMount'

# PV state (F9)
kubectl get pv geodata-local-pv \
  -o jsonpath='phase={.status.phase}{"\n"}claimRef={.spec.claimRef.namespace}/{.spec.claimRef.name}{"\n"}path={.spec.local.path}{"\n"}' \
  2>/dev/null || echo absent
```

### Recovery for already-affected nodes

**Node replacement is the primary and only verified procedure.** Label repair is demoted to an
unverified fast path, because it is **denied on Bottlerocket 1.64.0 from a privileged debug container**.

#### F1 — why repair is not the procedure

From `kubectl debug node/<affected> --profile=sysadmin` with an `amazonlinux:2023` image:

```
chcon -R system_u:object_r:local_t:s0 /host/mnt/geodata
```

The command walked the entire tree — it could **read** every filename, which independently confirms that
a privileged context bypasses MCS — but **every** `setxattr` returned `Permission denied`. Reading a
labeled object and relabeling it are governed by different permissions (`relabelfrom` and `relabelto`),
and Bottlerocket's policy does not grant them to that context. Read access proves nothing about write
access to the label.

Tooling notes for anyone repeating this, because several of them cost time:

- `chcon` is present, from coreutils.
- `matchpathcon` and `restorecon` are **absent** — policycoreutils is not shipped in that image. So the
  "let policy decide the target label" instrument is not available there either.
- `mount` is absent from the minimal image.
- `id -Z` fails, because `/sys/fs/selinux` is not in the debug container's mount namespace and that is
  what libselinux probes. hostPath-mounting `/sys/fs/selinux` into a privileged pod is a plausible
  workaround; it was **never tested**.

#### Primary procedure — node replacement

Verified end to end on this cluster. It needs no relabel permission, no privileged container, and no
host setting change. It works precisely because the mirror is **derived from an authoritative source**,
which is the premise of the node pool: discarding a node's copy costs a resync, not data.

1. Cordon the affected node.
2. **Relocate its ArcGIS workloads.** Cordon alone does not evict anything; without this step the broken
   node keeps serving requests it cannot satisfy.
3. Let the evicted warm-capacity placeholder force Karpenter to provision a replacement. This is the
   mechanism, so warm capacity has to be in place (3.9).
4. Wait for the new node to complete its first sync and clear `data-not-ready`. First pass on this
   cluster took 58.9 minutes for ~840 GiB (F8) — budget for it rather than assuming the `RESULTS.md`
   figure.
5. Confirm the replacement: label, mirror size, published generation and count, taint cleared.
6. Delete the old NodeClaim. The poisoned labels go with the instance store.

**Verified outcome.** Replacement `ip-10-0-39-129` came up `local_t:s0`, mirrored 840 GiB, published
`gen=g57a70ff5b429 count=3` matching the healthy node, and cleared the taint. `NodeClaim
geodata-nodes-ks8w4` was then deleted, and the poisoned labels went with the instance store.

**Prerequisite, unchanged.** Stop the re-poisoning source first (1.6). Replacing a node while a
relabeling consumer can still start means the replacement is poisoned in turn. Deregistration of the
data store, deletion of the ArcGIS-owned PVC, and removal of the PV all precede replacement.

**Blast radius.** One node. Its ArcGIS workloads move, its mirror is discarded and re-derived from the
authoritative source, and no other node is touched. The cost is one first sync — roughly an hour here —
and the FSx read that goes with it, which is concurrent with live reads from the remaining node.

**Post-recovery confirmation, once.** After the replacement's first sync completes, confirm the tree's
label and a successful read from the Path Shim pod. No recurring re-check is needed: F4 establishes that
new inodes do not take the writing process's categories, so a clean tree stays clean.

#### Demoted fast path — label repair (UNVERIFIED, denied here)

Retained only so the option is documented and so nobody rediscovers the denial from scratch. **Denied on
Bottlerocket 1.64.0 from a privileged debug container.** Do not put it in a runbook as a working step.

If a future Bottlerocket release, or a host-side context with `relabelto`, makes it possible, the shape
would be: quiesce the re-poisoning source, capture the reference label histogram from a healthy node
(F4: one uniform label here, so no split by filetype is needed), take an explicit typed operator
confirmation of the node name, then relabel to that reference and verify by histogram match rather than
by mere absence of categories — a `data_t:s0` tree has no categories and is still wrong. Cost is not the
obstacle: `IUsed 174` for 840 GB means about 174 `setxattr` calls (F5). Permission is the obstacle.

### Documentation changes

Rewrite around the durable lessons: the `seclabel` rule, category-free as the shareable state, the
relabel being on disk and outliving its consumer, category assignment being per pod sandbox, and node
replacement as the recovery. This **replaces** the shared-MCS-level guidance rather than sitting beside
it (2.10).

`README.md`

- Delete the "Required shared SELinux level" section entirely, including the ArcGIS Deployment patch
  script and the rollout-wait loops, and remove the entry from the table of contents.
- Delete the `selinux.sharedLevel` row from the "Important defaults" table.
- Delete the shared-level paragraph under "Upgrade safely" and its cross-reference, keeping the
  remaining upgrade guidance intact.
- Add a section, roughly where the removed one sat, covering: the `seclabel` rule as the actual test for
  whether a volume gets relabeled, with the observed `xfs rw,seclabel` versus `nfs4` mount lines; that a
  category-free object label is what lets consumers with different MCS categories share one tree,
  evidenced by the surveyed pods reading successfully with distinct category pairs; that the correct
  response is to ensure nothing relabels the tree, not to align categories; and that aligning ArcGIS
  categories is not available here because ArcGIS pod specifications cannot be changed. State plainly
  that `chmod 777`, disabling SELinux, and privileged consumers do not help, because mode bits do not
  override an MCS mismatch.
- Add the NFS evidence: six ArcGIS pods with six distinct MCS pairs, two co-located, all sharing
  `ontap-gisdata-pvc` (`ReadOnlyMany`, NFS) and all healthy. Add the `ReadWriteOnce`-EBS caveat as
  explicitly unverified: probably relabeled, does not bite because there is a single consumer, check
  `seclabel` before sharing one.
- Update the `datasetRoot` row and the architecture diagram: both arrows point at the one `hostPath`
  data store, and the local-PV branch is gone.
- Replace the recovery guidance with **node replacement** as the procedure — cordon, relocate ArcGIS
  workloads, let the evicted placeholder trigger Karpenter, wait for the first sync and taint clear,
  delete the old NodeClaim — and record that relabel repair is denied on Bottlerocket 1.64.0 from a
  privileged debug container. State plainly that removing the mount, the PVC, and the data store
  registration does not restore access on an already-affected node.
- Remove the "Geocoding local PersistentVolume" registration section, the two-stage install sequencing
  that exists only for it, and the "Before disabling the local volume" section, replacing them with the
  shared `hostPath` registration for both workloads.
- Add a troubleshooting note for F3: a PV-based folder data store appears in-pod at
  `/arcgis/volumes/pvc/<derived>/<registered-path>`, with `<derived>` taken from the CSI volume handle
  and the literal `noid` when there is none, so a local PV at `/mnt/geodata` appears as
  `/arcgis/volumes/pvc/noid/mnt/geodata`; a `hostPath` store appears at the host path itself. Looking for
  the host path inside a PV-backed pod and concluding the mount is missing is a live trap.
- Add the progress-observation notes from F7: `du -sh` overstates progress because rclone preallocates
  (`703G` disk versus `569G` apparent on the same tree), so `du --apparent-size` is the honest measure,
  and in-flight work is visible as `*.partial` files.

`RESULTS.md`

- Do not delete the original measurement. Add the conditions under which it was taken, and add the
  counter-measurement from the replacement node so the headline figure is not read as a guarantee:
  `Pass complete: ... 901830389268 bytes transferred, 901830740761 bytes on disk, 3533.9s total` — 58.9
  minutes for ~840 GiB, about 255 MB/s, while the FSx source was concurrently serving live reads from a
  second node and the payload was dominated by 18–35 GB locator archives. The existing table advertises
  roughly 500 GB in about five minutes; both numbers are real, and the difference is the conditions.

`tuning/gotchas.md`

- Replace the "Every pod mounting the shared hostPath must use one SELinux MCS level" rule. It is the
  wrong diagnosis: it describes the mechanism as symmetric across volume types and prescribes an action
  that is not permitted here.
- Add: **A volume is relabeled only if the plugin supports SELinux and the filesystem carries
  `seclabel`.** `hostPath` fails the first test; NFSv4.1 without `security_label` fails the second, and
  presents one fixed context for the whole mount. Check `/proc/mounts` for `seclabel`, and
  `CSIDriver.seLinuxMount` for whether `-o context=` is even available. One relabeling consumer redefines
  the tree's label for every other consumer on that node, and mode 0755 root:root does not help.
- Add: **A category-free label is the shareable state.** Any `container_t` process can access an object
  with no MCS categories, which is why consumers with unrelated category pairs coexisted for months.
- Add: **Distinct paths are necessary but not sufficient.** Labels live on inodes, so a bind mount, hard
  link, or symlink that exposes one tree under two paths exposes it to one relabel. Chart validation is
  lexical and cannot see this.
- Add: **The relabel is on disk and outlives the consumer.** The `local` plugin writes per-inode labels
  with `setxattr`; it does not use a `context=` mount option. Deregistering the data store, deleting the
  PVC, and removing the pod do not restore access. The discriminator is `seclabel` versus `context=`.
- Add: **The relabel changes the type as well as the level.** Healthy nodes carry
  `system_u:object_r:local_t:s0`; the relabeled node carried `system_u:object_r:data_t:s0:c20,c476`.
  Clearing categories alone leaves the wrong type.
- Add: **You cannot relabel it back on Bottlerocket.** `chcon -R` from a `--profile=sysadmin` debug
  container reads the whole tree and fails every `setxattr` with `Permission denied`; `restorecon` and
  `matchpathcon` are not even present in the `amazonlinux:2023` debug image. **Replace the node.** It
  works because the mirror is derived from an authoritative source.
- Add: **New files do not inherit the writer's categories.** The agent runs with categories and writes
  category-free files, so a clean tree stays clean and one post-sync confirmation is enough.
- Add: **MCS categories are per pod sandbox, not per service.** Two pods of one ReplicaSet on one node
  hold different pairs, so nothing can be aligned by service, Deployment, or ReplicaSet.
- Add: **Readiness labels are an inventory, not a health signal.** A node advertised the correct
  generation and count with no taint while its tree was unreadable, and every pod on it reported ready.
  Verify access as the consuming identity.
- Keep the existing rules on organization-wide `hostPath` registration scope, `hostPath.type: Directory`
  not creating paths, `allowServicesRestart`, and local-volume path immutability. Extend the last one
  with F9: a `Released` PV with a stale `claimRef` never rebinds and `helm upgrade` does not reconcile
  it; delete it explicitly, and note that `Retain` means deleting the object touches no node data.

`tuning/nvme.md`

- The line stating that the path appears in the DaemonSet, the shim, and the local PersistentVolume is
  wrong now. The local PersistentVolume is gone; update it to the single-`hostPath` model.

`charts/node-local-data/templates/NOTES.txt`

- Covered under the change set above. The install notes must not print an SELinux level requirement and
  must not tell operators to apply anything to ArcGIS workloads.

### Rollback of the prior session's shared-MCS-level changes

Satisfies requirements 2.11 and 2.12. `charts/`, `tests/`, `examples/`, and `my-values.yaml` are
untracked, so `git checkout` cannot revert them; `README.md` and `tuning/gotchas.md` are tracked but
carry large unrelated Helm-conversion changes that must not be wholesale-reverted. Every removal below
is a targeted edit. Verified by search: `examples/` contains no `selinux` references, so the examples
rely on the chart default — which means the schema `required` entry and the values default must be
removed in the same change, or rendering the examples breaks.

| File | Edit |
|---|---|
| `charts/node-local-data/values.yaml` | Remove the `selinux:` block and its six-line comment. |
| `charts/node-local-data/values.schema.json` | Remove `"selinux"` from `required`, and remove the `selinux` property object including its `sharedLevel` pattern and description. |
| `charts/node-local-data/templates/_validate.tpl` | Remove the `$sharedCategories` split and the distinct-MCS-category `fail`. |
| `charts/node-local-data/templates/07-sync-agent-daemonset.yaml` | Remove the pod-level `securityContext.seLinuxOptions` block and the two-line comment above it. |
| `charts/node-local-data/templates/08-path-shim.yaml` | Remove the pod-level `securityContext.seLinuxOptions` block and the two-line comment above it. |
| `charts/node-local-data/templates/NOTES.txt` | Remove the "SELinux requirement for every pod that mounts ..." block and the paragraph directing operators to apply it to ArcGIS workloads by admission mutation. |
| `charts/node-local-data/Chart.yaml` | The `0.1.1` bump existed only for the rejected change. Set `version: 0.2.0` for this fix rather than returning to `0.1.0`, because the rendered resource set changes. Record the reasoning in the commit message. |
| `tests/chart/test_render.py` | Remove the `shared_level` local and the two pod `securityContext` assertions; remove `assert "level: s0:c1022,c1023" in notes` and `assert "admission mutation before pod creation" in notes`; remove the three `selinux.sharedLevel` cases from the schema parametrize list, two schema-pattern cases and one distinct-category validation case. |
| `README.md` | The three edits listed under [Documentation changes](#documentation-changes): the "Required shared SELinux level" section plus its table-of-contents entry, the defaults-table row, and the "Upgrade safely" paragraph. Leave every unrelated Helm-conversion change untouched. |
| `tuning/gotchas.md` | Replace the shared-MCS-level rule paragraph as described above. Leave every other rule untouched. |
| `my-values.yaml` | Remove the `selinux:` block and the two-line comment above it. |

One further reason the rollback is correct rather than merely required: per-pod-sandbox category
assignment (root cause 6) means a pinned shared level would have had to reach **every replica of every
service**, including replicas created later by ArcGIS tooling, on every node. Two pods of one ReplicaSet
already hold different pairs today. Any single miss re-breaks the node, so the approach was not just
disallowed by constraint 2.7 — it had no reachable success state.

Sequencing: the schema `required` removal, the schema property removal, and the `values.yaml` default
removal must land together, otherwise an intermediate state fails to render the examples and breaks 3.8
at a commit boundary. Run `helm lint --strict`, `helm template` against both example values files,
`./scripts/check-placeholders.sh`, and the chart suite after the rollback and before any new behavior
is added, so the rollback is verified independently of the fix.

## Testing Strategy

### Validation Approach

Two phases. First, surface counterexamples that demonstrate the bug on unfixed code. Then verify the fix
holds for the bug condition and that everything outside the bug condition is unchanged.

A hard limit shapes this, and it is now a scoping decision rather than a caveat: **a true relabel is out
of scope for automated tests.** Reproducing one needs an SELinux-enforcing Bottlerocket node, a
relabeling volume, and a real pod, and — because the label is on disk (D1) and cannot be relabeled back
(F1) — a test that triggers it costs a node. The chart suite proves only that the configuration
expressing the bug is unrepresentable. The runtime behavior is covered by the **recorded evidence**: the
V1 confirm signals, the resolved determinations D1, D2 and D5, and the verified node replacement. State
this split in the test files rather than leaving it implicit, so nobody reads a green chart suite as
proof the relabel is fixed.

**Teardown for any future live relabel experiment is node replacement, not `chcon`.** Relabeling back is
denied (F1). Any such experiment must be scoped to a node that can be discarded, must never touch the
mirror on a node that is serving, and must budget for a first sync on the replacement (F8).

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples on the unfixed configuration. The root cause is confirmed and every
gating determination is resolved; what remains automatable is the chart and agent behavior.

**Test Plan**: Render the current chart with the overlapping configuration and observe that it succeeds
when it should fail. Reproduce the enumeration crash against a denied directory. The live observations
are recorded under [Evidence capture](#evidence-capture) and are not re-run as tests.

**Test Cases**:

1. **Overlap renders silently**: `helm template` with `localVolume.enabled=true` and the default
   `datasetRoot`, asserting that the `local` PV path and the Path Shim `hostPath` are the same string
   and that rendering nonetheless succeeds. Fails to fail on unfixed code — this is the counterexample
   for requirement 1.1, and it becomes the fixed-behavior test (now as a legacy-key rejection).
2. **Agent crashes rather than degrades**: call `list_datasets` against a directory that raises
   `PermissionError`, and assert the current code propagates it. Then assert that `main()`'s call site
   has no handler, so the pod dies. Reproduces 1.5 as a unit test with no SELinux needed, using a mode-
   or monkeypatch-induced denial rather than a real MCS mismatch.
3. **Readiness freezes on crash**: with the agent crashing, assert that neither `sync_pass` nor
   `reconcile_node_state` runs, so the taint and labels are never reconciled. This is the specific
   silent regression the optional hardening prevents.
4. **rclone output is swallowed** (F7): assert that the current `run()` buffers stderr, so nothing is
   logged until the pass returns. This is the counterexample for the streaming change.
5. **Edge case — no relabeling volume configured**: confirm the rendered routing-only resource set works
   and no relabel is possible. Passes on unfixed code, and is the baseline the preservation tests lock
   in.

**Out of scope for automated tests, covered by recorded evidence:**

- The relabel itself, and the AVC denials that accompany it.
- The V1 publication and the label-unchanged observation across all 8 nodes.
- The reference label histogram and the no-inheritance finding (F4).
- The `seclabel` rule observations, including the NFS multi-category sharing proof (F2).
- The recovery procedure — verified once, end to end, by node replacement.

**Expected Counterexamples**:

- A chart render that should be rejected and is not, with both paths identical.
- `PermissionError: [Errno 13]` out of `list_datasets`, propagating to process exit.
- No log output from a long-running pass until it completes.
- **Already observed:** the affected node's label unchanged after the data store was deregistered, the
  PVC deleted, and every referencing pod removed — the counterexample to mount-scoped labeling, and to
  "removing the consumer is the repair".
- **Already observed:** a type difference, not only a level difference, between healthy nodes (`local_t`)
  and the affected node (`data_t`) — the counterexample to a categories-only repair.
- **Already observed:** two pods of one ReplicaSet on one node holding different category pairs — the
  counterexample to any per-service label alignment.
- **Already observed:** `chcon -R` reading the whole tree and failing every `setxattr` — the
  counterexample to repair-in-place, and the reason replacement is the procedure.
- **Already observed:** a node advertising a correct generation, count, and no taint while unreadable,
  with every pod on it ready — the counterexample to readiness-as-health.
- Possible causes already excluded: mode bits, ownership, missing path, and `hostPath.type`, all ruled
  out by the 0755 root:root directory and the successful readers on other nodes; and mount-scoped
  labeling, excluded by `/proc/mounts` and by the label's persistence.

### Fix Checking

**Goal**: Verify that for all inputs where the bug condition holds, the fixed configuration produces
the expected behavior.

**Pseudocode:**

```
FOR ALL input WHERE isBugCondition(input) DO
  -- Phase 1: the configuration that expresses this state must be unrepresentable
  ASSERT renderChart(valuesOf(input)) = FAILURE
     AND failureMessage EXPLAINS why a relabeling volume cannot share the routing tree
     AND (legacyKeyPresent(input) OR failureMessage NAMES relabelingPathOf(input))

  -- Phase 2: the node state itself must be unreachable under the selected model
  state := applyFix(input)
  ASSERT NOT isBugCondition(state)
  ASSERT labelHistogram(sharedRootOf(state)) MATCHES referenceHistogram(healthyNode)
  -- Category-free is necessary and not sufficient: the type moved too.
  ASSERT FOR ALL c IN routingConsumers(state): canRead(c, sharedRootOf(state))
  ASSERT canRead(geocodingConsumer(state), sharedRootOf(state))
  ASSERT canCreateAndStat(pathShim, sharedRootOf(state))
  ASSERT canEnumerateAndSync(syncAgent, routingWriteRootOf(state))
END FOR
```

Phase 1 is fully covered by the chart suite. Phase 2's label and access assertions require an enforcing
node and are covered by the V1 confirm signals and the verified replacement: the label histogram from
`ip-10-0-37-12`, the all-8-nodes `local_t:s0` observation with successful reads, the geocode pod reading
as `container_t:s0:c40,c511`, and `ip-10-0-39-129` reaching `gen=g57a70ff5b429 count=3` with the taint
cleared. The mapping between each assertion and its evidence artifact is recorded in this document.

### Preservation Checking

**Goal**: Verify that for all inputs where the bug condition does NOT hold, the fixed configuration
produces the same result as the original.

**Pseudocode:**

```
FOR ALL input WHERE NOT isBugCondition(input) DO
  ASSERT renderChart_original(valuesOf(input)) = renderChart_fixed(valuesOf(input))
     MODULO { removedLocalVolumeResources, chartVersion, removedSELinuxSecurityContexts, notesText }
  ASSERT syncSemantics_original(input) = syncSemantics_fixed(input)
  ASSERT readinessReconciliation_original(input) = readinessReconciliation_fixed(input)
  ASSERT registeredRoutingPath_fixed(input) = "/mnt/geodata"
  ASSERT arcgisPodSpecs_fixed(input) = arcgisPodSpecs_original(input)
  ASSERT enumerationOutcome_fixed(readableSingleRoot) = enumerationOutcome_original(readableSingleRoot)
END FOR
```

The `MODULO` set is the intended difference and must be enumerated explicitly rather than left as
whatever the diff happens to show, or the preservation check degenerates into asserting the new
behavior. Every other rendered field must be byte-identical.

**Testing Approach**: Property-based testing suits preservation here because the input domain is
values-file configurations, which generate cheaply and cover far more of the space than the current
example-based cases. Generate `datasetRoot` values below `/mnt/`, node-pool variations, and legacy-key
presence, then assert the rendered routing resources are unchanged. Keep the existing example-based
tests as readable anchors; property tests catch the normalization edge cases the examples will not.

**Test Plan**: Observe behavior on the unfixed configuration for every non-overlapping input first —
render output, sync semantics, readiness reconciliation — then write tests that capture exactly that
behavior and run them against the fix.

**Test Cases**:

1. **Routing-only render preservation**: capture the full unfixed render with `localVolume.enabled=false`
   and assert the fixed render is identical except for the enumerated `MODULO` set. Locks in 3.2.
2. **Registered routing path preservation**: assert the Path Shim `hostPath`, the agent's routing
   `hostPath`, the agent's `--dest-root`, the bind target, and the NOTES registration path are all
   still exactly `/mnt/geodata`. Locks in 3.5 and prevents a silent forced re-registration.
3. **Sync semantics preservation**: assert `--metadata`, no `--inplace`, the excludes, the concurrency
   flags, and the single recursive `rclone sync` invocation shape are unchanged. Streaming stderr must
   not change the argv. Locks in 3.3.
4. **Readiness reconciliation preservation**: assert the generation digest and count for a given set of
   top-level names are unchanged, and that the read-before-write behavior in `write_generation_label`
   still suppresses identical writes. Locks in 3.3 and prevents reintroducing the per-interval write
   storm.
5. **NodePool and EC2NodeClass identity preservation**: assert every field is byte-identical, so no
   NodeClaim is marked Drifted. Locks in 3.7 and 3.9.
6. **No SELinux options anywhere**: assert no rendered pod or container carries `seLinuxOptions`, and no
   container is privileged. Locks in 3.6 and the rollback.
7. **Optional-hardening neutrality**: for a single readable root, assert the enumeration outcome, digest,
   count, taint transitions, and exit status match the unhardened agent exactly. This is the assertion
   that makes the hardening safe to keep.

### Unit Tests

- `pathsOverlap` normalization: identical paths; trailing separator; duplicate separators; `..`
  segments; true child in both directions; sibling with a shared prefix (`/mnt/geodata` versus
  `/mnt/geodata-geocoding`) accepted as non-overlapping.
- Legacy-key rejection: a values file carrying `localVolume` fails rendering with the explanatory
  message rather than an unknown-property error.
- `datasetRoot` outside `/mnt/` fails rendering, preserving the existing rule.
- Agent enumeration: unreadable directory yields an explicit unreadable outcome, not an empty list;
  empty directory still yields empty; an unreadable root does not raise, does not change the bootstrap
  taint, and does not alter the generation or count labels; a readable single root behaves exactly as
  today.
- rclone stderr streaming: output reaches the log before the process exits, and the one-line stats
  parsing still matches.
- `generation_value` and `write_generation_label` unchanged for unchanged inputs.

### Property-Based Tests

- Generate non-overlapping path pairs and assert `pathsOverlap` is false; generate overlapping pairs by
  construction (equal, child, grandchild, denormalized forms) and assert it is true. This is where the
  normalization bugs live.
- Generate values-file configurations across the non-bug domain and assert the routing resources render
  identically to the unfixed chart, modulo the enumerated difference set.
- Generate top-level dataset name sets and assert the generation digest and count are stable and
  order-independent, matching current behavior.

### Integration Tests

- Full chart lifecycle: `helm lint --strict`, `helm template` for both example values files,
  `./scripts/check-placeholders.sh`, and `kubectl apply --dry-run=server` for the rendered output.
- PersistentVolume lifecycle: confirm `geodata-local-pv` is absent after the upgrade. A `Released` PV
  with a stale `claimRef` surviving the change is a failure of the operational sequence, not a transient
  (F9).
- Live, recorded as evidence rather than automated: the V1 publication and query with the label
  re-checked afterwards across all 8 nodes; a geocoding pod restart cycle with routing readiness and
  agent passes observed throughout, covering 2.6; and the node replacement procedure, already verified
  end to end on `ip-10-0-39-129`.
- Node replacement: replace one pool node and confirm it reaches readiness, publishes matching
  generation and count labels, clears the taint, and comes up `local_t:s0`. This doubles as the recovery
  procedure's regression test and as the teardown for any live experiment.
