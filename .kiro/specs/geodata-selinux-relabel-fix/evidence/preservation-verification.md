# Preservation test verification — task 4.9

The second bugfix-workflow verification gate. Re-runs the **same** 25 preservation cases written in
task 3 — `tests/chart/test_preservation.py` — against the fixed chart. They were written and recorded
green against the **unfixed** (post-rollback) chart, so their whole purpose is to prove the fix changed
only what it was licensed to change. A difference outside the closed MODULO set is a regression, not an
intended change.

No test file, no baseline file, and no chart file was modified in this task. It is verification only,
repository only. **`PRESERVATION_BASELINE_RECORD` was never set.** No cluster-mutating operation was
performed, and no mirrored data was deleted, moved, or re-synced.

_Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9_

## Run context

| Item | Value |
|---|---|
| Timestamp (UTC) | 2026-09-03T19:02:41Z |
| Working directory | `/Users/cars3788/Development/carstenpiepel/kubernetes-node-local-data` |
| Git HEAD | `1e915d4` |
| Helm | `v4.2.4+g3900f43` |
| Python / pytest | `3.14.7` (`.venv/bin/python`) / `8.3.5` |
| hypothesis | `6.112.1`, profile `default`, pinned in `tests/chart/requirements-dev.txt:3` |
| Chart version | `0.2.0` |
| Live release | revision 4; `geodata-local-pv` and `geodata-local-sc` absent from chart and cluster |
| `PRESERVATION_BASELINE_RECORD` | **unset** — confirmed from the process environment and read back through the module constant |

## Result summary

| # | Check | Result |
|---|---|---|
| 1 | Preservation suite in isolation | **24 passed, 1 skipped** of 25 collected |
| 2 | Recorded pre-fix baselines compared, not regenerated | CONFIRMED — sha256 and mtime unchanged across every run |
| 3 | Nothing outside the MODULO set differed | CONFIRMED — both baseline comparisons byte-identical |
| 4 | MODULO members enumerated with exercised status | 1 of 4 exercised in this suite; 3 are documented no-ops |
| 5 | The single skip is the intended `localVolume.enabled=true` variant | CONFIRMED — and structurally the only skip possible |
| 6 | Not passing vacuously — falsifiability probed 4 ways | CONFIRMED — every probe failed as it must |
| 7 | Full suite `.venv/bin/python -m pytest -q` | **93 passed, 1 skipped** |
| 8 | `git diff --check` | PASS — no output, exit 0 |
| 9 | Baseline files unmodified | CONFIRMED — see section 6 |

**EXPECTED OUTCOME: tests PASS, identical modulo the explicit MODULO set. Met. No regressions.**

## 1. The preservation suite in isolation

```
$ .venv/bin/python -m pytest tests/chart/test_preservation.py -v -rs
platform darwin -- Python 3.14.7, pytest-8.3.5, pluggy-1.6.0
hypothesis profile 'default'
collected 25 items

test_routing_only_render_is_identical_modulo_the_difference_set PASSED           [  4%]
test_routing_only_render_contains_no_relabeling_volume PASSED                   [  8%]
test_registered_routing_path_defaults_to_the_documented_mount PASSED            [ 12%]
test_registered_routing_path_follows_dataset_root_everywhere PASSED             [ 16%]
test_sync_pass_invocation_shape_is_unchanged PASSED                             [ 20%]
test_chart_passes_the_recorded_sync_arguments PASSED                            [ 24%]
test_generation_digest_and_count_match_the_recorded_values[datasets0-none-0] PASSED           [ 28%]
test_generation_digest_and_count_match_the_recorded_values[datasets1-gda38dcffebc1-1] PASSED  [ 32%]
test_generation_digest_and_count_match_the_recorded_values[datasets2-g4d7d3b6aedbf-3] PASSED  [ 36%]
test_generation_digest_and_count_match_the_recorded_values[datasets3-g7d247bb41d03-3] PASSED  [ 40%]
test_generation_digest_is_order_independent_and_within_the_label_limit PASSED    [ 44%]
test_write_generation_label_still_suppresses_identical_writes PASSED             [ 48%]
test_write_generation_label_rewrites_when_labels_are_unreadable PASSED           [ 52%]
test_node_pool_and_node_class_are_byte_identical PASSED                          [ 56%]
test_unrelated_values_never_disturb_node_identity PASSED                         [ 60%]
test_disruption_settings_and_ami_alias_are_pinned PASSED                         [ 64%]
test_no_rendered_pod_or_container_carries_selinux_options[set_values0] PASSED    [ 68%]
test_no_rendered_pod_or_container_carries_selinux_options[set_values1] PASSED    [ 72%]
test_no_rendered_pod_or_container_carries_selinux_options[set_values2] PASSED    [ 76%]
test_no_rendered_pod_or_container_carries_selinux_options[set_values3] SKIPPED   [ 80%]
test_chart_surfaces_carry_no_shared_selinux_level_setting PASSED                 [ 84%]
test_shared_prefix_siblings_are_not_overlapping_paths PASSED                     [ 88%]
test_shared_prefix_sibling_roots_both_render[/mnt/geodata] PASSED                [ 92%]
test_shared_prefix_sibling_roots_both_render[/mnt/geodata-geocoding] PASSED      [ 96%]
test_both_example_values_files_still_render PASSED                               [100%]
=========================== short test summary info ============================
SKIPPED [1] tests/chart/test_preservation.py:731: ('localVolume.enabled=true',) is no longer representable; nothing to render
======================== 24 passed, 1 skipped in 2.54s =========================
```

**The exact expected split is 24 passed + 1 skipped of 25 collected.** Not 25 passed. The skip is the
designed post-fix outcome of the one parameter whose values shape the fix deliberately made
unrepresentable, and it was anticipated in the test's own docstring at task 3. Every other case,
including all seven Case-group behaviours, passes unchanged.

Note on the identifier: the skipping case is the `localVolume.enabled=true` variant of
`test_no_rendered_pod_or_container_carries_selinux_options`, but its pytest node id is
`[set_values3]`, not `[localVolume.enabled=true]` — the parametrize argument is a tuple, so pytest
generates a positional id. The `-rs` reason line is what names the variant:
`('localVolume.enabled=true',) is no longer representable`. Section 5 establishes the mapping directly.

## 2. The recorded baseline was compared, not regenerated

### How the mechanism works

`tests/chart/test_preservation.py` reads two recorded files:

```python
BASELINE_DIR = Path(__file__).resolve().parent / "preservation_baseline"
ROUTING_ONLY_BASELINE = BASELINE_DIR / "routing_only_render.yaml"
NODE_IDENTITY_BASELINE = BASELINE_DIR / "node_identity.yaml"
RECORD_ENVIRONMENT_VARIABLE = "PRESERVATION_BASELINE_RECORD"

def require_baseline(path):
    assert path.exists(), (... "must not be re-recorded after the fix")
    return path.read_text(encoding="utf-8")

def recorded_baseline(path, observed):
    if os.environ.get(RECORD_ENVIRONMENT_VARIABLE):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(observed, encoding="utf-8")
    return require_baseline(path)
```

The single write in the whole module is inside `recorded_baseline`, gated on
`os.environ.get("PRESERVATION_BASELINE_RECORD")`. With the variable unset, `recorded_baseline`
degenerates to `require_baseline` — a read of an existing file that fails loudly when the file is
missing. There is no other write path, no `mkdir` outside that branch, and no fallback that substitutes
the observed value for a missing baseline.

### What guarantees the comparison actually happened

Four independent facts, not one:

1. **The variable was never set.** `env | grep PRESERVATION_BASELINE_RECORD` returns nothing, and the
   module constant read back through the interpreter reports `currently set: False`. No command in this
   task set it, exported it, or prefixed a run with it.
2. **The baseline bytes are unchanged.** sha256 taken before the first run, after the isolated
   preservation run, and after the full suite is identical in all three snapshots
   (`60ce2b5b…dc73`, `1112246d…a581`), and mtimes stayed at `13:50:21Z` / `13:50:30Z`. A recording run
   would have rewritten both files and moved both mtimes.
3. **The baselines predate the fix.** Baseline mtimes are `13:50`; every task-4 chart edit is later —
   `values.yaml 14:02`, `values.schema.json 14:08`, `_validate.tpl 14:13`, `_helpers.tpl 14:18`,
   `NOTES.txt 14:22`, and `templates/09-local-pv.yaml` deleted. The recorded observation is therefore
   from the unfixed chart, exactly as task 3 required.
4. **A comparison that did not happen would not fail.** Probes 2 and 3 in section 6 show that an empty
   or missing baseline produces a failure, not a pass — so a green run cannot be the result of the
   comparison being skipped.

### The comparison result, measured directly

| Comparison | Observed bytes | Baseline bytes | Equal |
|---|---|---|---|
| Routing-only render (`normalize` of the full render) | 10089 | 10089 | **True** |
| Node identity (EC2NodeClass + NodePool) | 2646 | 2646 | **True** |
| Rendered resource identity set | 13 | 13 | **True** |

The 13 identities, all matching the pre-fix baseline: `ClusterRole/geodata-sync-agent`,
`ClusterRoleBinding/geodata-sync-agent`, `DaemonSet/geodata-path-shim`, `DaemonSet/geodata-sync-agent`,
`Deployment/geodata-warm-capacity`, `EC2NodeClass/geodata-nodes`, `Namespace/geodata-sync`,
`NodePool/geodata-nodes`, `PersistentVolume/geodata-source-pv`,
`PersistentVolumeClaim/geodata-source-pvc`, `PriorityClass/geodata-warm-capacity`,
`ServiceAccount/geodata-sync-agent`, `StorageClass/geodata-source-sc`.

**Nothing outside the MODULO set differed.** Byte-identity over the normalized render is what
establishes that: the normalizer factors out only the four named members, so any other change survives
into the compared string.

## 3. The MODULO difference set, enumerated with exercised status

The literal from the test file, unchanged:

```python
MODULO_DIFFERENCE = frozenset({
    "removedLocalVolumeResources",
    "chartVersion",
    "removedSELinuxSecurityContexts",
    "notesText",
})
```

| Member | Normalizer mechanism | Exercised by **this** run? | Evidence |
|---|---|---|---|
| `removedLocalVolumeResources` | `is_removed_local_volume_resource` drops `StorageClass *-local-sc` and `PersistentVolume *-local-pv` | **No — filter dropped nothing.** The pre-fix baseline was recorded at the chart default, where `localVolume.enabled` was already false, so no local PV or SC existed in it either | `filter dropped: NOTHING (no-op this run)`; the baseline's 13 resources contain no `local-pv`/`local-sc` |
| `chartVersion` | `_strip_keys` removes `helm.sh/chart` at any depth | **No — documented no-op.** 0 occurrences in the render; the chart emits no `helm.sh/chart` label. The version did move `0.1.1 → 0.2.0`, but never into a manifest | `occurrences of ('helm.sh/chart',): 0` |
| `removedSELinuxSecurityContexts` | `_strip_keys` removes `seLinuxOptions` at any depth | **No — no-op.** The task 1 rollback removed them *before* the baseline was recorded, so neither side carries any. 0 occurrences | `occurrences of ('seLinuxOptions',): 0` |
| `notesText` | The NOTES body is excluded from the byte comparison entirely — `normalize` takes manifests only | **Yes — the one member actually exercised** | `NOTES.txt` rewritten in task 4.5 (mtime `14:22` vs baseline `13:50`); `'localVolume' in NOTES: False`; the removed local-volume registration block and the two-stage paragraph are recorded in `option1-sequence.md` |

Three of four members being unexercised is the correct and expected result, and both no-ops are
documented as such in the test module's own docstring. It is worth being precise about why, because
"unexercised" here does not mean "unremoved":

- **The local PV and SC really are gone** — `templates/09-local-pv.yaml` is deleted, and the recorded
  live delta from release revision 3 to revision 4 in `option1-sequence.md` shows exactly two removed
  objects, `geodata-local-sc` and `geodata-local-pv`, with 12 objects unchanged. That render used
  `my-values.yaml`, which carried `localVolume.enabled: true`. The preservation suite renders
  `examples/values-example.yaml` at the chart default instead, which never had them. So the member is
  exercised on the operator's values path and is a no-op on the preservation path.
- **The removal is still asserted in this suite**, just not through the byte comparison:
  `test_routing_only_render_contains_no_relabeling_volume` asserts no rendered `PersistentVolume` uses
  the `local` plugin, and the `set_values3` skip is the direct consequence of the key being rejected.
- **`notesText` is not unchecked.** The one load-bearing property of the NOTES survives as an
  assertion: `notes_dataset_paths(notes) == {datasetRoot}`, verified as `{'/mnt/geodata'}`. An operator
  is still told to register exactly one path, and it is still `datasetRoot`. The `seLinuxOptions` case
  additionally appends the NOTES body to the string it scans, so the notes cannot smuggle in an SELinux
  level.

Task 5.4 will restate this set as explicit test cases. This section verifies and reports it only.

## 4. What each preservation case proved against the fixed chart

| Case | Requirement | Outcome |
|---|---|---|
| 1 — routing-only render byte-identical modulo the set | 3.2, 3.8 | PASS, 10089 bytes both sides |
| 1 — no relabeling volume in the render | 3.2 | PASS, no PV uses the `local` plugin |
| 2 — registered routing path: Path Shim `hostPath`, agent `hostPath`, `--dest-root`, bind target, NOTES path all exactly `/mnt/geodata` | 3.5 | PASS, example-based and property-based (8 generated roots) |
| 3 — one recursive `rclone sync`, `--metadata` present, `--inplace` absent, excludes and concurrency flags unchanged | 3.3 | PASS, argv shape and chart-passed args both |
| 4 — generation digest and count for the four recorded name sets; digest order-independent and ≤63 chars; `write_generation_label` still suppresses identical writes and still rewrites unknown labels | 3.3, 3.9 | PASS, 200 generated examples on the order-independence property |
| 5 — NodePool and EC2NodeClass byte-identical; no unrelated value disturbs them; `amiAlias`, disruption, `expireAfter`, taints pinned | 3.7, 3.9 | PASS, 2646 bytes both sides, 7 unrelated overrides |
| 6 — no `seLinuxOptions` and no privileged container on any rendered pod | 3.4, 3.6 | PASS on 3 of 4 variants; 4th skipped (section 5) |
| 7 — `/mnt/geodata` and `/mnt/geodata-geocoding` both render and do not overlap | 3.2, 3.5, 3.8 | PASS, the guard does not over-reject |
| Both example values files render | 3.8 | PASS |

Case 5 is the one that matters most operationally: NodePool and EC2NodeClass byte-identity is what
guarantees the fix marked no NodeClaim Drifted, and therefore that no node lost its multi-hundred-GB
mirror as a side effect (3.1, 3.7, 3.9).

## 5. The single skip, confirmed

```
$ .venv/bin/python -m pytest "tests/chart/test_preservation.py::test_no_rendered_pod_or_container_carries_selinux_options" -v -rs
...[set_values0] PASSED   # ()
...[set_values1] PASSED   # ('pathShim.enabled=false',)
...[set_values2] PASSED   # ('warmCapacity.enabled=false',)
...[set_values3] SKIPPED  # ('localVolume.enabled=true',)
SKIPPED [1] tests/chart/test_preservation.py:731: ('localVolume.enabled=true',) is no longer representable; nothing to render
3 passed, 1 skipped in 0.35s
```

- **It is the `localVolume.enabled=true` variant.** `SELINUX_VARIANT_VALUES` is
  `((), ('pathShim.enabled=false',), ('warmCapacity.enabled=false',), ('localVolume.enabled=true',))`,
  and the skip reason interpolates the tuple, naming it outright.
- **It is the only skip, structurally.** A grep across `tests/` and `agent/tests/` for
  `pytest.skip|skipif|xfail` returns exactly one hit: `test_preservation.py:731`. No other test in the
  repository can skip, so `-rs` reporting one `SKIPPED` line is not a matter of luck.
- **It skips for the intended reason.** The `except subprocess.CalledProcessError: pytest.skip(...)` arm
  is reached because the render is rejected, and the rejection is the explanatory task-4.2 failure, not
  an unrelated render error:

```
$ helm install node-local-data-test charts/node-local-data --dry-run \
    --namespace node-local-data-test --values examples/values-example.yaml \
    --set localVolume.enabled=true
Error: INSTALLATION FAILED: execution error at (node-local-data/templates/08-path-shim.yaml:1:4):
localVolume was removed in chart 0.2.0 and must be deleted from your values file. ...
Geocoding is now served from the same non-relabeling hostPath folder data store that routing already
uses, registered at datasetRoot "/mnt/geodata". Remove the localVolume block; there is no replacement
setting.
```

- **It is anticipated, not adjusted.** The docstring written at task 3 against the unfixed chart states
  the variant "renders now and is rejected after the fix; the SELinux assertion is asserted only on the
  renders that succeed, so the case survives either way." Nothing was changed here to produce the skip.
- **It masks no failure.** The assertion it guards — `seLinuxOptions` absent, `sharedLevel` absent, no
  container privileged — still runs on the other three parameters, all passing, and cannot regress on
  the skipped variant because that variant renders nothing at all.

## 6. Not passing vacuously — four falsifiability probes

The concern is a preservation suite that is green for a hollow reason: an empty baseline, a comparison
that short-circuits when the baseline is missing, or an assertion that cannot fail. All probes ran
against **temporary copies** under `/tmp`. The committed baselines and test file were never touched, and
`PRESERVATION_BASELINE_RECORD` was never set in any probe.

Probe harness: `tests/` copied to a temp root, with `charts/`, `examples/`, and `agent/` reachable from
it, so `Path(__file__).resolve().parents[2]` resolves to the temp root and `BASELINE_DIR` resolves to
the temp copy of the baselines. Harness fidelity was checked first — the three baseline-comparison tests
**pass** in the unmutated probe copy, so a failure in a probe is attributable to the mutation.

| # | Probe | Expected | Result |
|---|---|---|---|
| 1 | Mutate the temp baseline copies: `--interval=300 → 301` in the routing render, `expireAfter: Never → 720h` in the node identity | both comparisons fail | **3 failed** — `test_routing_only_render_is_identical_modulo_the_difference_set`, `test_node_pool_and_node_class_are_byte_identical`, `test_unrelated_values_never_disturb_node_identity`, each with a diff naming the mutated token |
| 2 | Truncate both temp baselines to **empty files** | fail, not pass | **3 failed.** An empty baseline is not a free pass |
| 3 | **Delete** both temp baselines | fail loudly, no short-circuit | **3 failed** with `AssertionError: missing recorded baseline routing_only_render.yaml; it is observed from the unfixed chart by running once with PRESERVATION_BASELINE_RECORD=1 and must not be re-recorded after the fix` |
| 4 | Leave the baselines byte-identical to the committed ones and instead drift a **temp copy of the chart**: `intervalSeconds: 300 → 301` in `values.yaml`, `expireAfter: Never → 720h` in `03-nodepool.yaml` | fail — the comparison must catch chart-side drift, which is the direction that matters | **4 failed** — the three comparison tests plus `test_disruption_settings_and_ami_alias_are_pinned` |

Probe 4 is the decisive one. Probes 1–3 show the assertion is sensitive to the baseline; probe 4 shows
it is sensitive to the chart, which is the direction a regression would actually arrive from. A
one-token change to either the values default or a template is detected. The assertion can fail, so its
passing carries information.

All four temp probe trees were removed after the runs.

## 7. Baseline-file integrity

The baselines live under `tests/`, which is **untracked** (2.12), so `git status` reports it only as
`?? tests/` and cannot show a per-file modification. Integrity is therefore established by content hash
and mtime, taken at three points — before the first run, after the isolated preservation run, and after
the full suite:

```
60ce2b5b8c8b776ff86b00dba0119a97839ddf3e9b1576de3324abf51d23dc73  routing_only_render.yaml   10089 bytes  mtime 2026-09-03T13:50:21Z
1112246d93cf4b94bddf17a81676ae896c9f38adb69a8feae78a7db5d776a581  node_identity.yaml          2646 bytes  mtime 2026-09-03T13:50:30Z
004cd15dbd3b08d4970e0baad62b8a30444d7d9938afcf248b2abcae61ef4c48  test_preservation.py       31594 bytes  mtime 2026-09-03T13:51:43Z
```

Identical in all three snapshots. Both baselines still carry the pre-fix render: 13 resources, none of
them `geodata-local-pv` or `geodata-local-sc`, which is what a `localVolume.enabled=false` render looked
like before the key was removed — and is why the current render matches it byte for byte.

`git status --porcelain` is unchanged from the start of this task. The in-progress unrelated
modifications — `README.md`, `RESULTS.md`, `tuning/gotchas.md`, `tuning/nvme.md`, `tuning/rclone.md`,
`agent/sync_agent.py`, `agent/Dockerfile`, `agent/tests/*`, `.gitignore`, `CONTRIBUTING.md`,
`scripts/check-placeholders.sh`, the deleted `manifests/` — are all untouched by this task. No
`git checkout` and no revert of any kind was run.

```
$ git diff --check
(no output)
git-diff-check-exit=0
```

## 8. Full suite

```
$ .venv/bin/python -m pytest -q -rs
...............................................................s........ [ 76%]
......................                                                   [100%]
=========================== short test summary info ============================
SKIPPED [1] tests/chart/test_preservation.py:731: ('localVolume.enabled=true',) is no longer representable; nothing to render
93 passed, 1 skipped in 5.05s
```

Expected 93 passed, 1 skipped. **Met**, and unchanged from the task 4.8 gate. Per-suite split:
`tests/chart` 49 passed + 1 skipped, `agent/tests` 44 passed — 94 total, so the run covers both suites
and nothing else.

## Scope note — what a green preservation suite does and does not prove

It proves the fix changed only what the MODULO set licenses: the routing-only render, the registered
routing path, the rclone invocation, the readiness identity, and the node-identity resources are all
what they were before the fix. It does **not** prove the relabel is fixed. A real SELinux relabel cannot
be reproduced by `helm template`; it needs an enforcing Bottlerocket node, a relabeling volume, and a
real pod, and because the label is written per inode on disk (D1) and cannot be relabeled back (F1),
triggering one costs a node. The runtime side rests on the recorded evidence set: the reference label
histogram from `ip-10-0-37-12`, all 8 pool nodes at `system_u:object_r:local_t:s0` with successful
reads, the geocode pod reading as `container_t:s0:c40,c511`, and `ip-10-0-39-129` reaching
`gen=g57a70ff5b429 count=3` with `data-not-ready` cleared.

**A green chart suite is never proof that the relabel is fixed.** This note is also carried in the test
module's docstring.

## Conclusion

The 25 task-3 preservation cases run unchanged against the fixed chart: **24 pass, 1 skips** for the
anticipated reason, and the two byte-identical comparisons match the **pre-fix** recorded baselines
exactly — 10089 and 2646 bytes, 13 of 13 resource identities, with `PRESERVATION_BASELINE_RECORD` never
set and both baseline files provably unmodified since `13:50`, before any task-4 chart edit. Of the four
MODULO members, only `notesText` was exercised in this suite; `removedLocalVolumeResources` is exercised
on the operator values path and recorded in `option1-sequence.md`, and `chartVersion` and
`removedSELinuxSecurityContexts` are documented no-ops. Nothing outside the set differed. Four
falsifiability probes on temporary copies each failed as required, including a chart-side drift probe,
so the green run carries information rather than being vacuous. Full suite: 93 passed, 1 skipped.

No regressions. Nothing was fixed or modified in this task; it is verification only. No cluster or node
state was touched, and no mirrored data was deleted, moved, or re-synced (3.1).
