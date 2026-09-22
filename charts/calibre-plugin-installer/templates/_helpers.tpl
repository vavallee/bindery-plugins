{{/*
Image reference. Prefers a digest when one is set, falls back to the tag.
*/}}
{{- define "calibre-plugin-installer.image" -}}
{{- if .digest -}}
{{ .repository }}@{{ .digest }}
{{- else -}}
{{ .repository }}:{{ .tag }}
{{- end -}}
{{- end -}}

{{/*
Basename of the release zip, derived from the templated release URL so the
downloaded file keeps the name the published .sha256 refers to.
*/}}
{{- define "calibre-plugin-installer.zipName" -}}
{{ base (tpl .Values.releaseUrlTemplate .) }}
{{- end -}}

{{/*
Guard against values that no longer do anything. pluginDestDir used to point at
Calibre's plugins directory, which calibre never scans, so silently ignoring it
would leave an operator believing the old no-op install still works.
*/}}
{{- define "calibre-plugin-installer.checkRemovedValues" -}}
{{- if .Values.pluginDestDir -}}
{{ fail "pluginDestDir was removed in chart 0.2.0. Calibre reads its plugin registry from customize.py.json, not from the plugins directory, so dropping a zip there installed nothing. Set calibreConfigDir to Calibre's config directory instead, typically /config/.config/calibre." }}
{{- end -}}
{{- end -}}
