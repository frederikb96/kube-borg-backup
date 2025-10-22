{{- define "kube-borg-backup.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "kube-borg-backup.fullname" -}}
{{- $name := include "kube-borg-backup.name" . -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s" $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "kube-borg-backup.labels" -}}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version | replace "+" "_" }}
app.kubernetes.io/name: {{ include "kube-borg-backup.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "kube-borg-backup.snapshotConfigSecret" -}}
{{ include "kube-borg-backup.fullname" . }}-snapshot-config
{{- end -}}

{{- define "kube-borg-backup.borgConfigSecret" -}}
{{ include "kube-borg-backup.fullname" . }}-borg-config
{{- end -}}
