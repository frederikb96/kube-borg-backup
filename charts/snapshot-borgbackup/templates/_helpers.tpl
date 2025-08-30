{{- define "snapshot-borgbackup.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "snapshot-borgbackup.fullname" -}}
{{- $name := include "snapshot-borgbackup.name" . -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s" $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "snapshot-borgbackup.labels" -}}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version | replace "+" "_" }}
app.kubernetes.io/name: {{ include "snapshot-borgbackup.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "snapshot-borgbackup.snapshotConfigSecret" -}}
{{ include "snapshot-borgbackup.fullname" . }}-snapshot-config
{{- end -}}

{{- define "snapshot-borgbackup.borgConfigSecret" -}}
{{ include "snapshot-borgbackup.fullname" . }}-borg-config
{{- end -}}
