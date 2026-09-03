# Rollback validation — task 1.5

Proves the task 1.1–1.4 rollback of the prior session's rejected shared-MCS-level changes is green
**before any new behavior is added**, so a later failure cannot be ambiguous between the rollback and
the fix.

_Requirements: 2.11, 2.12, 3.8_

## Run context

| Item | Value |
|---|---|
| Timestamp (UTC) | 2026-09-03T17:35:10Z |
| Working directory | `/Users/cars3788/Development/carstenpiepel/kubernetes-node-local-data` |
| Git HEAD | `1e915d4` |
| Helm | `v4.2.4+g3900f43` |
| Python | `3.14.7` (`.venv/bin/python`) |
| pytest | `8.3.5` |
| Chart version | `charts/node-local-data/Chart.yaml` line 5: `version: 0.2.0`; line 6: `appVersion: "0.5.0"` |

Renders were written to `/tmp/nld-render/{values-example,values-instance-types,my-values}.yaml` and
reused by the structural checks below.

## Result summary

| # | Command | Exit | Result |
|---|---|---|---|
| 1 | `helm lint --strict charts/node-local-data -f examples/values-example.yaml` | 0 | PASS |
| 2 | `helm lint --strict charts/node-local-data -f examples/values-instance-types.yaml` | 0 | PASS |
| 3 | `helm lint --strict charts/node-local-data -f my-values.yaml` | 0 | PASS |
| 4 | `helm template test charts/node-local-data -f examples/values-example.yaml` | 0 | PASS |
| 5 | `helm template test charts/node-local-data -f examples/values-instance-types.yaml` | 0 | PASS |
| 6 | `helm template test charts/node-local-data -f my-values.yaml` | 0 | PASS |
| 7 | `./scripts/check-placeholders.sh` | 0 | PASS |
| 8 | `.venv/bin/python -m pytest -q` | 0 | PASS — 60 passed |
| 9 | `.venv/bin/python -m pytest tests/chart -q` | 0 | PASS — 16 passed |
| 10 | `.venv/bin/python -m pytest agent/tests -q` | 0 | PASS — 44 passed |
| 11 | `git diff --check` | 0 | PASS — no output |
| 12 | `grep -ri selinux charts/ tests/ my-values.yaml` | 1 | PASS — no matches (exit 1 is the expected no-match status) |

**EXPECTED OUTCOME: all green. Met.**

One command in the tasks.md list is recorded separately because it fails for a pre-existing reason
unrelated to the rollback — see [Pre-existing condition](#pre-existing-condition-bare-helm-lint---strict).

## Command output

### 1–3. `helm lint --strict` with each values file

Identical output for all three values files:

```
==> Linting charts/node-local-data
[INFO] Chart.yaml: icon is recommended
1 chart(s) linted, 0 chart(s) failed
```

`[INFO] icon is recommended` is informational and pre-existing; it does not fail `--strict`.
No schema error mentions `selinux`, which is the signal that the schema `required` entry, the schema
property, and the `values.yaml` default were removed together (task 1.1) rather than partially.

### 4–6. `helm template`

| Values file | Exit | Lines | Documents |
|---|---|---|---|
| `examples/values-example.yaml` | 0 | 500 | 13 |
| `examples/values-instance-types.yaml` | 0 | 472 | 13 |
| `my-values.yaml` | 0 | 533 | 14 |

### 7. `./scripts/check-placeholders.sh`

```
No unreplaced placeholders in rendered manifests or NOTES.
```

The script runs `helm install placeholder-check --dry-run --debug` against
`examples/values-example.yaml`, so this also covers a NOTES render of that values file.

### 8–10. pytest

```
............................................................             [100%]
60 passed in 2.37s
---
................                                                         [100%]
16 passed in 1.07s        # tests/chart
---
............................................                             [100%]
44 passed in 1.10s        # agent/tests
```

60 = 16 chart + 44 agent, so the full run covers both suites and nothing else. Single runs, not watch
mode.

### 11. `git diff --check`

No output, exit 0. No whitespace errors or conflict markers in the tracked-file edits.

### 12. `grep -ri selinux charts/ tests/ my-values.yaml`

No matches, exit 1. Zero `selinux` references of any kind remain in the chart, the chart tests, or the
operator values file — not just `selinux.sharedLevel`. Scope is the task 1.1–1.4 file set; `README.md`
and `tuning/` are task 7's scope and are not asserted here.

## No `seLinuxOptions` in any render

`grep -rn "seLinuxOptions\|privileged" /tmp/nld-render/` returns only:

```
/tmp/nld-render/my-values.yaml:235:            privileged: false
/tmp/nld-render/values-instance-types.yaml:202:            privileged: false
/tmp/nld-render/values-example.yaml:202:            privileged: false
```

Zero `seLinuxOptions` hits. The `privileged` hits are the pre-existing explicit `privileged: false` on
the sync-agent container.

Structural confirmation by parsing each render and walking every pod template:

| Render | Workload | Pod-level `securityContext` | Container `securityContext` |
|---|---|---|---|
| all three | `DaemonSet/geodata-sync-agent` | ABSENT | `sync-agent`: `allowPrivilegeEscalation: false, privileged: false, runAsUser: 0` |
| all three | `DaemonSet/geodata-path-shim` | ABSENT | `ensure-path`: `runAsUser: 0`; `keep-path`: `allowPrivilegeEscalation: false, runAsUser: 0` |
| all three | `Deployment/geodata-warm-capacity` | ABSENT | `pause`: ABSENT |
| all three | `NodePool/geodata-nodes` | ABSENT | n/a |

Pod-level `securityContext` is absent everywhere — the key the rejected change added is gone, not merely
emptied. No container is privileged.

## NOTES render

`helm install --dry-run` NOTES captured for all three values files (44, 38, 38 lines).
`grep -rn -i "selinux\|admission mutation\|s0:c"` across all three returns no matches, exit 1. The
shared-level requirement block and the admission-mutation paragraph are gone from `NOTES.txt`.

## Rendered resource set otherwise unchanged

The untracked `rendered-install.yaml` (mtime 2026-08-31 21:29:59) predates the shared-level session
(chart edits are 2026-09-03 13:24–13:31), so it serves as a pre-change reference. It contains no
`selinux` reference, consistent with predating the rejected change.

Comparing it against the current `my-values.yaml` render:

- Resource kinds present in the artifact but missing now: **none**.
- Resource kinds present now but not in the artifact: `StorageClass/geodata-local-sc` and
  `PersistentVolume/geodata-local-pv`. This reflects `localVolume.enabled: true` in the current
  `my-values.yaml` (lines 17–19), not a rollback difference. Removing that pair is task 4.1's scope.
- Both DaemonSet pod specs: **key sets identical and differing-key list empty** — every pod-spec key
  compares equal, including `volumes`, `tolerations`, `nodeSelector`, and
  `terminationGracePeriodSeconds`.
- Pod-level `securityContext`: ABSENT in the artifact, ABSENT now.
- Every container `securityContext` compares identical between the artifact and the current render
  (`sync-agent`, `ensure-path`, `keep-path`).

The pod specs matching the pre-change artifact key-for-key is the direct evidence that the shared-level
change only ever added a pod-level `securityContext`, and that removing it left nothing else altered.

**Caveat on scope of this comparison.** `charts/` is untracked (constraint 2.12) and was edited in
place, so a byte-level before/after render diff of the rollback itself is not reproducible from git.
`rendered-install.yaml` also predates unrelated values edits — the `labelDomain`
(the operator's own domain → `data.example.com` in the examples), the FSx `volumeHandle`, and the
NFS share (`#geodata#` → `#gisdata#`) — so the whole-file diff is confounded and only the structural pod-spec
comparison above is load-bearing.

## Pre-existing condition: bare `helm lint --strict`

tasks.md lists `helm lint --strict charts/node-local-data` without a values file. It exits 1:

```
[ERROR] values.yaml: - at '/labelDomain': validation failed
  - at '/labelDomain': minLength: got 0, want 1
- at '/nfs': validation failed
  - at '/nfs/server': ...  '/nfs/shareName': ...  '/nfs/sharePath': ...  '/nfs/capacity': ...
- at '/nodePool/zones': minItems: got 0, want 1
- at '/aws': validation failed
  - at '/aws/amiAlias': ...  '/aws/nodeRole': ...  '/aws/securityGroupSelectorTerms': ...  '/aws/subnetSelectorTerms': ...
- at '/image/repository': minLength: got 0, want 1
Error: 1 chart(s) linted, 1 chart(s) failed
```

**Not a rollback failure.** Every error is an empty required placeholder that `values.yaml` ships
deliberately for the operator to fill: `labelDomain`, `nfs.*`, `nodePool.zones`, `aws.*`,
`image.repository`. The chart cannot lint without a values file by design, which is why the validation
set supplies `-f`. **No error mentions `selinux`** — a partial task 1.1 rollback would have surfaced
here as either a missing-required-`selinux` error or an unknown-property error, and neither appears.

## Conclusion

The rollback stands on its own. All twelve validation commands pass, no `seLinuxOptions` or pod-level
`securityContext` survives in any render, `Chart.yaml` is at `0.2.0`, and the rendered pod specs match
the pre-change artifact key-for-key. This is the known-good checkpoint for tasks 2 onward.

Nothing was fixed or modified in this task; it is verification only. No cluster or node state was
touched.
