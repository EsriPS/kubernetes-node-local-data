# Implementation Plan: helm-chart-configurable-nodepool

## Overview

Implement the feature in five integrated work streams: simplify the sync agent,
create the chart, template the resources, migrate deployment guidance, and add a
focused validation suite. The sync agent mirrors the ONTAP source directly into
`datasetRoot`, and the same exact path is used by the EC2 bind, sync mount, local
PV, Path Shim, NOTES, tests, and documentation.

The chart and documented example values become the only maintained deployment
source. No task introduces three-state values, derived-size arithmetic, cluster
`lookup`, Helm keep annotations, generated manifest copies, a drift-report
script, or property-based Helm subprocess tests.

## Tasks

- [x] 1. Change the sync agent from append-only datasets to a direct mirror

  - [x] 1.1 Replace the per-dataset staging algorithm in `agent/sync_agent.py`
    - Invoke one `rclone sync <source>/ <datasetRoot>/` for each pass.
    - Preserve configured concurrency, statistics, and exclude arguments while
      relying on rclone's default size-and-modification-time comparison.
    - Do not pass `--inplace` or maintain a second full staging tree.
    - Remove staging-directory sweeping, pending-dataset selection, and
      top-level rename logic.
    - Treat a successful rclone exit as completion of the whole pass.
    - After success, derive dataset-generation and dataset-count labels from the
      top-level names directly in `datasetRoot` and remove the startup taint.
    - Reapply the startup taint when `datasetRoot` is absent or empty.
    - Update module and function documentation to describe source-driven
      deletion, per-file replacement, and the lack of whole-tree atomicity.
    - _Requirements: 5.1–5.7_

  - [x] 1.2 Update mandatory sync-agent behavior and real-rclone integration tests in `agent/tests/test_integration.py`
    - Replace append-only and staging expectations with source additions,
      modifications, deletions, configured excludes, and interrupted-pass retry.
    - Assert the command is exactly one recursive sync from the source root to
      `datasetRoot` and never contains `--inplace`.
    - Require exactly rclone `1.75.0` for every real-rclone scenario by replacing
      the module-wide skip with an executable/version preflight that fails with
      a clear installation or wrong-version error.
    - Run additions, modifications, deletions, configured excludes, and a failed
      interrupted pass followed by a successful retry with the real executable;
      do not assert rollback of files completed before failure.
    - Verify default size-and-modification-time detection assumptions and direct
      mirror contents.
    - _Requirements: 5.1–5.5, 9.4–9.7, 9.9_

  - [x] 1.3 Add mandatory focused tests for Design Property 3: successful syncs establish mirror readiness
    - Verify a successful direct sync updates dataset-generation and
      dataset-count from top-level names in `datasetRoot` and removes the
      startup taint.
    - Verify an absent or empty `datasetRoot` reapplies the startup taint.
    - Keep these as targeted example-based tests rather than generated
      property-based tests.
    - **Validates: Requirements 5.6–5.7, 9.4, 9.9**

  - [x] 1.4 Update non-chart agent image-version references
    - Update the agent build/push command examples and other non-chart version
      references in repository documentation, including `README.md`, to `0.5.0`
      because source-deletion behavior materially changes the agent contract.
    - Do not edit the chart values in this task; Task 2.2 exclusively owns the
      chart's default image tag.
    - _Requirements: 5.8_

- [x] 2. Create the chart interface

  - [x] 2.1 Create `charts/node-local-data/Chart.yaml`
    - Use `apiVersion: v2`, no chart dependencies, and a Kubernetes version
      range appropriate for the rendered APIs.
    - Keep templates compatible with Helm CLI 3.0.0 and later and add no hooks.
    - _Requirements: 1.1, 1.7_

  - [x] 2.2 Create `charts/node-local-data/values.yaml`
    - Implement the finalized values structure with every static default written
      directly in the file and only installation-specific required values empty.
    - Use exact `datasetRoot` semantics with `/mnt/geodata` as the default and
      set `image.tag: 0.5.0` as the chart-owned default.
    - Use explicit values for source capacity, local capacity, data-volume size,
      NodePool limits, warm replicas, and every instance requirement.
    - Default the local NVMe minimum to `1200` GB encoded as `Gt: 1199`, data
      volume to `200Gi`, local PV to `600Gi`, warm replicas to `2`, node limit to
      `3`, and CPU limit to `144`.
    - Default the remaining instance floors, optional-component booleans, and
      sync-agent tuning values to the finalized design values.
    - Include concise comments for operationally dangerous values, especially
      the pinned AMI, NVMe and network floors, disruption settings, and the
      prohibition on `--inplace`.
    - _Requirements: 1.3, 2.1–2.2, 3.3, 3.7, 4.1, 4.3, 4.6–4.7, 5.8, 6.1–6.6_

  - [x] 2.3 Create `charts/node-local-data/values.schema.json`
    - Require non-empty label-domain, image, NFS endpoint/share, source-capacity,
      AMI, node-role, selector-term, and zone values.
    - Validate native selector-term and Karpenter requirement shapes without
      introducing chart-specific modes.
    - Validate booleans, integer bounds, quantity syntax, names, label values,
      image pull policy, capacity types, and operator-specific values lists.
    - Use `additionalProperties: false` for chart-owned objects and do not model
      null as a meaningful value.
    - _Requirements: 2.3–2.4, 2.6, 3.5_

- [x] 3. Implement shared helpers and concise validation

  - [x] 3.1 Create `charts/node-local-data/templates/_helpers.tpl`
    - Derive resource names from `namePrefix` and resolve the release namespace.
    - Derive common labels and selectors, workload and component label keys,
      taint keys, dataset-generation and dataset-count keys, source/local storage
      names, the source volume handle, exact `datasetRoot`, and escaped JSONPath
      label keys.
    - Use the `datasetRoot` helper for the EC2 bind, sync destination mount,
      local PV path, Path Shim-created path, NOTES output, readiness checks, and
      emptiness checks.
    - Use helpers only where multiple resources or NOTES must agree.
    - _Requirements: 1.5–1.6, 4.4–4.5, 6.5, 8.1–8.3_

  - [x] 3.2 Create `charts/node-local-data/templates/_validate.tpl`
    - Fail when `datasetRoot` is not a non-root path below `/mnt/`.
    - Fail when enabled warm replicas exceed the NodePool node limit.
    - Fail when `nodePool.instanceRequirements` repeats architecture, OS, zone,
      or capacity-type keys managed by the chart.
    - Do not add recursive scans, quantity arithmetic, cluster checks, ownership
      checks, warning collectors, or values-provenance helpers.
    - _Requirements: 2.5–2.7, 3.1–3.2_

- [x] 4. Convert the Kubernetes resources to Helm templates

  - [x] 4.1 Template Namespace and RBAC resources
    - Gate the Namespace on `namespace.create` and use the resolved namespace on
      namespaced resources.
    - Preserve exactly `get`, `list`, `watch`, and `patch` for the node resource
      and grant no additional API access.
    - Retain a concise template comment describing the cluster-wide node-patch
      risk.
    - _Requirements: 1.2, 1.6, 5.9_

  - [x] 4.2 Template the EC2NodeClass
    - Render the role, AMI alias, subnet and security-group selector terms, data
      volume size, tags, and shared name.
    - Keep one essential bootstrap command entry with `init` before `bind`, and
      render exact `datasetRoot` as the bind target.
    - Preserve the rationale for the Bottlerocket `/mnt/` restriction and command
      ordering in concise template comments.
    - _Requirements: 1.3, 4.1, 4.4, 4.6, 9.11_

  - [x] 4.3 Template the NodePool
    - Render architecture, OS, zones, and capacity types first, followed by
      `nodePool.instanceRequirements` in supplied order.
    - Render explicit node and CPU limits without inferring either value.
    - Preserve shared workload labels, permanent and startup taints, NodeClass
      reference, `expireAfter: Never`, no node termination grace period,
      `consolidationPolicy: WhenEmpty`, `consolidateAfter: Never`, and a zero
      voluntary-disruption budget.
    - _Requirements: 3.1–3.7, 7.14_

  - [x] 4.4 Template warm capacity
    - Gate both the PriorityClass and Deployment on `warmCapacity.enabled`.
    - Render the explicit replica count and priority.
    - Preserve required hostname anti-affinity, dedicated-node selector and
      tolerations, and the low-resource pause container.
    - _Requirements: 6.1–6.3, 6.8_

  - [x] 4.5 Template source storage
    - Render one source StorageClass, PersistentVolume, and PersistentVolumeClaim.
    - Use the same `nfs.capacity` value in the PV and PVC.
    - Render configured mount options, CSI endpoint/share values, claim
      reference, and shared names.
    - Keep `Retain` reclaim policies and render no Helm keep annotation.
    - _Requirements: 4.1–4.2, 7.3, 7.11_

  - [x] 4.6 Template the sync-agent DaemonSet
    - Render the `0.5.0` agent image and configurable interval, transfer count,
      multi-thread stream count, excludes, memory, and termination grace period.
    - Mount the source read-only and mount exact `datasetRoot` as the direct sync
      destination.
    - Update template comments to state that `datasetRoot` is the direct mirror,
      source deletions propagate, and no second full staging tree exists.
    - Preserve scheduling, priority, security context, RBAC identity, and
      environment values.
    - _Requirements: 5.1–5.4, 5.8–5.9_

  - [x] 4.7 Template the Path Shim
    - Gate the whole DaemonSet on `pathShim.enabled`.
    - Create exact `datasetRoot` with the configured image.
    - Preserve scheduling, priority, and path-creation behavior without adding
      an uninstall command or hook that deletes node filesystem content.
    - _Requirements: 6.4–6.5, 6.8, 7.3, 7.12_

  - [x] 4.8 Template local storage
    - Gate the local StorageClass and PersistentVolume together on
      `localVolume.enabled`.
    - Render explicit capacity, access modes, exact `datasetRoot` as the local PV
      path, shared names, and pool affinity.
    - Keep `Retain` reclaim policies and render no Helm keep annotation.
    - _Requirements: 4.1, 4.5, 6.6, 6.8, 7.3, 7.11_

- [x] 5. Add release output, examples, and placeholder validation

  - [x] 5.1 Create `charts/node-local-data/templates/NOTES.txt`
    - Print rendered commands for agent logs and dataset-generation/count labels.
    - When local storage is disabled, print the follow-up upgrade command and
      first-sync prerequisite.
    - When local storage is enabled, print access mode, capacity, StorageClass,
      PV, readiness command, and exact `datasetRoot` for Enterprise Manager.
    - When the Path Shim is enabled, print exact `datasetRoot` as the routing
      hostPath plus the ArcGIS placement toleration and affinity.
    - When warm capacity is disabled, explain that the first workload waits for
      node provisioning and sync.
    - Remind operators that NodePool or EC2NodeClass changes can mark NodeClaims
      Drifted and require deliberate replacement.
    - Do not query the cluster or emit placeholder tokens.
    - _Requirements: 6.7, 7.15, 8.1–8.5_

  - [x] 5.2 Create documented example values
    - Create `examples/values-example.yaml` with tag-based selectors, one zone,
      `nfs.capacity: 1536Gi`, and no placeholder tokens.
    - Create `examples/values-instance-types.yaml` with all required environment
      values and replace the default floor list with one instance-type `In`
      requirement.
    - _Requirements: 3.4, 4.6, 9.2–9.3_

  - [x] 5.3 Simplify `scripts/check-placeholders.sh`
    - Render the documented example with Helm and fail if rendered manifests or
      NOTES contain a string matching `__[A-Z0-9_]+__`.
    - Keep the script independent of generated manifests and additional YAML
      parsing dependencies.
    - _Requirements: 1.4, 9.2–9.3, 9.8–9.9_

- [x] 6. Add focused automated validation

  - [x] 6.1 Create the mandatory chart test harness
    - Pin exact pytest and PyYAML versions in
      `tests/chart/requirements-dev.txt`.
    - Add one helper that invokes Helm once per test case and parses the rendered
      manifest stream and NOTES.
    - Do not add generated-input strategies, cluster fixtures, or a custom
      comparison framework.
    - _Requirements: 9.3, 9.9_

  - [x] 6.2 Add the mandatory focused render and validation matrix
    - Render the documented example with local storage off and on, the explicit
      instance-type example, and warm capacity disabled.
    - Render with `pathShim.enabled: false`; verify only the Path Shim DaemonSet
      is omitted and every unrelated rendered resource remains identical to the
      enabled baseline.
    - Render with `namespace.create: false`; verify no Namespace is rendered and
      every namespaced resource uses `namespace.name`.
    - Assert resource counts, optional-resource isolation, shared names and
      references, explicit EBS and local capacities, storage reclaim policies,
      absence of Helm keep annotations, replacement of default instance floors,
      bootstrap command order, NodePool safety settings, RBAC scope, NOTES
      gating, exact `datasetRoot` use, and zero placeholders.
    - Add representative schema failures and one case for each of the three
      `_validate.tpl` relationships.
    - Keep the suite example-based and small enough to run in seconds.
    - _Requirements: 1.2, 1.4–1.7, 2.3–2.6, 3.1–3.5, 4.1–4.6, 5.8–5.9, 6.1–6.8, 7.3, 7.11, 7.14, 8.1–8.5, 9.3, 9.9_

  - [x] 6.3 Add mandatory focused tests for Design Property 1: rendered identifiers and capacities remain consistent
    - Assert every helper-derived logical identifier agrees across rendered
      resources and NOTES.
    - Assert the source PV and PVC render the same configured capacity.
    - Keep these as focused examples in `tests/chart/test_render.py` rather than
      generated property-based subprocess tests.
    - **Validates: Requirements 1.5, 4.2, 9.3, 9.9**

  - [x] 6.4 Add mandatory focused tests for Design Property 2: dataset paths use the direct dataset root
    - Assert the EC2 bind target, sync destination mount, enabled local PV path,
      Path Shim-created path, readiness and emptiness roots, and NOTES paths all
      equal exact configured `datasetRoot`.
    - Test both the default and one overridden `datasetRoot` below `/mnt/`.
    - Keep these as focused examples in `tests/chart/test_render.py`.
    - **Validates: Requirements 4.4–4.5, 6.5, 8.2–8.3, 9.3, 9.9**

- [x] 7. Migrate maintained deployment content

  - [x] 7.1 Rewrite README installation, configuration, and lifecycle sections
    - Replace placeholder substitution and raw-manifest apply instructions with
      Helm install, staged local-volume upgrade, upgrade review, and uninstall
      commands.
    - Document Helm CLI 3.0.0 or later, prerequisites, ordinary defaults, the
      replaceable instance-requirements list, and the explicit-instance-type
      example.
    - Add a pre-upgrade procedure for changing `localVolume.enabled` from `true`
      to `false`: deregister the local-PV ArcGIS data store, delete any bound
      ArcGIS-owned claim, verify with `kubectl` that the claim no longer exists,
      and only then invoke `helm upgrade`.
    - Add a pre-upgrade procedure for changing `pathShim.enabled` from `true` to
      `false`: deregister the routing hostPath ArcGIS data store before invoking
      `helm upgrade`.
    - Add an explicit pre-uninstall procedure for a local PV bound to an
      ArcGIS-owned claim: delete that claim, verify with `kubectl` that it no
      longer exists, and only then invoke `helm uninstall`.
    - Document independent capacities, sizing responsibility, static-PV
      replacement steps, normal uninstall behavior, reclaim policy, PV
      protection, ArcGIS deregistration, and loss of node-local copies when
      NodePool nodes terminate.
    - Use exact `datasetRoot` for the EC2 bind, local PV, Path Shim hostPath,
      readiness, emptiness, and ArcGIS registration guidance.
    - Add simple `kubectl` prerequisite commands for Karpenter APIs and the NFS
      CSI driver and use workload value `geodata` consistently.
    - _Requirements: 1.1, 2.7, 3.4–3.6, 4.4–4.9, 6.7, 7.1–7.11, 7.13, 7.15, 8.2–8.4_

  - [x] 7.2 Rewrite sync and operational guidance
    - Describe ONTAP as the authoritative source and exact `datasetRoot` as its
      direct mirror, including propagation of source deletions.
    - Explain why `--inplace` is not used and distinguish per-file replacement
      from whole-tree atomicity.
    - Explain size-and-modification-time detection, rclone temporary-file space,
      operational headroom, retry behavior, and the fact that the
      dataset-generation label hashes top-level names rather than file contents.
    - Update `README.md`, `tuning/rclone.md`, `tuning/nvme.md`, and
      `tuning/gotchas.md` to remove staging, simultaneous full-generation, and
      append-only assumptions and use exact `datasetRoot` throughout.
    - _Requirements: 4.8, 5.1–5.7, 9.11_

  - [x] 7.3 Remove superseded deployment sources and update contributor commands
    - Delete all raw files under `manifests/` without creating generated manifest
      replacements.
    - Keep the Helm chart and documented examples as the only maintained
      deployment source.
    - Update `CONTRIBUTING.md` with a reproducible required-environment setup
      step that installs or provides exactly rclone `1.75.0`, verifies that
      version, and runs it before the mandatory focused pytest command.
    - Document that absent or wrong-version rclone is a validation failure, not
      a reason to skip the real-rclone scenarios.
    - Keep the mandatory strict lint, focused pytest, and placeholder-check
      commands together in contributor validation instructions.
    - Do not add drift-report scripts, generated manifest directories, or
      cluster-dependent test tooling.
    - _Requirements: 9.1–9.2, 9.5–9.10_

  - [x] 7.4 Update `RESULTS.md` for the deployment-source migration
    - Preserve the historical test conditions, measured performance findings,
      and their single-environment qualifications.
    - Replace claims that `manifests/` is the current tested configuration with
      an explicit statement that raw-manifest validation is historical and no
      longer validates the maintained deployment source.
    - Add a separate validation-status statement for the Helm chart and
      direct-mirror sync-agent implementation, without presenting historical
      performance measurements as qualification of the migrated implementation.
    - _Requirements: 9.12–9.14_

- [x] 8. Complete mandatory repository validation

  - [x] 8.1 Run every mandatory validation command and fix implementation failures
    - Provision and verify exactly rclone `1.75.0` using the documented required
      validation-environment setup; treat an absent or wrong version as a test
      failure with a clear version-requirement message.
    - Run `helm lint --strict charts/node-local-data -f examples/values-example.yaml`.
    - Run `pytest -q tests/chart agent/tests` without watch mode and confirm the
      mandatory real-rclone addition, modification, deletion, configured-exclude,
      and interrupted-pass retry scenarios execute rather than skip.
    - Run `./scripts/check-placeholders.sh`.
    - Render both documented examples and correct code or test failures involving
      resource sets, configured capacities, requirement lists, optional gates,
      exact `datasetRoot` paths, or placeholder absence.
    - _Requirements: 9.3–9.9_

## Notes

- Sync-agent behavior/readiness tests, chart harness/render/property tests,
  exact-rclone integration scenarios, and final validation are mandatory.
- Each task references current acceptance criteria for traceability.
- The three design properties are covered by focused example-based assertions;
  no generated property-testing framework is introduced.
- All runtime and operator-facing paths resolve to exact `datasetRoot`.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "2.1"] },
    { "id": 1, "tasks": ["1.2", "1.4", "2.2", "2.3"] },
    { "id": 2, "tasks": ["1.3", "3.1", "3.2"] },
    { "id": 3, "tasks": ["4.1", "4.2", "4.3", "4.4", "4.5", "4.6", "4.7", "4.8"] },
    { "id": 4, "tasks": ["5.1", "5.2", "5.3", "6.1", "7.1"] },
    { "id": 5, "tasks": ["6.2", "7.2"] },
    { "id": 6, "tasks": ["6.3", "7.3"] },
    { "id": 7, "tasks": ["6.4", "7.4"] },
    { "id": 8, "tasks": ["8.1"] }
  ]
}
```
