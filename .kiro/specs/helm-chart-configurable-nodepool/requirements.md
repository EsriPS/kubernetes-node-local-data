# Requirements Document

## Introduction

This feature packages the Kubernetes deployment as a Helm chart. The chart makes
environment-specific settings, NodePool selection, capacity values, warm
capacity, and sync-agent tuning configurable through an ordinary Helm values
file.

The design intentionally follows normal Helm behavior:

- static defaults live in `values.yaml` and are visible through
  `helm show values`;
- optional behavior uses explicit booleans, not absent/null/assigned semantics;
- every rendered object remains part of the Helm release and uninstall requests
  its deletion;
- templates do not query the cluster or attempt to predict upgrade drift;
- Kubernetes and Karpenter report missing APIs, conflicting objects, and drift;
- storage capacities and Karpenter requirements are explicit operator values,
  not values inferred through chart arithmetic.

The ONTAP export is the authoritative dataset. The sync agent mirrors that
export directly into `datasetRoot`, including deletion of destination content
that no longer exists on ONTAP. `datasetRoot`, `/mnt/geodata` by default, is also
the EC2 bind target, local PV path, Path Shim-created path, ArcGIS hostPath
registration path, readiness and emptiness root, and path rendered in NOTES. The
mirror is updated with `rclone sync <source>/ <datasetRoot>/` without `--inplace`:
an individual destination file is replaced only after its new content has
transferred, but the directory tree is not presented as one atomic snapshot.
Operators that require cross-file atomicity must update the source and roll or
drain consumers as one operational change.

## Glossary

- **Chart**: `charts/node-local-data`.
- **Source volume**: the static NFS PersistentVolume and claim read by the sync
  agent.
- **Local volume**: the optional local PersistentVolume read by geocoding
  services.
- **Path shim**: the optional DaemonSet that creates the host path needed by the
  routing folder data store on every node.
- **Warm capacity**: the optional PriorityClass and Deployment that cause
  Karpenter to keep dedicated nodes provisioned.
- **Dataset root**: the direct mirror destination, instance-store mount, EC2 bind
  target, local PV path, Path Shim-created path, ArcGIS hostPath registration
  path, readiness and emptiness root, and NOTES path; `/mnt/geodata` by default.
- **Chart documentation**: the repository README, chart NOTES, and operational
  guidance maintained for installation, upgrade, and uninstall.
- **Repository validation**: the mandatory chart, sync-agent, integration, and
  command checks used to accept the implementation.
- **Required validation environment**: the environment in which Repository
  validation executes, including the supported rclone executable.

## Requirements

### Requirement 1: The deployment is packaged as a conventional Helm chart

**User Story:** As a cluster operator, I want to configure and install the
solution with Helm so that I do not hand-edit or apply a sequence of manifests.

#### Acceptance Criteria

1. THE repository SHALL contain a Helm chart with `apiVersion: v2` at
   `charts/node-local-data`, compatible with Helm CLI 3.0.0 and later.
2. WHEN all optional components are enabled, THE Chart SHALL render fifteen
   resources: one Namespace, one ServiceAccount, one ClusterRole, one
   ClusterRoleBinding, one EC2NodeClass, one NodePool, one PriorityClass, one
   Deployment, two DaemonSets, two StorageClasses, two PersistentVolumes, and
   one PersistentVolumeClaim.
3. THE Chart SHALL expose values for every current placeholder: label domain,
   image repository, NFS server, NFS share name, NFS share path, AMI alias, node
   IAM role, subnet selectors, security-group selectors, and zones.
4. THE Chart SHALL render zero strings matching `__[A-Z0-9_]+__` when supplied
   with a valid values file.
5. THE Chart SHALL derive repeated names, labels, taints, selectors, the source
   volume handle, and `datasetRoot` through shared helpers so that each logical
   identifier has one source.
6. WHERE `namespace.create` is `false`, THE Chart SHALL omit the Namespace and
   render namespaced resources into `namespace.name`.
7. THE Chart SHALL use no Helm hooks and no `lookup` calls.

### Requirement 2: Values and validation are explicit and unsurprising

**User Story:** As a cluster operator, I want normal Helm defaults and concise
validation so that configuration behaves the same across supported Helm 3
versions.

#### Acceptance Criteria

1. THE Chart SHALL put every static default directly in `values.yaml`; it SHALL
   NOT depend on distinguishing an omitted value from an explicit null.
2. Optional resources SHALL use explicit boolean `enabled` values. Optional
   requirement entries SHALL be controlled by replacing or editing a list, not
   by assigning null.
3. THE values schema SHALL require non-empty environment-specific values that
   have no portable default, including label domain, image repository, NFS
   endpoint and share values, source-volume capacity, AMI alias, node IAM role,
   at least one subnet selector, at least one security-group selector, and at
   least one zone.
4. THE values schema SHALL validate booleans, integer bounds, enums, Kubernetes
   quantity syntax, DNS-compatible names, and list item shape where JSON Schema
   can express those constraints directly.
5. THE Chart SHALL use template `fail` only for material relationships that JSON
   Schema cannot express, limited to:
   - `datasetRoot` must be below `/mnt/` for the Bottlerocket bind;
   - `warmCapacity.replicas` must not exceed `nodePool.limits.nodes` when warm
     capacity is enabled;
   - an instance requirement must not redefine the chart-managed architecture,
     operating-system, zone, or capacity-type keys.
6. Validation messages need only identify the offending key and constraint; the
   Chart SHALL NOT implement recursive provenance tracking or custom messages
   for failures already reported by the values schema.
7. THE Chart SHALL perform no cluster-dependent prerequisite, ownership,
   retention, or drift validation. The documentation SHALL list Karpenter,
   `karpenter.k8s.aws/v1`, and `nfs.csi.k8s.io` as prerequisites and provide
   simple `kubectl` commands for checking them before installation.

### Requirement 3: NodePool instance selection is configurable through one list

**User Story:** As a cluster operator, I want to use measured resource floors or
pin instance types without learning chart-specific modes and null semantics.

#### Acceptance Criteria

1. THE Chart SHALL always render architecture, operating-system, zone, and
   capacity-type requirements from dedicated values.
2. THE Chart SHALL append `nodePool.instanceRequirements` to those managed
   requirements in the order supplied.
3. `values.yaml` SHALL default `nodePool.instanceRequirements` to the current
   measured floors, corrected to a `1200` GB local-NVMe floor:
   - categories `c`, `m`, `r`, and `i`;
   - generation greater than `7`;
   - CPU at least `8`;
   - memory at least `32768` MiB;
   - local NVMe at least `1200` GB;
   - network bandwidth greater than `16000` Mbps.
4. TO pin instance types, an operator SHALL be able to replace the default list
   with a `karpenter.k8s.aws/instance-type` `In` requirement. No separate mode
   value SHALL be required.
5. THE values schema SHALL constrain each supplied requirement to a non-empty
   key, one of `In`, `NotIn`, `Exists`, `DoesNotExist`, `Gt`, or `Lt`, and an
   appropriate values list. Because Karpenter supports `Gt` rather than `Gte`,
   an intended minimum of `N` SHALL be represented as `Gt` with `N - 1`.
6. THE Chart SHALL NOT attempt to determine whether operator-supplied
   requirements or instance types provide enough NVMe, CPU, memory, or network
   bandwidth. The documentation SHALL state that this is the operator's
   responsibility.
7. THE NodePool node and CPU limits SHALL be explicit values, defaulting to `3`
   and `144`; the Chart SHALL NOT derive one from the other.

### Requirement 4: Storage capacities are explicit

**User Story:** As a cluster operator, I want capacities expressed in the units
used by Kubernetes and Karpenter so that the chart does not hide storage-policy
assumptions in arithmetic.

#### Acceptance Criteria

1. THE Chart SHALL expose independent values for:
   - the source PV/PVC capacity, as a Kubernetes quantity;
   - the local PV capacity, as a Kubernetes quantity;
   - the Bottlerocket data-volume size, as a Kubernetes quantity;
   - the local-NVMe selection floor, as part of
     `nodePool.instanceRequirements` and expressed in GB as Karpenter expects.
2. THE Chart SHALL render the same source capacity into the source PV and PVC.
3. THE Chart SHALL default `datasetRoot` to `/mnt/geodata`.
4. THE Chart SHALL render `datasetRoot` as the EC2 bind target.
5. WHERE the local volume is enabled, THE Chart SHALL render `datasetRoot` as the
   local PV path.
6. THE defaults SHALL be `600Gi` for the local PV and `200Gi` for the
   Bottlerocket data volume. The documented environment example SHALL use
   `1536Gi` for the source volume.
7. THE Chart SHALL NOT derive a generation size, round a dataset size, or infer
   one capacity from another.
8. THE documentation SHALL explain that the local PV capacity is advisory for
   the host directory and that the selected instance must have room for the
   complete source mirror, transfer-temporary files, and operational headroom.
9. THE documentation SHALL explain that changing a static PV's immutable fields
   or capacity may require deregistering the ArcGIS data store, deleting the old
   PV after it is unbound, upgrading the release, and re-registering it.

### Requirement 5: The sync agent mirrors the ONTAP source

**User Story:** As a data operator, I want ONTAP to be the gold copy so that
adding, changing, or deleting source content is reflected on each node.

#### Acceptance Criteria

1. WHEN a sync pass begins, THE Sync Agent SHALL run one recursive
   `rclone sync <source>/ <datasetRoot>/`.
2. AFTER a successful pass, every included source path SHALL match the source
   according to rclone's size-and-modification-time comparison, and every
   destination path absent from the source SHALL have been deleted, subject to
   configured excludes. The documentation SHALL note that source publication
   must change size or modification time for a same-named file to be detected
   without checksum mode.
3. THE Sync Agent SHALL NOT use rclone's `--inplace` option. A changed file's
   existing destination path SHALL remain intact until the replacement file has
   completed transfer and is renamed into place.
4. THE Sync Agent SHALL mirror source content directly into `datasetRoot`
   without maintaining a second full staging tree. Temporary files created by
   rclone are permitted, are not mirrored dataset paths, and SHALL be reconciled
   by a later successful pass.
5. THE documentation SHALL state that file replacement is atomic per file but a
   pass is not an atomic whole-tree transaction; consumers can observe a mixture
   of old and new files while a pass is running.
6. WHILE the first full sync has not succeeded, THE Sync Agent SHALL retain the
   startup taint. IF `datasetRoot` is absent or empty, THEN THE Sync Agent SHALL
   apply the startup taint again.
7. AFTER each successful pass, THE agent SHALL update the existing
   dataset-generation and dataset-count node labels. The dataset-generation
   label SHALL be documented as an identity of top-level dataset names, not a
   checksum of file contents.
8. THE agent image, sync interval, transfer count, multi-thread stream count,
   excludes, memory, and termination grace period SHALL be configurable with
   ordinary defaults matching the current deployment.
9. THE ClusterRole SHALL continue to grant only `get`, `list`, `watch`, and
   `patch` on nodes.

### Requirement 6: Optional components use direct enablement and counts

**User Story:** As a cluster operator, I want to choose warm capacity and the two
ArcGIS integration resources independently.

#### Acceptance Criteria

1. `warmCapacity.enabled` SHALL default to `true`; when false, the Chart SHALL
   omit both the warm-capacity Deployment and PriorityClass.
2. `warmCapacity.replicas` SHALL be an explicit integer defaulting to `2`; it
   SHALL NOT be derived from the NodePool limit.
3. Warm-capacity pods SHALL use required hostname anti-affinity and the dedicated
   node's selectors and tolerations so each replica holds a distinct node open.
4. `pathShim.enabled` SHALL default to `true`; when false, the Chart SHALL omit
   the Path Shim DaemonSet.
5. WHERE the Path Shim is enabled, THE Chart SHALL configure the Path Shim
   DaemonSet to create `datasetRoot`.
6. `localVolume.enabled` SHALL default to `false`; when false, the Chart SHALL
   omit the local StorageClass and PV.
7. Post-install notes SHALL tell the operator to enable the local volume only
   after the intended nodes have completed their first sync.
8. Disabling one optional component SHALL not change unrelated rendered
   resources.

### Requirement 7: Installation, upgrade, and uninstall follow Helm semantics

**User Story:** As a cluster operator, I want lifecycle behavior to match normal
Helm expectations.

#### Acceptance Criteria

1. `helm install` SHALL create the resources enabled by the supplied values.
2. `helm upgrade` SHALL update those resources using Helm's normal three-way
   merge behavior.
3. Every rendered object SHALL remain managed by the Helm release, including the
   NodePool, EC2NodeClass, source and local storage objects, Path Shim, and
   warm-capacity objects. The Chart SHALL render no
   `helm.sh/resource-policy: keep` annotation.
4. WHEN an operator prepares to uninstall a release whose local PV is
   registered as an ArcGIS data store, THE Chart documentation SHALL instruct
   the operator to deregister the local-PV data store before invoking
   `helm uninstall`.
5. WHEN an operator prepares to uninstall a release whose routing hostPath is
   registered as an ArcGIS data store, THE Chart documentation SHALL instruct
   the operator to deregister the routing hostPath data store before invoking
   `helm uninstall`.
6. IF an ArcGIS-owned claim is bound to the local PV when an operator prepares
   to uninstall the release, THEN THE Chart documentation SHALL instruct the
   operator to delete the claim and verify that the claim no longer exists before
   invoking `helm uninstall`.
7. WHEN an upgrade changes `localVolume.enabled` from `true` to `false`, THE
   Chart documentation SHALL instruct the operator to deregister the local-PV
   ArcGIS data store before invoking `helm upgrade`.
8. IF an ArcGIS-owned claim is bound to the local PV when an upgrade changes
   `localVolume.enabled` from `true` to `false`, THEN THE Chart documentation
   SHALL instruct the operator to delete the claim and verify that the claim no
   longer exists before invoking `helm upgrade`.
9. WHEN an upgrade changes `pathShim.enabled` from `true` to `false`, THE Chart
   documentation SHALL instruct the operator to deregister the routing hostPath
   ArcGIS data store before invoking `helm upgrade`.
10. `helm uninstall` SHALL request deletion of every object rendered by the
    release. The Chart documentation SHALL explain that Kubernetes PV protection
    can leave a bound PV in `Terminating` until an external claim is removed; the
    Chart SHALL add no hook or force-deletion mechanism to bypass that protection.
11. StorageClasses and PVs SHALL retain their current `Retain` reclaim policies,
    but the Chart documentation SHALL clarify that reclaim policy does not keep
    an object installed during `helm uninstall`.
12. THE Chart SHALL contain no command or hook that deletes files from ONTAP or
    from node filesystems during uninstall.
13. THE Chart documentation SHALL warn that uninstalling the NodePool can
    terminate its nodes and therefore discard their instance-store copies;
    ONTAP remains the authoritative recoverable copy.
14. THE Chart SHALL preserve `expireAfter: Never`, `consolidateAfter: Never`,
    `consolidationPolicy: WhenEmpty`, and a zero voluntary-disruption budget.
15. THE Chart documentation SHALL explain that NodePool or EC2NodeClass changes
    can mark NodeClaims Drifted and that, with the zero disruption budget,
    operators review the rendered upgrade and replace NodeClaims deliberately.
    No custom drift report script or live-object comparison SHALL be required.

### Requirement 8: ArcGIS registration guidance uses rendered values

**User Story:** As an ArcGIS administrator, I want release-specific registration
values without searching generated YAML.

#### Acceptance Criteria

1. NOTES SHALL print the log and node-readiness commands using the rendered
   namespace, label domain, workload value, and resource names.
2. WHEN the local volume is enabled, NOTES SHALL print its access mode, capacity,
   StorageClass name, PV name, and `datasetRoot` path for Enterprise Manager.
3. WHEN the Path Shim is enabled, NOTES SHALL print `datasetRoot` as the routing
   hostPath registration path and SHALL print the ArcGIS node placement
   toleration and affinity.
4. NOTES SHALL explain the cold-start consequence when warm capacity is disabled.
5. NOTES SHALL contain no placeholder tokens and SHALL perform no cluster lookup.

### Requirement 9: Deployment source and validation are focused

**User Story:** As a maintainer, I want one deployment source and focused
validation so that the Helm chart remains maintainable and its behavior is
verifiable.

#### Acceptance Criteria

1. WHEN the Chart is implemented, THE repository SHALL delete the raw files
   under `manifests/`.
2. THE repository SHALL retain the Chart and documented example values as the
   only maintained deployment source, without generated manifest copies.
3. THE Repository validation SHALL require chart render tests that cover the
   documented example with the local volume disabled and enabled, the explicit
   instance-type requirements example, representative invalid values, resource
   counts, shared identifiers, storage capacities, NodePool safety settings,
   optional-resource isolation, and absence of placeholder tokens.
4. THE Repository validation SHALL require sync-agent behavior and readiness
   tests that cover source additions, modifications, deletions, configured
   excludes, interruption and retry without `--inplace`, direct-mirror contents,
   startup-taint removal, empty-root re-tainting, and dataset label updates.
5. THE Required validation environment SHALL provide rclone `1.75.0`, matching
   the version pinned by the supported sync-agent image, for every required
   real-rclone integration scenario.
6. WHEN the required real-rclone integration scenarios execute, THE Repository
   validation SHALL run the source-addition, source-modification,
   source-deletion, configured-exclude, and interrupted-pass retry scenarios
   with rclone `1.75.0`.
7. IF rclone `1.75.0` is absent or a different rclone version is detected in the
   Required validation environment, THEN THE Repository validation SHALL fail
   and report the version requirement instead of skipping a required
   real-rclone integration scenario.
8. THE Repository validation SHALL require successful completion of
   `helm lint --strict charts/node-local-data -f examples/values-example.yaml`,
   `pytest -q tests/chart agent/tests`, and
   `./scripts/check-placeholders.sh`.
9. THE Repository validation SHALL treat the chart render tests, sync-agent
   behavior and readiness tests, real-rclone integration scenarios, and final
   validation commands as mandatory implementation acceptance checks.
10. THE Chart validation SHALL omit requirements for exact source-comment
    preservation, documentation-table parsing, generated manifest drift tooling,
    property-based Helm subprocess testing, cluster-based chart tests, custom
    drift scripts, and exhaustive warning tests.
11. THE Chart documentation SHALL retain important operational rationale in the
    relevant template, README, or `tuning/` document without treating comment
    wording or order as an interface.
12. THE updated `RESULTS.md` SHALL preserve the historical test conditions,
    measured performance findings, and environment-specific qualification of
    those findings.
13. IF `RESULTS.md` identifies the raw files under `manifests/` as the current
    tested configuration or deployment source, THEN THE updated `RESULTS.md`
    SHALL remove that identification or qualify the identification as historical
    and no longer current.
14. WHEN `RESULTS.md` describes validation status after migration, THE updated
    `RESULTS.md` SHALL distinguish historical raw-manifest validation from
    validation of the Helm chart and direct-mirror sync-agent implementation.
