{{- define "llm-gateway.fullname" -}}
{{- if contains .Chart.Name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "llm-gateway.labels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{- define "llm-gateway.selector" -}}
app.kubernetes.io/name: {{ .root.Chart.Name }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "llm-gateway.image" -}}
{{ .Values.image.repository }}:{{ .Values.image.tag | default .Chart.AppVersion }}
{{- end -}}

{{- define "llm-gateway.secretName" -}}
{{- .Values.existingSecret | default (printf "%s-secrets" (include "llm-gateway.fullname" .)) -}}
{{- end -}}

{{- define "llm-gateway.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- .Values.serviceAccount.name | default (include "llm-gateway.fullname" .) -}}
{{- else -}}
{{- .Values.serviceAccount.name | default "default" -}}
{{- end -}}
{{- end -}}

{{/* The non-root, read-only container every workload of the chart runs as. */}}
{{- define "llm-gateway.podSecurityContext" -}}
runAsNonRoot: true
runAsUser: 10001
runAsGroup: 10001
fsGroup: 10001
seccompProfile:
  type: RuntimeDefault
{{- end -}}

{{- define "llm-gateway.containerSecurityContext" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities:
  drop: ["ALL"]
{{- end -}}

{{/* Settings shared by every container: config map, secret, policy file, extra env. */}}
{{- define "llm-gateway.env" -}}
envFrom:
  - configMapRef:
      name: {{ include "llm-gateway.fullname" . }}-config
  - secretRef:
      name: {{ include "llm-gateway.secretName" . }}
env:
  - name: PYTHONDONTWRITEBYTECODE
    value: "1"
  {{- if .Values.policy }}
  - name: GATEWAY_POLICY_FILE
    value: /etc/llm-gateway/policy.cedar
  {{- end }}
  {{- with .Values.extraEnv }}
  {{- toYaml . | nindent 2 }}
  {{- end }}
{{- end -}}

{{/* /tmp is the only writable path. The policy, when set, is mounted read-only. */}}
{{- define "llm-gateway.volumeMounts" -}}
volumeMounts:
  - name: tmp
    mountPath: /tmp
  {{- if .Values.policy }}
  - name: policy
    mountPath: /etc/llm-gateway
    readOnly: true
  {{- end }}
{{- end -}}

{{- define "llm-gateway.volumes" -}}
volumes:
  - name: tmp
    emptyDir:
      sizeLimit: 64Mi
  {{- if .Values.policy }}
  - name: policy
    configMap:
      name: {{ include "llm-gateway.fullname" . }}-policy
  {{- end }}
{{- end -}}
