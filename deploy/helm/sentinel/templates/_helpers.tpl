{{/*
密码：第一次安装时随机生成，之后升级用 lookup 读回已有的值（否则每次 helm upgrade 都换密码，已有连接全部失效）。
同一个密码要写进好几个命名空间的 Secret（例如 Postgres 的密码，agent 和执行器的连接串里都有），
所以生成一次后缓存在 .Values._pw 里，同一次渲染内多次调用拿到的是同一个值。
用法：include "sentinel.pw" (list $ "pg")
*/}}
{{- define "sentinel.pw" -}}
{{- $root := index . 0 -}}
{{- $key := index . 1 -}}
{{- if not (hasKey $root.Values "_pw") -}}
{{- $_ := set $root.Values "_pw" dict -}}
{{- end -}}
{{- if not (hasKey $root.Values._pw $key) -}}
{{- /* 规范来源：每个密码只有一个"主"Secret，从它读回 */ -}}
{{- $src := dict "pg" (list "sentinel-data" "postgres-auth" "password") "ro" (list "targets" "sentinel-ro-user" "password") "exec" (list "targets" "sentinel-exec-user" "password") "mem" (list "sentinel-data" "sentinel-mem-user" "password") -}}
{{- $loc := index $src $key -}}
{{- $existing := lookup "v1" "Secret" (index $loc 0) (index $loc 1) -}}
{{- $val := "" -}}
{{- if and $existing (hasKey $existing.data (index $loc 2)) -}}
{{- $val = index $existing.data (index $loc 2) | b64dec -}}
{{- else -}}
{{- /* 只用字母数字：密码会拼进连接串 URL，避免转义问题 */ -}}
{{- $val = randAlphaNum 32 -}}
{{- end -}}
{{- $_ := set $root.Values._pw $key $val -}}
{{- end -}}
{{- index $root.Values._pw $key -}}
{{- end -}}

{{- define "sentinel.image" -}}
{{ .Values.image.repository }}:{{ .Values.image.tag }}
{{- end -}}

{{- define "sentinel.storageClass" -}}
{{- if . }}
storageClassName: {{ . }}
{{- end }}
{{- end -}}
