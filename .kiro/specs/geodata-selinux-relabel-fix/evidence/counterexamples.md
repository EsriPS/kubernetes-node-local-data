# Bug condition counterexamples (task 2)

Recorded against the **rolled-back but unfixed** chart, after task 1 and before task 4.

Test file: `tests/chart/test_selinux_relabel_overlap.py`
Run: `.venv/bin/python -m pytest tests/chart -q`
Result: `5 failed, 17 passed`. The five failures are the five parameterised exploration
cases. Every pre-existing chart test still passes, so the failures are the bug and not
collateral damage.

## Scope of what these counterexamples prove

A real SELinux relabel cannot be reproduced by `helm template`. It needs an enforcing
Bottlerocket node, a relabeling `local` PV, and a real pod, and because the label is
written per inode on disk (D1) and cannot be relabeled back (F1), triggering one costs a
node. These counterexamples prove only that the **configuration expressing the bug is
representable**. The runtime behaviour is covered by the recorded live evidence — the V1
confirm signals, D1, D2, D5, the reference label histogram, and the verified node
replacement. This note is also written into the test file itself.

## Counterexample 1 — the overlap renders silently (requirement 1.1)

`helm template` succeeds when it should be rejected. The relabeling `local` PV path and
both non-relabeling `hostPath` paths render as **one identical string**, for every
`datasetRoot` tried.

Reproduced for `datasetRoot=/mnt/geodata` with `localVolume.enabled=true`:

```
datasetRoot='/mnt/geodata'
helm template SUCCEEDED. It should have been rejected.
Rendered mount paths (1 distinct value):
  PersistentVolume/geodata-local-pv .spec.local.path                    (RELABELING)
    '/mnt/geodata'
  DaemonSet/geodata-path-shim  volume 'dataset-root'    .hostPath.path  (NOT relabeled)
    '/mnt/geodata'
  DaemonSet/geodata-sync-agent volume 'node-local-data' .hostPath.path  (NOT relabeled)
    '/mnt/geodata'
```

The ArcGIS routing folder data store registers this same path organization-wide. It is
not a chart resource, so the Path Shim is its in-chart stand-in — the Path Shim exists
precisely to guarantee that registered path on every schedulable node.

### The overlap is structural, not tied to the default path

All five parameterised `datasetRoot` values produced one distinct path across all three
mounts, and all five rendered successfully:

| `datasetRoot` | distinct rendered paths | render |
|---|---|---|
| `/mnt/geodata` | 1 | succeeded (should fail) |
| `/mnt/geodata/` | 1 | succeeded (should fail) |
| `/mnt/alternate-geodata` | 1 | succeeded (should fail) |
| `/mnt/geodata//nested` | 1 | succeeded (should fail) |
| `/mnt/geodata/sibling/../nested` | 1 | succeeded (should fail) |

`node-local-data.datasetRoot` in `_helpers.tpl` interpolates `.Values.datasetRoot`
verbatim with no normalization, so every consumer receives the same raw string. The
denormalized and nested forms are included because a lexical `clean` collapses them onto
the same directory — `pathsOverlap` must catch them, and today nothing checks at all.

Edge case, passing on unfixed code and recorded as the baseline the preservation tests
lock in: with `localVolume` left disabled, no `PersistentVolume/geodata-local-pv` is
rendered and no rendered PV uses the `local` plugin, so nothing can relabel the tree.
This is the permanent state under the selected fix.

## Counterexamples 2-4 — agent side, observed in code, not yet reproduced as tests

These were **not** written as tests in this task. Two reasons, both for the orchestrator
to arbitrate before task 4: the task instruction as delivered scoped this task to the
chart render test, and a test asserting the graceful-degradation behaviour would encode
task 6, which the plan marks optional and independently skippable — a test that stays red
if task 6 is deferred is worse than no test. The defects themselves are confirmed by
direct reading of `agent/sync_agent.py`.

**Counterexample 2 — `list_datasets` propagates `PermissionError` (requirement 1.5).**
`list_datasets` at `agent/sync_agent.py:120-124` guards only `directory.is_dir()` and then
calls `directory.iterdir()`, which raises `PermissionError: [Errno 13]` on a denied
directory. There is no handler. Reproducible with mode bits or a monkeypatch; no SELinux
needed.

**Counterexample 3 — readiness freezes because the process exits (requirement 1.5).**
`sync_pass` calls `list_datasets(dest_root)` at line 229, and `main()` calls it again at
line 435. Both call sites sit in the `while True:` loop at line 429 with **no `try`
anywhere in the chain**, so the `PermissionError` reaches the top and the process exits.
`reconcile_node_state` at line 438 is never reached, so the readiness taint and the
dataset-generation and dataset-count labels freeze at whatever they last were. This is
the mechanism behind the observed `CrashLoopBackOff` — 269 restarts when first seen, 379
after the local-PV consumer was removed — and behind the node advertising correct labels
while unreadable.

**Counterexample 4 — rclone output is swallowed (F7).** `run()` at line 112-113 is
`subprocess.run(cmd, check=check, text=True, capture_output=True)`, so
`--stats=60s --stats-one-line -v` produces nothing until the pass returns. The 58.9-minute
first sync on the replacement node logged nothing after its startup banner and progress
had to be inferred from the filesystem.

## Already observed live, not automatable

Carried from the design's evidence set so this file is a complete counterexample record:

- The affected node's label unchanged after the data store was deregistered, the PVC
  deleted, and every referencing pod removed — the counterexample to mount-scoped
  labeling and to "removing the consumer is the repair".
- A **type** difference, not only a level difference: `local_t:s0` on healthy nodes versus
  `data_t:s0:c20,c476` on the affected one — the counterexample to a categories-only
  repair.
- Two pods of one ReplicaSet on one node holding different MCS pairs — the counterexample
  to any per-service label alignment.
- `chcon -R` reading the whole tree and failing every `setxattr` with `Permission denied`
  — the counterexample to repair-in-place, and the reason replacement is the procedure.
- A node advertising a correct generation, count, and no taint while unreadable, with
  every pod on it ready — the counterexample to readiness-as-health.
- Excluded as causes: mode bits, ownership, missing path, and `hostPath.type`, all ruled
  out by the 0755 root:root directory and the successful readers on other nodes; and
  mount-scoped labeling, excluded by `/proc/mounts` showing `seclabel` and no `context=`.
