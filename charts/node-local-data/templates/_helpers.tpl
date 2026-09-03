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

{{- define "node-local-data.datasetRoot" -}}
{{- .Values.datasetRoot -}}
{{- end -}}

{{- define "node-local-data.datasetGenerationJSONPathLabelKey" -}}
{{- include "node-local-data.datasetGenerationLabelKey" . | replace "." "\\." -}}
{{- end -}}

{{- define "node-local-data.datasetCountJSONPathLabelKey" -}}
{{- include "node-local-data.datasetCountLabelKey" . | replace "." "\\." -}}
{{- end -}}
