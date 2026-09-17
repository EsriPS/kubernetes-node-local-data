{{/*
Shared naming, labels, selectors, paths, and storage identifiers.
Keep values exact where they form runtime contracts; resource names are bounded
to the DNS-label limit after appending their chart-owned suffix.
*/}}

{{- define "node-local-data.resourceName" -}}
{{- $root := index . 0 -}}
{{- $suffix := index . 1 -}}
{{- printf "%s-%s" $root.Values.namePrefix $suffix | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "node-local-data.namespace" -}}
{{- .Values.namespace.name -}}
{{- end -}}

{{- define "node-local-data.nodesName" -}}
{{- include "node-local-data.resourceName" (list . "nodes") -}}
{{- end -}}

{{- define "node-local-data.syncAgentName" -}}
{{- include "node-local-data.resourceName" (list . "sync-agent") -}}
{{- end -}}

{{- define "node-local-data.warmCapacityName" -}}
{{- include "node-local-data.resourceName" (list . "warm-capacity") -}}
{{- end -}}

{{- define "node-local-data.pathShimName" -}}
{{- include "node-local-data.resourceName" (list . "path-shim") -}}
{{- end -}}

{{- define "node-local-data.sourceStorageClassName" -}}
{{- include "node-local-data.resourceName" (list . "source-sc") -}}
{{- end -}}

{{- define "node-local-data.sourcePersistentVolumeName" -}}
{{- include "node-local-data.resourceName" (list . "source-pv") -}}
{{- end -}}

{{- define "node-local-data.sourcePersistentVolumeClaimName" -}}
{{- include "node-local-data.resourceName" (list . "source-pvc") -}}
{{- end -}}

{{- define "node-local-data.componentLabelKey" -}}
{{- printf "%s/component" .Values.labelDomain -}}
{{- end -}}

{{- define "node-local-data.workloadLabelKey" -}}
{{- printf "%s/workload" .Values.labelDomain -}}
{{- end -}}

{{- define "node-local-data.dedicatedTaintKey" -}}
{{- printf "%s/dedicated" .Values.labelDomain -}}
{{- end -}}

{{- define "node-local-data.dataNotReadyTaintKey" -}}
{{- printf "%s/data-not-ready" .Values.labelDomain -}}
{{- end -}}

{{/*
Applied by the sync agent, never by this chart, when datasetRoot exists but
cannot be read -- the signature of an SELinux relabel by a relabeling volume.
It is deliberately absent from the NodePool startupTaints: a new node has no
data yet, which is data-not-ready, not data-unreadable. The value the agent
writes is "replace-node", which is why the two DaemonSets tolerate this key with
operator Exists rather than matching that value. They must be able to reschedule
onto an affected node whatever the value says, because if the agent cannot land
there it stops reporting and the node goes quiet again.
*/}}
{{- define "node-local-data.dataUnreadableTaintKey" -}}
{{- printf "%s/data-unreadable" .Values.labelDomain -}}
{{- end -}}

{{- define "node-local-data.datasetGenerationLabelKey" -}}
{{- printf "%s/dataset-generation" .Values.labelDomain -}}
{{- end -}}

{{- define "node-local-data.datasetCountLabelKey" -}}
{{- printf "%s/dataset-count" .Values.labelDomain -}}
{{- end -}}

{{- define "node-local-data.commonLabels" -}}
{{ include "node-local-data.componentLabelKey" . }}: node-local-data-stores
{{- end -}}

{{/* Pass the shared resource name as the context. */}}
{{- define "node-local-data.selectorLabels" -}}
app: {{ . | quote }}
{{- end -}}

{{- define "node-local-data.workloadSelector" -}}
{{ include "node-local-data.workloadLabelKey" . }}: {{ .Values.workloadValue | quote }}
{{- end -}}

{{- define "node-local-data.sourceVolumeHandle" -}}
{{- printf "%s#%s#%s" .Values.nfs.server .Values.nfs.shareName (include "node-local-data.sourcePersistentVolumeName" .) -}}
{{- end -}}

{{/*
Every rendered surface goes through this helper, and it emits the lexically
cleaned path rather than the raw value. templates/_validate.tpl already rejects a
denormalized datasetRoot, so this is normally a no-op; it is written this way so
that the hostPaths, mountPaths, --dest-root, and the Bottlerocket
ephemeral-storage bind argument cannot diverge from each other even if a future
caller reaches a template without validation. Callers must quote it: a trailing
space is stripped from a plain YAML scalar and preserved in a quoted one, and
that difference alone splits one value into two directories.
*/}}
{{- define "node-local-data.datasetRoot" -}}
{{- clean .Values.datasetRoot -}}
{{- end -}}

{{- define "node-local-data.datasetGenerationJSONPathLabelKey" -}}
{{- include "node-local-data.datasetGenerationLabelKey" . | replace "." "\\." -}}
{{- end -}}

{{- define "node-local-data.datasetCountJSONPathLabelKey" -}}
{{- include "node-local-data.datasetCountLabelKey" . | replace "." "\\." -}}
{{- end -}}
