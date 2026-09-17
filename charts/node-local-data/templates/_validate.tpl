{{/* Cross-field validation invoked by rendered chart templates. */}}
{{- define "node-local-data.validate" -}}
{{- $datasetRoot := .Values.datasetRoot -}}
{{- $cleanDatasetRoot := clean $datasetRoot -}}
{{- if or (not (hasPrefix "/mnt/" $datasetRoot)) (not (hasPrefix "/mnt/" $cleanDatasetRoot)) -}}
{{- fail "datasetRoot must be a non-root path below /mnt/" -}}
{{- end -}}

{{/*
  datasetRoot must already be normalized, because it is a runtime contract and
  not only a validation input. It is rendered into both DaemonSet hostPaths and
  mountPaths, the agent --dest-root argument, and the Bottlerocket
  ephemeral-storage bind argument. Validating a cleaned copy while rendering the
  raw value would let one spelling become several different directories: a
  trailing space, for instance, survives a quoted scalar and is stripped from a
  plain one, so the agent would bind one directory and mirror into another while
  still writing a correct-looking generation label and clearing the readiness
  taint. Rejecting the denormalized spelling here keeps every surface identical.
  values.schema.json rejects whitespace, repeated separators, and a trailing
  separator; this check covers . and .. segments, which the schema pattern cannot
  express in Go's regexp dialect.
*/}}
{{- if ne $datasetRoot $cleanDatasetRoot -}}
{{- fail (printf "datasetRoot %q is not normalized. Use %q instead: the value is rendered verbatim into both DaemonSet hostPaths, the agent --dest-root argument, and the Bottlerocket ephemeral-storage bind argument, and a denormalized spelling can resolve to different directories on different surfaces." $datasetRoot $cleanDatasetRoot) -}}
{{- end -}}

{{/*
  Legacy-key rejection. The geocoding local PersistentVolume was removed in chart
  0.2.0, so an upgrade from 0.1.0 carrying the old key must fail with an
  explanation rather than with a bare unknown-property error from the schema.
  values.schema.json declares localVolume permissively for exactly this reason:
  schema validation runs before templates, so a strict schema would reject the key
  before this message could ever render.
*/}}
{{- if hasKey .Values "localVolume" -}}
{{- fail (printf "localVolume was removed in chart 0.2.0 and must be deleted from your values file. The geocoding local PersistentVolume it rendered is SELinux-relabeled on mount, so the first pod consuming it restamped the whole shared tree with that pod's MCS categories and every other consumer on the node was denied. Geocoding is now served from the same non-relabeling hostPath folder data store that routing already uses, registered at datasetRoot %q. Remove the localVolume block; there is no replacement setting." $cleanDatasetRoot) -}}
{{- end -}}

{{/*
  Forward guard: no relabeling mount may overlap a shared root.

  Inventory of every host-directory mount the chart renders, tagged with whether
  its volume plugin relabels the tree on mount. Relabeling requires BOTH halves
  of the seclabel rule: the plugin reports SELinux support AND the backing
  filesystem carries seclabel. The in-tree hostPath plugin fails the first half
  by design, so it never relabels; the local plugin on instance-store xfs
  satisfies both and does.

  Every entry below is a hostPath mount, so relabelsOnMount is false for all of
  them and the derived relabeling set is EMPTY today — the chart renders no
  relabeling volume at all since the geocoding local PV was removed. The guard
  is retained so that a future volume added to this inventory with
  relabelsOnMount true is checked against the shared roots on the day it is
  added, rather than shipping the same defect again.

  Limitation, by construction: this comparison is lexical. helm template cannot
  resolve symlinks, bind mounts, or hard links on a node, so two distinct paths
  that alias the same inodes pass this check and still break at runtime. Labels
  live on inodes, so distinct paths are necessary but the operator must also
  ensure distinct inodes. See tuning/gotchas.md.
*/}}
{{- $hostDirectoryMounts := list -}}
{{- $hostDirectoryMounts = append $hostDirectoryMounts (dict
  "owner" "sync-agent DaemonSet volume \"node-local-data\""
  "path" $cleanDatasetRoot
  "relabelsOnMount" false
) -}}
{{- if .Values.pathShim.enabled -}}
{{- $hostDirectoryMounts = append $hostDirectoryMounts (dict
  "owner" "path-shim DaemonSet volume \"dataset-root\""
  "path" $cleanDatasetRoot
  "relabelsOnMount" false
) -}}
{{- end -}}

{{/*
  Shared roots start with the ArcGIS routing folder-data-store registration
  path. That is not a chart resource, but it is datasetRoot, which the Path Shim
  guarantees on every node. Non-relabeling chart mounts join it; relabeling
  mounts are the ones checked against it.
*/}}
{{- $sharedRoots := list $cleanDatasetRoot -}}
{{- $relabelingMounts := list -}}
{{- range $hostDirectoryMounts -}}
{{- if .relabelsOnMount -}}
{{- $relabelingMounts = append $relabelingMounts . -}}
{{- else -}}
{{- $sharedRoots = append $sharedRoots (clean .path) -}}
{{- end -}}
{{- end -}}
{{- $sharedRoots = uniq $sharedRoots -}}

{{- range $mount := $relabelingMounts -}}
{{- range $sharedRoot := $sharedRoots -}}
{{- if eq (include "node-local-data.pathsOverlap" (list $mount.path $sharedRoot)) "true" -}}
{{- fail (printf "relabeling path %q (%s) overlaps the shared routing path %q. A volume that is SELinux-relabeled on mount restamps its whole tree with the consuming pod's MCS categories, while the routing hostPath registration does not. Sharing one tree between a relabeling and a non-relabeling mount makes it unreadable to every other consumer. Choose a path that neither equals, contains, nor is contained by the routing path, and keep it below /mnt/." (clean $mount.path) $mount.owner $sharedRoot) -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- if and .Values.warmCapacity.enabled (gt (int .Values.warmCapacity.replicas) (int .Values.nodePool.limits.nodes)) -}}
{{- fail "warmCapacity.replicas must not exceed nodePool.limits.nodes when warmCapacity.enabled is true" -}}
{{- end -}}

{{- $managedRequirementKeys := dict
  "kubernetes.io/arch" true
  "kubernetes.io/os" true
  "topology.kubernetes.io/zone" true
  "karpenter.sh/capacity-type" true
-}}
{{- range .Values.nodePool.instanceRequirements -}}
{{- if hasKey $managedRequirementKeys .key -}}
{{- fail (printf "nodePool.instanceRequirements must not redefine chart-managed key %s" .key) -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
  node-local-data.pathsOverlap — argument: (list <pathA> <pathB>).

  Emits the string "true" when the two paths are the same directory, or when
  either contains the other; emits the empty string otherwise. Callers compare
  the result with eq ... "true", since a template define can only return text.

  Normalization: both paths go through `clean` — duplicate separators collapsed,
  `.` and `..` resolved lexically, trailing separator stripped — so
  /mnt/geodata/geocoding/, /mnt/geodata//geocoding, and
  /mnt/geodata/routing/../geocoding are one path. A remaining trailing separator
  is trimmed as well, which only affects "/": `clean` leaves it as "/", and
  trimming it to "" makes the root correctly contain every absolute path instead
  of matching nothing. The design is silent on the root case; this is the
  segment-boundary-correct reading of it.

  Containment is tested in both directions with an explicit trailing separator
  appended to the candidate prefix, which is what keeps the comparison on
  segment boundaries: /mnt/geodata does NOT match the unrelated sibling
  /mnt/geodata-geocoding, while it does match the true child
  /mnt/geodata/geocoding.

  hasPrefix takes the prefix first, matching the datasetRoot check above.

  Lexical only: symlink, bind-mount, and hard-link aliasing is not detected, and
  cannot be from helm template. Two distinct paths over the same inodes pass.
*/}}
{{- define "node-local-data.pathsOverlap" -}}
{{- $a := clean (index . 0) | trimSuffix "/" -}}
{{- $b := clean (index . 1) | trimSuffix "/" -}}
{{- if or (eq $a $b) (hasPrefix (printf "%s/" $a) $b) (hasPrefix (printf "%s/" $b) $a) -}}
true
{{- end -}}
{{- end -}}
