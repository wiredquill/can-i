{{- define "can-i.fullname" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* The client Deployment and its pod (<release>-client-...): the openshell CLI
     that creates the sandbox and forwards its UI. The sandbox pod, with the
     OpenShell supervisor, is named after the sandbox (sandbox.name). */}}
{{- define "can-i.clientName" -}}
{{- printf "%s-client" (include "can-i.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "can-i.labels" -}}
app.kubernetes.io/name: can-i
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{- define "can-i.selector" -}}
app.kubernetes.io/name: can-i
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/* The image the sandbox runs, from the form's dropdown. */}}
{{- define "can-i.sandboxImage" -}}
{{- if eq .Values.sandbox.image "custom" -}}
{{- required "sandbox.customImage is required when sandbox.image is custom" .Values.sandbox.customImage -}}
{{- else -}}
{{- required (printf "unknown sandbox.image %q (suse, opensuse, nvidia or custom)" .Values.sandbox.image) (get .Values.sandbox.images .Values.sandbox.image) -}}
{{- end -}}
{{- end -}}

{{/* Split a comma-separated form field into a list, dropping blanks. */}}
{{- define "can-i.csv" -}}
{{- $out := list -}}
{{- range splitList "," . -}}{{- if trim . -}}{{- $out = append $out (trim .) -}}{{- end -}}{{- end -}}
{{- toJson $out -}}
{{- end -}}
