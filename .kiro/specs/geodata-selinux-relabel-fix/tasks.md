# Implementation Plan

## How to read this plan

V1 is resolved and the design is collapsed to Option 1: the relabeling volume type is removed from the
chart and geocoding is served from the same non-relabeling `hostPath` folder data store that routing
uses. There is no branch to plan around, and no decision gate remains.

**Almost all cluster work is already done.** The affected node was recovered by replacement, the local
PV and the ArcGIS-owned PVC are gone, and geocoding and routing are both republished and healthy
against the `hostPath` store. Those steps are recorded under [Already complete](#already-complete-do-not-re-run)
so the order stays legible and so nothing here re-runs them. The remaining work is repository work plus
one `helm upgrade`.

Order: task 1 rolls back the prior session's rejected changes and proves that green on its own. Tasks 2
and 3 write the exploration and preservation tests against the rolled-back but unfixed chart. Task 4
implements Option 1 and re-runs both. Task 5 adds the remaining test coverage. Task 6 is optional agent
work, skippable in whole or in part. Task 7 is documentation. Task 8 is the final validation checkpoint.

**Untracked-file constraint (2.12).** `charts/`, `tests/`, `examples/`, and `my-values.yaml` are
untracked, so `git checkout` cannot revert them. Every removal and edit in this plan is a targeted edit.
`README.md` and `tuning/gotchas.md` are tracked but carry large unrelated Helm-conversion changes and
MUST NOT be wholesale-reverted.

**Mirror constraint (3.1), visible on every task that touches the cluster: do not delete, move, or
re-sync the existing multi-hundred-GB mirror.** Nothing in this plan removes or relocates mirrored data
or forces a re-sync. `reclaimPolicy: Retain` means deleting a PV object touches no node filesystem
content.

**Mutation marker.** Any task that changes cluster state, node filesystem state, or ArcGIS
configuration is marked **[MUTATES — OPERATOR CONFIRMATION REQUIRED]** and states its blast radius. Only
one such task remains: the `helm upgrade` in 4.7 and its PV absence check.

**What the chart suite cannot prove.** A real SELinux relabel cannot be reproduced by `helm template`.
Reproducing one needs an enforcing Bottlerocket node, a relabeling volume, and a real pod — and because
the label is on disk (D1) and cannot be relabeled back (F1), triggering one costs a node. The chart suite
proves only that the configuration expressing the bug is unrepresentable. The runtime behavior is covered
by recorded evidence: the V1 confirm signals, D1, D2 and D5, and the verified node replacement.
**A green chart suite is never proof that the relabel is fixed.** This note must be written into the test
files themselves, not left in this plan.

---

## Scope decisions

Recorded so the plan reflects reality rather than looking abandoned. **The user reviewed progress after
task 7 and cut scope, weighing remaining effort against value.** The cuts are deliberate, not skipped
work, and each cut task below carries the same note inline.

**Cut: 5.4, 5.5, and all of task 6 (6, 6.1, 6.2).**

- **5.4** was redundant. The task 3 suite (`tests/chart/test_preservation.py`) already *is* the
  preservation suite, it already enumerates the MODULO set as a literal, and task 4.9 already verified it
  green against the fixed chart with a chart-drift falsifiability probe. 5.4 would have re-stated existing
  coverage.
- **5.5** was conditional on task 6 and falls with it.
- **Task 6** was explicitly optional and labelled "not required to fix the bug" in this plan from the
  start. Under Option 1 there is one managed root and nothing in the chart can relabel it, so the denial
  that reached those code paths is no longer reachable from a chart configuration.

**Kept: the `pathsOverlap` forward guard (4.3) and its tests (5.2, 5.3).** The guard's relabeling-mount
set is empty today, so it rejects nothing — but it is what checks the next relabeling volume against the
shared roots on the day someone adds one, and the normalization it depends on is where the bugs live.
5.3 was implemented rather than cut.

### Known outstanding defects, carried forward from task 6 (do not lose these)

Cutting task 6 does **not** mean F6 and F7 were dispositioned as non-defects. Both are real defects in
`agent/sync_agent.py`, independent of this fix, and are recorded here so they survive the cut. Full
counterexamples are in [`evidence/counterexamples.md`](evidence/counterexamples.md) **CE2–CE4**.

| ID | Defect | Location | Counterexample |
|---|---|---|---|
| **F6** | `list_datasets` cannot distinguish **unreadable** from **empty**. `directory.iterdir()` raises `PermissionError` on a denied directory and there is no handler anywhere in the `main()` chain, so the process exits and the pod enters `CrashLoopBackOff` | `agent/sync_agent.py` `list_datasets`, and its call sites in `sync_pass` and `main()` | CE2, CE3 |
| **F6** | Readiness is derived from a **directory inventory**, which is not a health signal. A node advertised a correct generation and count with no `data-not-ready` taint while its tree was completely unreadable, and every pod on it reported ready | `reconcile_node_state` and the generation/count labels | CE3 |
| **F7** | rclone stderr is **buffered**, so `--stats=60s --stats-one-line -v` produces no output until the pass returns. The 58.9-minute first sync on the replacement node logged nothing after its startup banner | `agent/sync_agent.py` `run()`, `capture_output=True` | CE4 |

Neither is a blocker for this fix and neither has a test in the suite — a test asserting graceful
degradation would encode the cut hardening and would stay red. If they are picked up later, tasks 6.1 and
6.2 below hold the intended designs, and 5.5 holds the intended test cases.

---

## Already complete — do not re-run

Recorded so the sequence is legible. No task below repeats any of this.

- **Node recovery by replacement.** `ip-10-0-37-101` was cordoned, drained of ArcGIS workloads, and its
  NodeClaim deleted. Replacement `ip-10-0-39-129` completed its first sync (58.9 min for ~840 GiB),
  published `gen=g57a70ff5b429 count=3`, and cleared `data-not-ready`. All 8 nodes now report
  `/mnt/geodata` as `system_u:object_r:local_t:s0` with a successful read (2.9, 2.3, 2.4, 2.5).
- **`geodata-local-pv` and the ArcGIS-owned PVC are deleted.** The PV was `Released` with a stale
  `claimRef` and was removed by hand (F9).
- **Geocoding is republished against the routing `hostPath` folder data store and verified working;
  routing is republished and healthy** (2.2, 2.3, 2.7).
- **The geocoding local-PV folder data store is deregistered in ArcGIS Enterprise Manager.**
- **The requirement 2.8 evidence set is captured**, except for the two items in
  [Optional follow-ups](#optional-follow-ups-uncaptured-evidence).

---

## Optional follow-ups — uncaptured evidence

Two items from the 2.8 set were never obtained. **Neither is a blocker.** The cause is confirmed by
other means — the label and access observations on every consumer, the `seclabel` discriminator in
`/proc/mounts`, and the V1 result — and the affected node no longer exists, so neither can be captured
by observing production. Obtaining them now would require a **deliberate reproduction on a scratch
path**, which costs a node under the F1 denial, so do not attempt either as part of this fix.

- [x] Optional: item 3 — kernel AVC capture from the Bottlerocket host journal
  - `journalctl -k` under `sudo sheltie`, correlating `scontext`/`tcontext`/`tclass` for denials naming
    `/mnt/geodata`
  - Recorded in the design as "captured in effect": denial was confirmed from the consuming side on
    every consumer and superseded as a diagnostic by the direct label and access observations
  - The affected node is gone. A capture now needs a deliberate relabel on a scratch path on a node that
    can be discarded, and teardown is node replacement, not `chcon` (F1)
  - _Requirements: 2.8 (optional residual)_

- [x] Optional: item 7 — D2's controlled hostPath-versus-local comparison on a scratch path
  - Record `ls -ldZ` on a scratch directory, mount it into a pod by `hostPath` and re-record, then mount
    it by a throwaway `local` PV and re-record. Two before/after pairs on this exact stack
  - D2 is already resolved and generalized as the `seclabel` rule (F2), from `/proc/mounts` plus
    `CSIDriver.seLinuxMount=false` on all four drivers in use, so this would confirm rather than decide
  - **If anyone runs it: never point the throwaway `local` PV at `datasetRoot` or any path under it, and
    do not mount the live mirror.** Blast radius is one scratch directory plus throwaway cluster objects,
    on a node that can be discarded
  - _Requirements: 2.8 (optional residual)_

---

## Rollback, verified independently first

- [x] 1. Roll back the prior session's shared-MCS-level changes as targeted edits
  - **This runs first and is proven green before any new behavior is introduced**, so a later failure
    cannot be ambiguous between the rollback and the fix
  - No cluster or node state is touched. Repository edits only
  - Per-pod-sandbox category assignment is why this is correct rather than merely required: a pinned
    shared level would have had to reach every replica of every service on every node, and two pods of
    one ReplicaSet already hold different pairs, so the approach had no reachable success state
  - _Requirements: 2.11, 2.12_

  - [x] 1.1 Remove the schema `required` entry, the schema property, and the `values.yaml` default together in one change
    - `charts/node-local-data/values.schema.json`: remove `"selinux"` from `required`, and remove the `selinux` property object including its `sharedLevel` pattern and description
    - `charts/node-local-data/values.yaml`: remove the `selinux:` block and its six-line comment
    - **These edits MUST land together.** `examples/` contains no `selinux` reference, so the examples
      rely on the chart default. Removing the default without removing the schema `required` entry, or
      the reverse, leaves an intermediate commit that fails to render `examples/values-example.yaml` and
      `examples/values-instance-types.yaml`, breaking 3.8 at a commit boundary
    - _Requirements: 2.11, 2.12, 3.8_

  - [x] 1.2 Remove the template-side shared-level machinery
    - `charts/node-local-data/templates/_validate.tpl`: remove the `$sharedCategories` split and the distinct-MCS-category `fail`
    - `charts/node-local-data/templates/07-sync-agent-daemonset.yaml`: remove the pod-level `securityContext.seLinuxOptions` block and the two-line comment above it
    - `charts/node-local-data/templates/08-path-shim.yaml`: remove the pod-level `securityContext.seLinuxOptions` block and the two-line comment above it
    - `charts/node-local-data/templates/NOTES.txt`: remove the "SELinux requirement for every pod that mounts ..." block and the paragraph directing operators to apply it to ArcGIS workloads by admission mutation
    - _Requirements: 2.11, 2.12_

  - [x] 1.3 Remove the shared-level test coverage
    - `tests/chart/test_render.py`: remove the `shared_level` local and the two pod `securityContext` assertions
    - Remove `assert "level: s0:c1022,c1023" in notes` and `assert "admission mutation before pod creation" in notes`
    - Remove the three `selinux.sharedLevel` parametrize cases: two schema-pattern cases and one distinct-category validation case
    - _Requirements: 2.11, 2.12_

  - [x] 1.4 Remove the operator-facing shared-level values and set the chart version
    - `my-values.yaml`: remove the `selinux:` block and the two-line comment above it
    - `charts/node-local-data/Chart.yaml`: set `version: 0.2.0`. The `0.1.1` bump existed only for the
      rejected change; do not return to `0.1.0`, because Option 1 removes rendered resources and changes
      the install workflow, so it is not a patch. Record the reasoning in the commit message
    - _Requirements: 2.11, 2.12_

  - [x] 1.5 Prove the rollback green before any new behavior is added
    - `helm lint --strict charts/node-local-data`
    - `helm template` against **both** example values files: `examples/values-example.yaml` and `examples/values-instance-types.yaml`
    - `./scripts/check-placeholders.sh`
    - The chart suite and the agent suite as single runs, not watch mode: `pytest tests/chart` and `pytest agent/tests`
    - **EXPECTED OUTCOME**: all green
    - Record the command outputs as `evidence/rollback-validation.md` in the spec directory
    - _Requirements: 2.11, 2.12, 3.8_

---

## Exploration and preservation tests, written before the fix

- [x] 2. Write bug condition exploration test
  - **Property 1: Bug Condition** - No relabeling mount overlaps a shared tree
  - **CRITICAL**: This test MUST FAIL on the rolled-back but unfixed chart - failure confirms the bug exists
  - **DO NOT attempt to fix the test or the code when it fails**
  - **NOTE**: This test encodes the expected behavior - it will validate the fix when it passes after implementation
  - **GOAL**: Surface counterexamples that demonstrate the bug exists
  - **Scoped PBT Approach**: The bug is deterministic at the chart level, so scope the property to the
    concrete failing configuration — `localVolume.enabled=true` with the default `datasetRoot`, where the
    `local` PV path and the Path Shim `hostPath` are the same string — and generate the denormalized and
    nested variants (`/mnt/geodata/`, `/mnt/geodata//x`, `/mnt/geodata/y/../x`) as additional inputs that
    must also be rejected
  - Bug condition under test, from `isBugCondition` in the design: a relabeling mount — `relabelsOnMount`
    true when the plugin reports SELinux support **and** the filesystem carries `seclabel` (the `seclabel`
    rule, F2) — whose `hostPath` satisfies `pathsOverlap` against any member of `sharedRoots`, which is
    the routing registration path, also the Path Shim `hostPath` and the agent's routing `hostPath` mount
  - Assertion, matching the Expected Behavior Properties: `renderChart` FAILS, and the failure either
    names both the relabeling path and the shared root or is the legacy-key rejection
  - Run the test on the rolled-back but otherwise UNFIXED chart
  - **EXPECTED OUTCOME**: Test FAILS. Today the chart renders the overlap silently, which is the
    counterexample for 1.1
  - Also reproduce the agent-side counterexamples as unit tests that fail-to-fail today: `list_datasets`
    against a directory that raises `PermissionError` propagates the error; the `main()` call site has no
    handler, so the process exits and neither `sync_pass` nor `reconcile_node_state` runs; and `run()`
    buffers rclone stderr with `capture_output=True`, so nothing is logged until the pass returns (F7).
    Induce the denial with mode bits or a monkeypatch; no SELinux needed
  - Document the counterexamples in `evidence/counterexamples.md`: both paths identical in a successful
    render, `PermissionError: [Errno 13]` out of `list_datasets` propagating to process exit, and no log
    output from a long pass until it completes
  - **Record in the test file** that this proves representability only. A real relabel is not reachable
    from `helm template`; it is covered by the recorded evidence set instead
  - Mark this task complete when the test is written, run, and the failure is documented
  - _Requirements: 1.1, 1.5, 1.9, 2.1_

- [x] 3. Write preservation property tests (BEFORE implementing fix)
  - **Property 2: Preservation** - Non-overlapping node states and the routing-only deployment
  - **IMPORTANT**: Follow observation-first methodology. Observe the UNFIXED (post-rollback) behavior
    first, record it, then assert exactly that
  - Observe and record on the unfixed chart: the full render with `localVolume.enabled=false`; the Path
    Shim `hostPath`; the agent's routing `hostPath` and `--dest-root`; the bind target; the NOTES
    registration path; the rclone flag set; the generation digest and count for a fixed set of top-level
    names; the NodePool and EC2NodeClass field set
  - **The MODULO difference set is explicit and closed.** The fixed render may differ from the original
    only in: `removedLocalVolumeResources`, `chartVersion`, `removedSELinuxSecurityContexts`, and
    `notesText`. Every other rendered field must be byte-identical. Enumerate this set in the test file as
    a literal, not as whatever the diff happens to show, or the preservation check degenerates into
    asserting the new behavior
  - Write these cases:
    - Routing-only render preservation with `localVolume.enabled=false`, identical modulo the set above (3.2)
    - Registered routing path preservation: Path Shim `hostPath`, agent routing `hostPath`, `--dest-root`, bind target, and the NOTES registration path all still exactly `/mnt/geodata` (3.5)
    - Sync semantics preservation: `--metadata`, no `--inplace`, `syncAgent.excludes`, the concurrency flags, and the single recursive `rclone sync` invocation shape unchanged. If the optional streaming change in task 6 is applied, it must not change argv (3.3)
    - Readiness reconciliation preservation: generation digest and count unchanged and order-independent for a given set of top-level names, and `write_generation_label` still suppresses identical writes, so the per-interval write storm is not reintroduced (3.3)
    - NodePool and EC2NodeClass identity preservation: every field byte-identical, so no NodeClaim is marked Drifted and no node loses its mirror as a side effect (3.7, 3.9)
    - No `seLinuxOptions` on any rendered pod or container, and no container privileged (3.6, and the task 1 rollback)
    - Non-overlapping siblings still render: `/mnt/geodata` alongside `/mnt/geodata-geocoding` passes validation, so the overlap guard does not over-reject
  - Write the property-based tests over the non-bug input domain: `datasetRoot` values below `/mnt/`,
    node-pool variations, and legacy-key presence. Keep the existing example-based tests as readable anchors
  - Add `hypothesis` to `tests/chart/requirements-dev.txt` with a pinned version; it is not currently a dependency
  - Run the tests on the UNFIXED (post-rollback) chart
  - **EXPECTED OUTCOME**: Tests PASS. This is the baseline being preserved
  - Mark this task complete when the tests are written, run, and passing on unfixed code
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9_

---

## The fix

- [x] 4. Fix for the relabeling volume sharing the routing tree — remove the relabeling volume type
  - Serve both workloads from the one non-relabeling `hostPath` folder data store. Nothing relabels the
    tree, so it stays category-free permanently. The sync agent, its write root, the Path Shim, the bind
    target, and the registered routing path are all unchanged, which makes 3.1–3.5, 3.7, and 3.9 hold by
    construction. No MCS exemption exists anywhere, so 3.6 holds without its documented-exception clause

  - [x] 4.1 Remove the local PV, its StorageClass, and its values surface
    - Delete `charts/node-local-data/templates/09-local-pv.yaml`, removing the `geodata-local-sc` StorageClass and the `geodata-local-pv` PersistentVolume — the only relabeling mount the project can create
    - `charts/node-local-data/values.yaml`: remove the `localVolume` block and its comment
    - `charts/node-local-data/values.schema.json`: remove the `localVolume` property and its `required` entry. Top-level `additionalProperties: false` then makes any leftover `localVolume` key a hard render failure naming the key, rather than a silently ignored setting
    - `localVolume.enabled` resolves as **removed**, not defaulted false. There is no value, so there is no default, and the chart cannot express a relabeling volume at all
    - Targeted edits — these files are untracked and cannot be reverted with `git checkout` (2.12)
    - _Bug_Condition: isBugCondition(input) — a relabeling mount whose hostPath overlaps a shared root; this empties the relabeling-mount set_
    - _Expected_Behavior: expectedBehavior(result) from design — the shared tree keeps the healthy reference label, type and level, with no MCS categories, across any number of geocoding restarts_
    - _Preservation: Preservation Requirements from design — routing path, agent, Path Shim, bind target, and NodePool identity unchanged_
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 3.5_

  - [x] 4.2 Add the `localVolume` legacy-key rejection
    - `charts/node-local-data/templates/_validate.tpl`: fail when `.Values.localVolume` is present at all, with an explanatory message stating that the geocoding local PV was removed because it relabels the shared tree, and that geocoding is now served by the routing `hostPath` registration
    - This is how 2.1 is satisfied today — the overlap is unrepresentable rather than merely validated — and it is what turns an unknown-property error into a failure that explains itself
    - _Bug_Condition: isBugCondition(input) from design_
    - _Expected_Behavior: renderChart(valuesOf(input)) = FAILURE with a message explaining why a relabeling volume cannot share the routing tree_
    - _Preservation: Preservation Requirements from design — no effect on any values file that does not carry the legacy key_
    - _Requirements: 2.1, 2.2_

  - [x] 4.3 Add the `pathsOverlap` helper and the overlap check as a forward guard
    - `charts/node-local-data/templates/_validate.tpl`: add `node-local-data.pathsOverlap` and the overlap check, invoked from the templates that already call `node-local-data.validate`
    - Build the shared-roots set from the routing registration path, which is also the Path Shim `hostPath` and the agent's routing `hostPath` mount. The relabeling-path set is **empty today**, so the check has nothing to reject; it is retained so that any future volume that could be relabeled under the `seclabel` rule (F2) is checked against the shared roots on the day it is added to the chart
    - Compare `clean`-normalized paths only: collapse duplicate separators, resolve `.` and `..` lexically, strip a trailing separator. Test containment in both directions with an explicit trailing separator on the prefix, so `/mnt/geodata` does not falsely match the sibling `/mnt/geodata-geocoding` while it does match the true child `/mnt/geodata/geocoding`
    - The failure message must name both paths and explain the asymmetry, per the design's message text
    - **Stated limitation**, which must appear in the surrounding documentation and in `tuning/gotchas.md`: this is lexical. `helm template` cannot resolve symlinks, bind mounts, or hard links, so two distinct paths that alias the same inodes pass validation and still break at runtime. Because labels live on inodes, distinct paths are necessary but the operator must also ensure distinct inodes
    - _Bug_Condition: isBugCondition(input) from design — the guard covers any future member of the relabeling-mount set_
    - _Expected_Behavior: renderChart fails naming both the relabeling path and the shared root_
    - _Preservation: Preservation Requirements from design — non-overlapping configurations render unchanged_
    - _Requirements: 2.1, 2.2_

  - [x] 4.4 Remove the now-unused helpers
    - `charts/node-local-data/templates/_helpers.tpl`: remove `node-local-data.localStorageClassName` and `node-local-data.localPersistentVolumeName`
    - _Requirements: 2.2, 3.8_

  - [x] 4.5 Rewrite the install notes
    - `charts/node-local-data/templates/NOTES.txt`: remove **both** branches of the `localVolume.enabled` conditional and the two-stage install guidance that exists only to sequence it
    - Replace with a single shared-hostPath registration note: geocoding and routing share the one `hostPath` folder data store at the rendered path, and the readiness command must show a completed first sync before publishing against it
    - The notes must not print an SELinux level requirement and must not tell operators to apply anything to ArcGIS workloads
    - _Requirements: 2.10, 2.11_

  - [x] 4.6 Confirm the chart version
    - `charts/node-local-data/Chart.yaml` is at `version: 0.2.0` from task 1.4. Confirm it, since this task group is what justifies the minor bump: rendered resources are removed and the install workflow changes
    - _Requirements: 2.11_

  - [x] 4.7 Execute the remaining operational step — the chart upgrade and the PV absence check
    - **[MUTATES — OPERATOR CONFIRMATION REQUIRED]**. **Blast radius**: Helm deletes the
      `geodata-local-sc` StorageClass and the `geodata-local-pv` PersistentVolume. **No node filesystem
      content is deleted and the mirror is not touched, moved, or re-synced (3.1)** —
      `reclaimPolicy: Retain` means deleting the PV object touches no node data. Reversible by
      re-installing the prior chart version, which recreates the PV but not any binding
    - The prerequisites that had to precede this are already done: the geocoding data store is
      deregistered and the ArcGIS-owned PVC is deleted. **Deleting a PV that still has a live claim
      leaves the claim unbound and surfaces only as a binding timeout**, which is why that order mattered
    - `helm upgrade` to the version that no longer renders the StorageClass and PV
    - Then confirm `geodata-local-pv` is genuinely absent, and delete it explicitly if it survived (F9).
      A `Released` PV with a stale `claimRef` never rebinds and `helm upgrade` does not reconcile it
      ```bash
      kubectl get pv geodata-local-pv -o jsonpath='{.status.phase}{"\n"}' 2>/dev/null || echo absent
      kubectl delete pv geodata-local-pv     # only if it is still present after the upgrade
      ```
    - **The PV was already deleted by hand, so the expected result is `absent` and this verification may
      be a no-op.** Run it anyway — a surviving `Released` PV is a failure of the sequence, not a transient
    - Verify the end state: routing readiness green, the geocode service still serving, an agent pass in
      the log on both pool nodes, and `/mnt/geodata` still `local_t:s0` cluster-wide
    - Record the upgrade output, the PV check, and the operator confirmation in `evidence/option1-sequence.md`
    - _Bug_Condition: the cluster must contain no object able to create a relabeling mount over the shared tree_
    - _Expected_Behavior: expectedBehavior(result) from design — the shared tree stays at the healthy reference label with every consumer succeeding_
    - _Preservation: Preservation Requirements from design — the mirror stays in place, the registered routing path is unchanged, and no NodeClaim is marked Drifted_
    - _Requirements: 2.2, 2.3, 2.6, 2.7, 2.9, 3.1_

  - [x] 4.8 Verify bug condition exploration test now passes
    - **Property 1: Expected Behavior** - No relabeling mount overlaps a shared tree
    - **IMPORTANT**: Re-run the SAME test from task 2 - do NOT write a new test
    - The test from task 2 encodes the expected behavior; when it passes, the expected behavior is satisfied
    - Run bug condition exploration test from step 2
    - **EXPECTED OUTCOME**: Test PASSES, confirming the overlap configuration is now unrepresentable
    - Note in the run record that this covers **Phase 1** of Fix Checking only. Phase 2's label and access
      assertions need an enforcing node and are covered by the recorded evidence: the reference histogram
      from `ip-10-0-37-12`, the all-8-nodes `local_t:s0` observation with successful reads, the geocode pod
      reading as `container_t:s0:c40,c511`, and `ip-10-0-39-129` reaching `gen=g57a70ff5b429 count=3` with
      the taint cleared
    - _Requirements: Expected Behavior Properties from design — 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7_

  - [x] 4.9 Verify preservation tests still pass
    - **Property 2: Preservation** - Non-overlapping node states and the routing-only deployment
    - **IMPORTANT**: Re-run the SAME tests from task 3 - do NOT write new tests
    - Run preservation property tests from step 3
    - **EXPECTED OUTCOME**: Tests PASS, identical modulo the explicit MODULO set:
      `removedLocalVolumeResources`, `chartVersion`, `removedSELinuxSecurityContexts`, `notesText`. Any
      other difference is a regression, not an intended change
    - Confirm all tests still pass after the fix (no regressions)
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9_

---

## Remaining test coverage

- [x] 5. Add the fixed-behavior chart, schema, unit, and property tests
  - _[Complete. 5.1, 5.2, and 5.3 implemented and green — 21 + 7 + 32 chart tests. 5.4 and 5.5 cut by the
    user; see [Scope decisions](#scope-decisions).]_
  - **Add the standing note to every file touched here**: these tests prove representability only. A real
    relabel cannot be reproduced by `helm template` and is covered by the recorded evidence instead, so a
    green chart suite is never mistaken for proof that the relabel is fixed
  - _Requirements: 2.1, 3.3, 3.8_

  - [x] 5.1 Chart render and schema cases
    - Legacy-key rejection: a values file carrying `localVolume` fails rendering with the explanatory message from 4.2, not a bare unknown-property error
    - ~~Schema case: a leftover `localVolume` key fails against top-level `additionalProperties: false`, naming the key~~ **CORRECTED — this bullet was written before 4.2 and describes behaviour the fix deliberately does not have.** Helm validates `values.schema.json` *before* rendering templates, so an undeclared `localVolume` was rejected by the schema first and the operator never reached the explanation. Task 4.2 therefore re-declared `localVolume` in the schema **permissively** — a `description` only, no type or property constraints, absent from `required` — purely as a pass-through so the key reaches the explanatory `fail` in `_validate.tpl`. Do not assert that `localVolume` produces an `additional properties` error; it does not, and must not. Instead: exercise `additionalProperties: false` against a **different** unknown key (`test_render.py` already covers this with its `unexpectedValue=true` parametrize case — reference it, do not duplicate), and add a case that pins the arrangement so that removing the permissive declaration degrades the message and removing the template check accepts the key. `test_selinux_relabel_overlap.py`'s `rejection_explains_the_overlap` helper accepts either rejection shape, so it alone cannot detect such a swap
    - Overlap guard: a configuration placing a relabeling path over a shared root is rejected with a message naming both paths. The guard's relabeling-mount set is empty by construction, so no values file reaches it; the case injects one synthetic `relabelsOnMount: true` inventory entry into a scratch copy of the chart, which is the only way to exercise the branch at all
    - `datasetRoot` outside `/mnt/` still fails rendering, preserving the existing rule
    - _Requirements: 2.1, 3.8_

  - [x] 5.2 `pathsOverlap` normalization unit cases
    - Identical paths; trailing separator; duplicate separators; `..` segments; true child in both directions
    - **Sibling with a shared prefix — `/mnt/geodata` versus `/mnt/geodata-geocoding` — accepted as non-overlapping.** This is the case the naive prefix test gets wrong
    - _Requirements: 2.1, 3.8_

  - [x] 5.3 `pathsOverlap` property-based cases
    - **[IMPLEMENTED — not cut.** `tests/chart/test_paths_overlap_properties.py` exists and is green,
      contributing **7 tests**: P1 reflexivity, P2 root, P3 ancestor/descendant, P4 segment boundary,
      P5 distinct siblings, P6 symmetry, P7 normalization invariance. Kept alongside the 4.3 forward
      guard.**]**
    - Generate non-overlapping path pairs and assert `pathsOverlap` is false
    - Generate overlapping pairs by construction — equal, child, grandchild, and denormalized forms — and assert it is true
    - The normalization bugs live here, which is why this is property-based rather than example-based
    - _Requirements: 2.1, 3.8_

  - [x] 5.4 Preservation cases with the MODULO difference set enumerated
    - **[CUT BY THE USER as redundant, after reviewing remaining effort against value.** The task 3 suite
      `tests/chart/test_preservation.py` already *is* the preservation suite: it enumerates the MODULO set
      as a literal (`MODULO_DIFFERENCE` at line 88), covers all seven case groups, and task 4.9 verified it
      green against the fixed chart with a chart-drift falsifiability probe. 5.4 would have re-stated
      existing coverage. See [Scope decisions](#scope-decisions).**]**
    - The task 3 cases, kept as the preservation suite: routing-only render, registered routing path, sync semantics, readiness reconciliation, NodePool and EC2NodeClass identity, and no `seLinuxOptions` anywhere
    - The MODULO set appears in the test file as a literal — `removedLocalVolumeResources`, `chartVersion`, `removedSELinuxSecurityContexts`, `notesText` — and every other rendered field is asserted byte-identical
    - `generation_value` and `write_generation_label` unchanged for unchanged inputs
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9_

  - [x] 5.5 Agent enumeration cases — only if task 6 is implemented
    - **[CUT BY THE USER, because it is conditional on task 6, which was also cut.** These cases test the
      optional hardening; with the hardening deferred they would assert behavior the agent does not have
      and would stay red. Retained below as the intended test design if F6/F7 are picked up later. See
      [Scope decisions](#scope-decisions).**]**
    - Skip entirely if the optional hardening is deferred
    - An unreadable directory yields an explicit **unreadable** outcome, distinct from empty; an empty directory still yields empty
    - An unreadable root does not raise, does not change the bootstrap taint, and does not alter the generation or count labels
    - **A readable single root behaves exactly as today**: same enumeration, same digest, same count, same taint transitions, same exit status. This is the assertion that makes the hardening safe to keep
    - rclone stderr streaming: output reaches the log before the process exits, and the one-line stats parsing still matches
    - _Requirements: 2.5, 3.3, 3.8_

---

## Optional agent improvements

- [x] 6. Optional agent hardening — **not required to fix the bug**
  - **[CUT BY THE USER in whole, after reviewing remaining effort against value.** This task was explicitly
    optional and labelled "not required to fix the bug" in the plan itself from the start. F6
    (unreadable-vs-empty detection) and F7 (rclone stderr streaming) are **real agent defects**, but they
    are independent of this fix: with one managed root that nothing can relabel, the denial that reached
    those code paths is no longer reachable from a chart configuration.
    **The defects are recorded as known outstanding work under
    [Known outstanding defects](#known-outstanding-defects-carried-forward-from-task-6-do-not-lose-these),
    with counterexamples at [`evidence/counterexamples.md`](evidence/counterexamples.md) CE2–CE4, so they
    are not lost.** The deferral decision is recorded in [Scope decisions](#scope-decisions).**]**
  - With one managed root that nothing can relabel, the fix does not depend on any of this. Each item
    below is **independently skippable**. If skipped, record the deferral decision in the spec directory
  - Whatever is implemented must not change observable behavior for a single readable root, which is
    exactly what the task 3 and 5.4 preservation tests assert
  - _Requirements: 2.5, 3.3, 3.8_

  - [x] 6.1 Optional (F7) — stream rclone's stderr so long syncs are observable
    - **[CUT BY THE USER with its parent task 6.** F7 remains a real, unfixed defect — recorded under
      [Known outstanding defects](#known-outstanding-defects-carried-forward-from-task-6-do-not-lose-these)
      with counterexample **CE4** in [`evidence/counterexamples.md`](evidence/counterexamples.md). The
      design below stands if it is picked up later.**]**
    - **Not required to fix the bug.** Independently skippable
    - `agent/sync_agent.py`: replace the buffered `capture_output=True` call for the sync invocation with streamed stderr, so `--stats=60s --stats-one-line -v` lands in the pod log as the pass runs. The 58.9-minute first sync on the replacement node logged nothing after its startup banner and progress had to be inferred from the filesystem
    - The parsing that consumes rclone's one-line stats must keep working, and **argv must not change**, or the sync-semantics preservation case fails
    - _Requirements: 2.5, 3.3_

  - [x] 6.2 Optional (F6) — distinguish unreadable from empty and scope readiness to an access check
    - **[CUT BY THE USER with its parent task 6.** F6 remains a real, unfixed defect — recorded under
      [Known outstanding defects](#known-outstanding-defects-carried-forward-from-task-6-do-not-lose-these)
      with counterexamples **CE2** and **CE3** in
      [`evidence/counterexamples.md`](evidence/counterexamples.md). The design below stands if it is picked
      up later.**]**
    - **Not required to fix the bug.** Independently skippable
    - `agent/sync_agent.py`: `list_datasets` currently calls `directory.iterdir()`, which raises `PermissionError` on a denied directory, and `main()` calls it outside any handler, so the process exits and the pod enters `CrashLoopBackOff` — after which `sync_pass` and `reconcile_node_state` never run and the taint and labels freeze. Return an explicit outcome — a `(status, names)` pair or a small result type — rather than a bare list, and do not let a denial kill the process
    - An empty directory means "this node holds no datasets" and legitimately re-applies the bootstrap taint; a denied directory means "unknown", and treating unknown as empty would re-taint a node whose mirror is complete
    - **Readiness stays derived from the routing root bit for bit.** The dataset-generation digest and dataset-count label keep their current derivation and current values, so 3.3 is preserved exactly and no label churn is introduced
    - Base readiness on an explicit access check by the consuming identity rather than a directory inventory. A node advertised a correct generation and count with no taint while its tree was completely unreadable, and every pod on it reported ready — directory inventory is not a health signal
    - Surface a persistently unreadable root as a distinct chart-owned node label recording the root's last outcome, kept strictly separate from the readiness taint so it cannot gate routing scheduling
    - _Requirements: 2.5, 3.3_

---

## Documentation

- [x] 7. Rewrite the documentation around the `seclabel` rule and volume-type asymmetry
  - _[Complete. All nine subtasks 7.1–7.9 done across `README.md`, `tuning/gotchas.md`, `tuning/nvme.md`,
    and `RESULTS.md`. Verified at the task 8 checkpoint: every markdown link in the four edited docs
    resolves, no removed `localVolume` / `geodata-local-pv` / `geodata-local-sc` reference survives outside
    the 0.2.0 migration path, and no document recommends an ArcGIS-side remedy.]_
  - This **replaces** the shared-MCS-level guidance rather than sitting beside it (2.10)
  - Targeted edits only. `README.md` and `tuning/gotchas.md` are tracked but carry large unrelated
    Helm-conversion changes that must not be wholesale-reverted (2.12)
  - _Requirements: 2.10, 1.7, 1.8, 1.10, 2.9, 2.11, 2.12_

  - [x] 7.1 `README.md` — remove the rejected guidance
    - Delete the "Required shared SELinux level" section entirely, including the ArcGIS Deployment patch script and the rollout-wait loops, and remove its table-of-contents entry
    - Delete the `selinux.sharedLevel` row from the "Important defaults" table
    - Delete the shared-level paragraph under "Upgrade safely" and its cross-reference, keeping the remaining upgrade guidance intact
    - _Requirements: 2.11, 2.12, 1.8_

  - [x] 7.2 `README.md` — the `seclabel` rule and volume-type asymmetry
    - Add a section roughly where the removed one sat, covering: the `seclabel` rule as the actual test for whether a volume gets relabeled — the plugin must report SELinux support **and** the filesystem must carry `seclabel` — with the observed `xfs rw,seclabel` versus `nfs4` mount lines; that a category-free object label is what lets consumers with different MCS categories share one tree, evidenced by the surveyed pods reading successfully with distinct category pairs; and that the correct response is to ensure nothing relabels the tree, not to align categories
    - State that aligning ArcGIS categories is not available here because ArcGIS pod specifications cannot be changed, and that categories are assigned **per pod sandbox** — two pods of one ReplicaSet on one node hold different pairs — so nothing can be aligned by service, Deployment, or ReplicaSet
    - Add the NFS evidence: six ArcGIS pods with six distinct MCS pairs, two co-located, all sharing `ontap-gisdata-pvc` (`ReadOnlyMany`, NFS) and all healthy. Add the `ReadWriteOnce`-EBS caveat as **explicitly unverified**: probably relabeled, does not bite because there is a single consumer, check `seclabel` before sharing one
    - _Requirements: 2.10, 1.8_

  - [x] 7.3 `README.md` — close the "just turn SELinux off" question with citations
    - State plainly that `chmod 777`, disabling SELinux enforcement, and privileged consumers do not help, because Unix mode bits do not override an MCS mismatch: the denied directory was `drwxr-xr-x` root:root throughout, and a uid 0 non-privileged pod was denied on it
    - Cite the evidence rather than asserting it: the successful readers on other nodes over the same mode bits, and the type-plus-level difference between `local_t:s0` on healthy nodes and `data_t:s0:c20,c476` on the affected one
    - _Requirements: 2.10, 1.8, 2.7_

  - [x] 7.4 `README.md` — the single-`hostPath` path model
    - Update the `datasetRoot` row and the architecture diagram: both arrows point at the one `hostPath` folder data store and the local-PV branch is gone
    - Remove the "Geocoding local PersistentVolume" registration section, the two-stage install sequencing that exists only for it, and the "Before disabling the local volume" section. Replace them with the shared `hostPath` registration for both workloads
    - Record what this gives up: PV-based registration through Enterprise Manager moves to the Admin REST API model with organization-wide mount scope, which is already in force for routing and so is not a new exposure; and the loud `MountVolume.SetUp` failure a `local` volume gives on a node without the data is replaced by a path-present-but-empty failure at query time — the same trade the Path Shim already makes for routing, and the reason the first-sync gate and node affinity still matter
    - _Requirements: 2.10, 3.5_

  - [x] 7.5 `README.md` — node-replacement recovery as the primary runbook
    - Make **node replacement** the recovery procedure: cordon, relocate the ArcGIS workloads (cordon alone evicts nothing), let the evicted warm-capacity placeholder force Karpenter to provision a replacement, wait for the first sync and the taint to clear, confirm label and mirror and published generation and count, then delete the old NodeClaim
    - State the blast radius: **one node.** Its ArcGIS workloads move, its mirror is discarded and re-derived from the authoritative source, and no other node is touched. Budget for one first sync — 58.9 minutes here — and the concurrent FSx read
    - **Carry the mirror constraint into the recovery text (3.1): nothing in this procedure deletes, moves, or re-syncs another node's mirror.** Node replacement discards one node's copy by design, which is the node pool's own premise
    - State the prerequisite and its order: stop the re-poisoning source first. Deregistration of the data store, deletion of the ArcGIS-owned PVC, and removal of the PV all precede replacement, or the replacement is poisoned in turn
    - State plainly that removing the mount, the PVC, and the data store registration does **not** restore access on an already-affected node. The label is on disk, and the affected node's sync agent went from 269 to 379 restarts across the removal
    - **Demote label repair and mark it denied on Bottlerocket 1.64.0.** `chcon -R` from a `--profile=sysadmin` debug container read the entire tree and failed every `setxattr` with `Permission denied`; `restorecon` and `matchpathcon` are not present in the `amazonlinux:2023` debug image. Keep it documented only so nobody rediscovers the denial from scratch, and do not present it as a working runbook step
    - Note that one post-recovery label confirmation is enough: new inodes do not take the writing process's categories, so a clean tree stays clean
    - _Requirements: 2.9, 1.10, 1.7, 3.1_

  - [x] 7.6 `README.md` — troubleshooting: in-pod path remapping and progress observation
    - PV-versus-`hostPath` in-pod path remapping (F3): a PV-based folder data store appears in-pod at `/arcgis/volumes/pvc/<derived>/<registered-path>`, with `<derived>` taken from the CSI volume handle and the literal **`noid`** when there is none, so a local PV at `/mnt/geodata` appears as `/arcgis/volumes/pvc/noid/mnt/geodata`. A `hostPath` store is not remapped and appears at the host path itself. Looking for the host path inside a PV-backed pod and concluding the mount is missing is a live trap that cost time during V1
    - Progress observation (F7): `du -sh` overstates progress because rclone preallocates — `703G` disk versus `569G` apparent on the same tree — so `du --apparent-size` is the honest measure, and in-flight work is visible as `*.partial` files
    - _Requirements: 2.10, 1.7_

  - [x] 7.7 `tuning/gotchas.md` — replace the wrong rule and add the durable ones
    - **Replace** the "Every pod mounting the shared hostPath must use one SELinux MCS level" rule. It describes the mechanism as symmetric across volume types and prescribes an action that is not permitted here
    - Add: **A volume is relabeled only if the plugin supports SELinux and the filesystem carries `seclabel`.** `hostPath` fails the first test; NFSv4.1 without `security_label` fails the second and presents one fixed context for the whole mount. Check `/proc/mounts` for `seclabel`, and `CSIDriver.seLinuxMount` for whether `-o context=` is even available. One relabeling consumer redefines the tree's label for every other consumer on that node, and mode 0755 root:root does not help
    - Add: **A category-free label is the shareable state.** Any `container_t` process can access an object with no MCS categories, which is why consumers with unrelated category pairs coexisted for months
    - Add: **Distinct paths are necessary but not sufficient.** Labels live on inodes, so a bind mount, hard link, or symlink exposing one tree under two paths exposes it to one relabel. Chart validation is lexical and cannot see this
    - Add: **The relabel is on disk and outlives the consumer.** The `local` plugin writes per-inode labels with `setxattr`; it does not use a `context=` mount option. Deregistering the data store, deleting the PVC, and removing the pod do not restore access. The discriminator is `seclabel` versus `context=`
    - Add: **The relabel changes the type as well as the level.** `local_t:s0` on healthy nodes versus `data_t:s0:c20,c476` on the affected one, so clearing categories alone leaves the wrong type
    - Add: **You cannot relabel it back on Bottlerocket.** `chcon -R` from a `--profile=sysadmin` debug container reads the whole tree and fails every `setxattr`; `restorecon` and `matchpathcon` are absent from the `amazonlinux:2023` debug image. **Replace the node.** It works because the mirror is derived from an authoritative source
    - Add: **New files do not inherit the writer's categories.** The agent runs with categories and writes category-free files, so a clean tree stays clean and one post-sync confirmation is enough
    - Add: **MCS categories are per pod sandbox, not per service.** Two pods of one ReplicaSet on one node hold different pairs
    - Add: **Readiness labels are an inventory, not a health signal.** A node advertised the correct generation and count with no taint while its tree was unreadable, and every pod on it reported ready. Verify access as the consuming identity
    - Keep the existing rules on organization-wide `hostPath` registration scope, `hostPath.type: Directory` not creating paths, `allowServicesRestart`, and local-volume path immutability. Extend the last with F9: a `Released` PV with a stale `claimRef` never rebinds and `helm upgrade` does not reconcile it, so delete it explicitly — and note that `Retain` means deleting the object touches no node data
    - Leave every other rule untouched, and do not wholesale-revert this tracked file
    - _Requirements: 2.10, 1.8, 2.11, 2.12_

  - [x] 7.8 `tuning/nvme.md` — path-model correction
    - The line stating that the path appears in the DaemonSet, the shim, and the local PersistentVolume is now wrong. The local PersistentVolume is gone; update it to the single-`hostPath` model
    - _Requirements: 2.10_

  - [x] 7.9 `RESULTS.md` — conditions plus the counter-measurement
    - **Keep the original figure.** Do not delete the existing measurement; add the conditions under which it was taken
    - Add the counter-measurement from the replacement node: `Pass complete: ... 901830389268 bytes transferred, 901830740761 bytes on disk, 3533.9s total` — **58.9 minutes for ~840 GiB, about 255 MB/s**, while the FSx source was concurrently serving live reads from a second node and the payload was dominated by 18–35 GB locator archives
    - State that both numbers are real and the difference is the conditions, so the headline figure is not read as a guarantee. The existing table advertises roughly 500 GB in about five minutes
    - _Requirements: 2.10_

---

## Final validation

- [x] 8. Checkpoint - Ensure all tests pass
  - `helm lint --strict charts/node-local-data` **with `-f <values>`** against each of
    `examples/values-example.yaml`, `examples/values-instance-types.yaml`, and `my-values.yaml`
    - **CORRECTION 1 — the bare form without `-f` exits 1 BY DESIGN and is not a checkpoint failure.**
      `values.yaml` ships empty required placeholders for the operator to fill (`labelDomain`, `nfs.*`,
      `nodePool.zones`, `aws.*`, `image.repository`), so the chart cannot lint without a values file. This
      is pre-existing and correct; it is already recorded under
      [Pre-existing condition](evidence/rollback-validation.md) in the task 1.5 evidence. Always pass `-f`
    - **CORRECTION 2 — `helm lint --strict` is NOT a reliable gate for the `_validate.tpl` failures.** On
      Helm v4.2.4 it exits **0** even when a template `fail` fires: linting the chart with
      `--set localVolume.enabled=true` emits six `level=INFO msg="funcMap fail"` lines and still reports
      `0 chart(s) failed`. Gate the legacy-key and overlap rejections with `helm template` or
      `helm install --dry-run`, which do exit 1
  - `helm template` for **both** example values files: `examples/values-example.yaml` and `examples/values-instance-types.yaml`
  - `./scripts/check-placeholders.sh`
  - `kubectl apply --dry-run=server` against the rendered output
  - The full pytest suite as single runs, not watch mode: `pytest tests/chart` and `pytest agent/tests`
  - Confirm the task 2 exploration test now PASSES and the task 3 preservation tests still PASS, with the difference set limited to the explicit MODULO list
  - Confirm no rendered pod or container carries `seLinuxOptions` and none is privileged
  - Confirm the task 1 rollback is still complete: no `selinux.sharedLevel` reference remains in `charts/`, `tests/`, `examples/`, `my-values.yaml`, `README.md`, or `tuning/`
  - Confirm `geodata-local-pv` is absent (task 4.7). A surviving `Released` PV is a failure of the operational sequence, not a transient
  - **Do not treat a green suite as proof the relabel is fixed.** Confirm the recorded evidence set is complete: the V1 confirm signals, D1, D2 and D5, the reference label histogram, and the verified node replacement. The two uncaptured items are the optional follow-ups listed at the top of this plan and are not blockers
  - Confirm the mirror constraint held: nothing in this work deleted, moved, or re-synced mirrored data (3.1)
  - Ensure all tests pass, ask the user if questions arise
  - **RESULT — checkpoint passed.** Run against chart 0.2.0 with the live release at revision 4.
    - `helm lint --strict -f` for all three values files: `0 chart(s) failed`
    - `helm template` both example files: 13 kinds each, zero `geodata-local` or `localVolume` residue
    - `./scripts/check-placeholders.sh`: no unreplaced placeholders
    - `pytest tests/chart`: 90 passed, 1 skipped. `pytest agent/tests`: 44 passed
    - `seLinuxOptions` across `charts/`, `examples/`, `my-values.yaml`, `README.md`, `tuning/`: **0 hits**. `sharedLevel` across all six locations: present only in `tests/chart/test_preservation.py` as assertions that it is absent, plus stale `__pycache__` binaries
    - `geodata-local-pv` and `geodata-local-sc`: both `NotFound` in the cluster
    - `git diff --check`: clean
    - `kubectl apply --dry-run=server` — the only check not already covered by an earlier task, and the only one that validates against the real API including the Karpenter CRDs. Against `my-values.yaml`, the deployed configuration: **all 12 objects valid**, `EC2NodeClass` and `NodePool` included. Against `examples/values-example.yaml`: one rejection, `spec.persistentvolumesource: Forbidden: spec.persistentvolumesource is immutable after creation` on `geodata-source-pv`. **This is not a chart defect.** The example file carries the placeholder NFS server `10.0.0.10#geodata` while the live PV carries the real FSx server, and PV sources are immutable, so dry-running one environment's values against a cluster provisioned from another's collides on that field alone. Everything else in that render validated. It is the situation the README's "Replace the source PersistentVolume" procedure covers
    - The `kubectl.kubernetes.io/last-applied-configuration` warnings are expected: the objects were created by Helm rather than `kubectl apply`, so kubectl computes a patch instead of reporting `unchanged`. No drift is implied
  - _Requirements: 2.6, 2.8, 2.11, 3.8_
