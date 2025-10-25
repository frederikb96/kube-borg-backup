{{/*
Expand the name of the chart.
*/}}
{{- define "kube-borg-backup.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this (by the DNS naming spec).
If release name contains chart name it will be used as a full name.
*/}}
{{- define "kube-borg-backup.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "kube-borg-backup.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Common labels
*/}}
{{- define "kube-borg-backup.labels" -}}
helm.sh/chart: {{ include "kube-borg-backup.chart" . }}
{{ include "kube-borg-backup.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/*
Selector labels
*/}}
{{- define "kube-borg-backup.selectorLabels" -}}
app.kubernetes.io/name: {{ include "kube-borg-backup.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
Service account name (hardcoded)
*/}}
{{- define "kube-borg-backup.serviceAccountName" -}}
kbb
{{- end -}}

{{/*
Generate resource name with kbb prefix and app name
Usage: {{ include "kube-borg-backup.resourceName" (dict "appName" .name "resource" "snapshot-cronjob") }}
*/}}
{{- define "kube-borg-backup.resourceName" -}}
{{- printf "kbb-%s-%s" .appName .resource | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Validate app name is DNS-safe (lowercase alphanumeric + hyphens only)
Usage: {{ include "kube-borg-backup.validateAppName" .name }}
*/}}
{{- define "kube-borg-backup.validateAppName" -}}
{{- if not (regexMatch "^[a-z0-9]([-a-z0-9]*[a-z0-9])?$" .) -}}
{{- fail (printf "App name '%s' is not DNS-safe. Must be lowercase alphanumeric with hyphens only (no leading/trailing hyphens)" .) -}}
{{- end -}}
{{- end -}}

{{/*
Get unique namespaces from apps list for RBAC deduplication
Returns: comma-separated list of unique namespaces
Usage: {{ include "kube-borg-backup.uniqueNamespaces" . }}
*/}}
{{- define "kube-borg-backup.uniqueNamespaces" -}}
{{- $namespaces := dict -}}
{{- range .Values.apps -}}
  {{- $_ := set $namespaces .namespace true -}}
{{- end -}}
{{- keys $namespaces | join "," -}}
{{- end -}}

{{/*
Merge snapshot defaults with per-app overrides
Usage: {{ include "kube-borg-backup.mergeSnapshotConfig" (dict "root" $ "app" .) }}
Returns: merged snapshot config with app overrides taking precedence
*/}}
{{- define "kube-borg-backup.mergeSnapshotConfig" -}}
{{- $defaults := .root.Values.snapshot -}}
{{- $appConfig := .app.snapshot | default dict -}}
{{- toJson (mergeOverwrite $defaults $appConfig) -}}
{{- end -}}

{{/*
Merge borgbackup defaults with per-app overrides
Usage: {{ include "kube-borg-backup.mergeBorgConfig" (dict "root" $ "app" .) }}
Returns: merged borgbackup config with app overrides taking precedence
*/}}
{{- define "kube-borg-backup.mergeBorgConfig" -}}
{{- $defaults := .root.Values.borgbackup -}}
{{- $appConfig := .app.borgbackup | default dict -}}
{{- toJson (mergeOverwrite $defaults $appConfig) -}}
{{- end -}}

{{/*
Resolve borg repository configuration from borgRepos by name
Usage: {{ include "kube-borg-backup.resolveBorgRepo" (dict "root" $ "repoName" "borgbase-main") }}
Returns: repository config dict or fails if not found
*/}}
{{- define "kube-borg-backup.resolveBorgRepo" -}}
{{- $found := dict -}}
{{- range $.root.Values.borgRepos -}}
  {{- if eq .name $.repoName -}}
    {{- $found = . -}}
  {{- end -}}
{{- end -}}
{{- if not $found.name -}}
{{- fail (printf "BorgRepo '%s' not found in .Values.borgRepos" $.repoName) -}}
{{- end -}}
{{- toJson $found -}}
{{- end -}}

{{/*
Validate required fields in app snapshot config
Usage: {{ include "kube-borg-backup.validateSnapshotConfig" (dict "appName" .name "config" $snapshotConfig) }}
*/}}
{{- define "kube-borg-backup.validateSnapshotConfig" -}}
{{- if not .config.pvcs -}}
{{- fail (printf "App '%s': snapshot.pvcs is REQUIRED but not specified" .appName) -}}
{{- end -}}
{{- end -}}

{{/*
Validate required fields in app borgbackup config
Usage: {{ include "kube-borg-backup.validateBorgConfig" (dict "appName" .name "config" $borgConfig) }}
*/}}
{{- define "kube-borg-backup.validateBorgConfig" -}}
{{- if not .config.cache -}}
{{- fail (printf "App '%s': borgbackup.cache is REQUIRED but not specified (must be unique per-app)" .appName) -}}
{{- end -}}
{{- if not .config.pvcs -}}
{{- fail (printf "App '%s': borgbackup.pvcs is REQUIRED but not specified" .appName) -}}
{{- end -}}
{{- if not .config.borgRepo -}}
{{- fail (printf "App '%s': borgbackup.borgRepo is REQUIRED but not specified" .appName) -}}
{{- end -}}
{{- end -}}
