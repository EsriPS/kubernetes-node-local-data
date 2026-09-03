# Bug condition exploration test verification — task 4.8

The bugfix-workflow verification gate. Re-runs the **same** test written in task 2 —
`tests/chart/test_selinux_relabel_overlap.py::test_relabeling_local_pv_over_a_shared_hostpath_root_is_rejected`,
parameterised over five `datasetRoot` values — against the fixed chart. On the unfixed chart it FAILED
for all five parameters, which was the success case for an exploration test and was recorded with PBT
status `passed`. It must now PASS.

No test file and no chart file was modified in this task. It is verification only, repository only. No
cluster-mutating operation was performed.

_Requirements: Expected Behavior Properties from design — 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7_

## Run context

| Item | Value |
|---|---|
| Timestamp (UTC) | 2026-09-03T18:48:39Z |
| Working directory | `/Users/cars3788/Development/carstenpiepel/kubernetes-node-local-data` |
| Git HEAD | `1e915d4` |
| Helm | `v4.2.4+g3900f43` |
| Python / pytest | `3.14.7` (`.venv/bin/python`) / `8.3.5` |
| hypothesis | `6.112.1`, profile `default` |
| Chart version | `charts/node-local-data/Chart.yaml` line 5: `version: 0.2.0`; line 6: `appVersion: "0.5.0"` |
| Templates present | `00`–`08`, `NOTES.txt`, `_helpers.tpl`, `_validate.tpl`. **No `09-local-pv.yaml`** |

## Result summary

| # | Check | Result |
|---|---|---|
| 1 | Exploration test in isolation, all 5 params | PASS — 6 passed (5 params + the edge case) |
| 2 | Each param rejected via the explanatory `_validate.tpl` message | PASS — 5 of 5 |
| 3 | Each param rejected **not** via a bare schema `additional properties` error | PASS — 0 of 5 |
| 4 | Rejection is a real `helm` non-zero exit, not a swallowed exception or a skip | PASS — `helm exit=1` for all 5 |
| 5 | The assertion is falsifiable — a non-explanatory failure message fails the test | PASS — probed |
| 6 | Counterexample cross-check against `evidence/counterexamples.md` | PASS — all automatable counterexamples now rejected |
| 7 | Full suite `.venv/bin/python -m pytest -q` | PASS — 93 passed, 1 skipped |
| 8 | The 1 skip is the pre-existing intended self-skip, and the only skip | CONFIRMED |
| 9 | `git diff --check` | PASS — no output, exit 0 |

**EXPECTED OUTCOME: test PASSES, confirming the overlap configuration is now unrepresentable. Met.**

## 1. Before / after test output contrast

### Before — unfixed chart, recorded in `evidence/counterexamples.md` (task 2)

```
Run: .venv/bin/python -m pytest tests/chart -q
Result: 5 failed, 17 passed
```

The five failures were the five parameterised exploration cases. `helm template` **succeeded** on every
one of them, rendering the relabeling `local` PV path and both non-relabeling `hostPath` paths as one
identical string:

| `datasetRoot` | distinct rendered paths | render |
|---|---|---|
| `/mnt/geodata` | 1 | succeeded (should fail) |
| `/mnt/geodata/` | 1 | succeeded (should fail) |
| `/mnt/alternate-geodata` | 1 | succeeded (should fail) |
| `/mnt/geodata//nested` | 1 | succeeded (should fail) |
| `/mnt/geodata/sibling/../nested` | 1 | succeeded (should fail) |

### After — fixed chart

```
$ .venv/bin/python -m pytest tests/chart/test_selinux_relabel_overlap.py -v
platform darwin -- Python 3.14.7, pytest-8.3.5, pluggy-1.6.0
hypothesis profile 'default'
collected 6 items

test_relabeling_local_pv_over_a_shared_hostpath_root_is_rejected[/mnt/geodata] PASSED                  [ 16%]
test_relabeling_local_pv_over_a_shared_hostpath_root_is_rejected[/mnt/geodata/] PASSED                 [ 33%]
test_relabeling_local_pv_over_a_shared_hostpath_root_is_rejected[/mnt/alternate-geodata] PASSED        [ 50%]
test_relabeling_local_pv_over_a_shared_hostpath_root_is_rejected[/mnt/geodata//nested] PASSED          [ 66%]
test_relabeling_local_pv_over_a_shared_hostpath_root_is_rejected[/mnt/geodata/sibling/../nested] PASSED[ 83%]
test_routing_only_configuration_renders_no_relabeling_volume PASSED                                    [100%]

6 passed in 0.60s
```

Chart suite as a whole: `49 passed, 1 skipped` (was `5 failed, 17 passed` at task 2; the count grew
because task 3 added the preservation suite after the counterexamples were recorded).

## 2. What makes it green now, versus red before

Red before: nothing in the chart rejected the configuration. `node-local-data.datasetRoot` interpolated
`.Values.datasetRoot` verbatim into `09-local-pv.yaml`'s `spec.local.path` and into both DaemonSet
`hostPath.path` values, `helm` exited 0, and `pytest.raises(subprocess.CalledProcessError)` saw no
exception. The test then fell through to `pytest.fail(counterexample_report(...))` inside the `with`
block, which is why the failure output carried the rendered paths rather than a bare `DID NOT RAISE`.

Green now: two independent changes, only the first of which is load-bearing today.

- **Task 4.1 removed the capability.** `templates/09-local-pv.yaml` is deleted, so `geodata-local-sc`
  and `geodata-local-pv` cannot be rendered at all. The relabeling-mount set is empty by construction.
- **Task 4.2 made the leftover key an explaining failure.** `_validate.tpl` fails on
  `hasKey .Values "localVolume"`. The test sets `--set localVolume.enabled=true`, so `helm` now exits
  non-zero, `CalledProcessError` is raised, and the assertion on the message is what the test actually
  checks.

The `pytest.raises` body is therefore never entered past its first statement, and
`rejection_explains_the_overlap(message, dataset_root)` is evaluated on the real `helm` stderr.

### 2a. Which rejection path each param takes — the point that needed checking

`rejection_explains_the_overlap` accepts **either** shape: `"localVolume" in message` (legacy-key
rejection) **or** an overlap message naming the path. A bare schema `Additional property localVolume is
not allowed` also contains the substring `localVolume`, so a green test alone does not distinguish the
explanatory rejection from an unexplained schema error. Verified directly against `helm` instead of
inferring it from the test result:

```
$ helm install node-local-data-test charts/node-local-data --dry-run \
    --namespace node-local-data-test --values examples/values-example.yaml \
    --set "datasetRoot=$r" --set localVolume.enabled=true
```

| `datasetRoot` | helm exit | rejection path | `_validate.tpl` frame | bare `additional propert*` lines | `datasetRoot %q` in message |
|---|---|---|---|---|---|
| `/mnt/geodata` | 1 | explanatory legacy-key | `08-path-shim.yaml:1:4` | 0 | `"/mnt/geodata"` |
| `/mnt/geodata/` | 1 | explanatory legacy-key | `08-path-shim.yaml:1:4` | 0 | `"/mnt/geodata"` |
| `/mnt/alternate-geodata` | 1 | explanatory legacy-key | `08-path-shim.yaml:1:4` | 0 | `"/mnt/alternate-geodata"` |
| `/mnt/geodata//nested` | 1 | explanatory legacy-key | `08-path-shim.yaml:1:4` | 0 | `"/mnt/geodata/nested"` |
| `/mnt/geodata/sibling/../nested` | 1 | explanatory legacy-key | `08-path-shim.yaml:1:4` | 0 | `"/mnt/geodata/nested"` |

All five take the **explanatory `_validate.tpl` legacy-key path**. Zero take a bare schema path. The
verbatim message, identical across all five apart from the interpolated path:

```
Error: INSTALLATION FAILED: execution error at (node-local-data/templates/08-path-shim.yaml:1:4):
localVolume was removed in chart 0.2.0 and must be deleted from your values file. The geocoding local
PersistentVolume it rendered is SELinux-relabeled on mount, so the first pod consuming it restamped the
whole shared tree with that pod's MCS categories and every other consumer on the node was denied.
Geocoding is now served from the same non-relabeling hostPath folder data store that routing already
uses, registered at datasetRoot "/mnt/geodata". Remove the localVolume block; there is no replacement
setting.
```

It names `localVolume` as removed, states the relabel mechanism, and points at the `hostPath` store as
the replacement — the three things 4.2 was for.

Why the bare schema error does not fire, and why that is deliberate: `values.schema.json` declares
`localVolume` **permissively**, with a description only and no type or property constraints. Helm
validates the schema before rendering templates, so under the top-level `additionalProperties: false`
an undeclared key would fail first and the operator would never see the explanation. The permissive
declaration is what lets the key reach `_validate.tpl`.

The last two rows also confirm the path normalization survives into the message: `clean` collapses
`/mnt/geodata//nested` and `/mnt/geodata/sibling/../nested` onto the same `/mnt/geodata/nested`.

The `pathsOverlap` forward guard (task 4.3) fires on **none** of the five. Expected: its
relabeling-mount set is empty today, since every entry in the `_validate.tpl` host-directory inventory
carries `relabelsOnMount: false`. The guard is retained for a future relabeling volume, not exercised
here. No message contains the substring `overlap`.

### 2b. Not passing vacuously

Three ways a green run could have been hollow, each ruled out:

- **No swallowed exception.** `helm` exits 1 for all five params, `conftest.render_chart` runs
  `subprocess.run(..., check=True)`, so `CalledProcessError` is genuinely raised and
  `pytest.raises` genuinely catches it.
- **No skip.** The exploration test has no `skipif`, no `xfail`, and no `pytest.skip` on any path. The
  run reports `PASSED`, not `SKIPPED` or `XFAIL`.
- **The assertion can fail.** `rejection_explains_the_overlap` was called directly against a
  non-explanatory failure message for each of the five params:

  | probe message | result for all 5 params |
  |---|---|
  | `Error: INSTALLATION FAILED: something else entirely` | `False` — test would fail |
  | bare schema `Additional property localVolume is not allowed` | `True` — accepted, which is why 2a checks `helm` directly |

  A rejection for the wrong reason is therefore caught by the assertion, and the one shape the
  assertion cannot distinguish is excluded by the direct `helm` evidence above.

## 3. Counterexample cross-check against `evidence/counterexamples.md`

### Counterexample 1 — the overlap renders silently (requirement 1.1)

Every configuration that previously demonstrated the bug is now rejected. This is the full set from the
task 2 record:

| Counterexample configuration | Task 2 (unfixed) | Task 4.8 (fixed) | Rejection path |
|---|---|---|---|
| `datasetRoot=/mnt/geodata`, `localVolume.enabled=true` | rendered, 1 distinct path | REJECTED | explanatory legacy-key |
| `datasetRoot=/mnt/geodata/`, `localVolume.enabled=true` | rendered, 1 distinct path | REJECTED | explanatory legacy-key |
| `datasetRoot=/mnt/alternate-geodata`, `localVolume.enabled=true` | rendered, 1 distinct path | REJECTED | explanatory legacy-key |
| `datasetRoot=/mnt/geodata//nested`, `localVolume.enabled=true` | rendered, 1 distinct path | REJECTED | explanatory legacy-key |
| `datasetRoot=/mnt/geodata/sibling/../nested`, `localVolume.enabled=true` | rendered, 1 distinct path | REJECTED | explanatory legacy-key |

The three rendered mounts named in the counterexample —
`PersistentVolume/geodata-local-pv .spec.local.path` (RELABELING),
`DaemonSet/geodata-path-shim` volume `dataset-root`, and `DaemonSet/geodata-sync-agent` volume
`node-local-data` — can no longer coincide, because the first of the three is not renderable at all.

The counterexample file's recorded **edge case** — `localVolume` left disabled renders no
`PersistentVolume/geodata-local-pv` and no PV using the `local` plugin — is now the permanent and only
state, and is asserted by `test_routing_only_configuration_renders_no_relabeling_volume`, which passes.

### Counterexamples 2–4 — agent side

Recorded in task 2 as **observed in code, not written as tests**, and deliberately so: a test asserting
graceful degradation would encode task 6, which the plan marks optional and independently skippable.
They are therefore **out of scope for this gate** and unchanged by task 4:

- **CE2** — `list_datasets` propagates `PermissionError` (`agent/sync_agent.py:120-124`).
- **CE3** — readiness freezes because the process exits; no `try` anywhere in the `main()` chain.
- **CE4** — rclone stderr swallowed by `capture_output=True` (`run()`, lines 112-113).

Under Option 1 the fix does not depend on any of them: there is one managed root and nothing in the
chart can relabel it, so the denial that triggered these paths is not reachable from a chart
configuration. Their disposition is task 6's decision, not this gate's.

### Already observed live, not automatable

The remaining counterexamples in the task 2 record — the label surviving deregistration, the `local_t`
versus `data_t` type difference, two pods of one ReplicaSet holding different MCS pairs, `chcon -R`
failing every `setxattr`, and a node advertising correct labels while unreadable — are runtime
observations. None is reachable from `helm template` and none is re-checkable here. They are covered by
the recorded live evidence set.

## 4. Fix Checking scope — Phase 1 only

This gate covers **Phase 1** of Fix Checking: the configuration expressing the bug is unrepresentable.

Phase 2's label and access assertions need an SELinux-enforcing node and are **not** covered here. A
real relabel cannot be reproduced by `helm template`: it needs an enforcing Bottlerocket node, a
relabeling volume, and a real pod, and because the label is written per inode on disk (D1) and cannot be
relabeled back (F1), triggering one costs a node. Phase 2 rests on the recorded evidence:

- the reference label histogram from `ip-10-0-37-12`;
- all 8 pool nodes observed at `system_u:object_r:local_t:s0` with a successful read;
- the geocode pod reading the shared tree as `container_t:s0:c40,c511`;
- replacement node `ip-10-0-39-129` reaching `gen=g57a70ff5b429 count=3` with `data-not-ready` cleared.

**A green chart suite is never proof that the relabel is fixed.** This note is also carried in the test
file's module docstring.

## 5. Full suite

```
$ .venv/bin/python -m pytest -q -rs
...............................................................s........ [ 76%]
......................                                                   [100%]
=========================== short test summary info ============================
SKIPPED [1] tests/chart/test_preservation.py:731: ('localVolume.enabled=true',) is no longer representable; nothing to render
93 passed, 1 skipped in 5.34s
```

Expected 93 passed, 1 skipped. Met. Split: `tests/chart` 49 passed + 1 skipped, `agent/tests` 44
passed — 94 total, so the full run covers both suites and nothing else.

### The single skip, confirmed

`test_no_rendered_pod_or_container_carries_selinux_options[localVolume.enabled=true]` in
`tests/chart/test_preservation.py`.

- **It is the only skip.** `-rs` reports exactly one `SKIPPED` line across the whole run.
- **It is pre-existing and anticipated.** The test's own docstring, written at task 3 against the
  unfixed chart, states that this variant "renders now and is rejected after the fix; the SELinux
  assertion is asserted only on the renders that succeed, so the case survives either way." The skip is
  the designed post-fix outcome, not an adjustment made here.
- **It skips for the intended reason.** The `except subprocess.CalledProcessError: pytest.skip(...)`
  arm is reached because the render is rejected — and section 2a establishes independently that the
  rejection is the explanatory legacy-key failure, so the skip is the fix firing, not an unrelated
  render error.
- **It masks no failure.** The assertion it guards is `seLinuxOptions` absent and no container
  privileged. That assertion still runs on the other three parameters of the same test — default,
  `pathShim.enabled=false`, `warmCapacity.enabled=false` — all passing, and it cannot regress on the
  skipped variant because that variant renders nothing at all.

## 6. `git diff --check`

```
$ git diff --check
(no output)
git-diff-check-exit=0
```

No whitespace errors and no conflict markers in the tracked-file edits.

## Conclusion

The task 2 exploration test passes on the fixed chart for all five parameters, and passes for the right
reason: each is rejected by the explanatory `_validate.tpl` legacy-key failure naming `localVolume` as
removed and pointing at the shared `hostPath` store, with a real non-zero `helm` exit, zero bare schema
errors, and a falsifiable assertion. Every automatable counterexample from task 2 is now rejected. The
full suite is 93 passed, 1 skipped, with the single skip confirmed as the anticipated self-skip of the
now-unrepresentable variant.

Phase 1 of Fix Checking is satisfied. Nothing was fixed or modified in this task; it is verification
only. No cluster or node state was touched, and no mirrored data was deleted, moved, or re-synced.
