{{- define "hxlink.labels" -}}
app: hxlink
app.kubernetes.io/name: hxlink
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}
