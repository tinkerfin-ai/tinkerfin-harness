import { useEffect, useRef, useState } from 'react'
import { requestJson } from '../../api/shared/http'
import type { JsonObject } from '../../types'

export interface ChatOptions {
  max_tokens: number | null
  temperature: number | null
  top_p: number | null
  stop: string[] | null
  reasoning_effort: 'low' | 'medium' | 'high' | null
  context_window: number | null
  keep_alive: number | null
}
export interface ModelSettings {
  model_id: string
  connection_id: string
  display_name: string
  purpose: 'chat' | 'image'
  model_name: string
  image_support: 'supported' | 'unsupported' | 'unknown'
  reasoning_enabled: boolean
  enabled: boolean
  is_default: boolean
  sort_order: number
  generation_options: JsonObject
  chat_options: ChatOptions
}
export interface ModelConnection {
  connection_id: string
  display_name: string
  provider_id: string
  api_type: 'openai_chat_completions' | 'ollama'
  base_url: string
  auth_type: 'api_key' | 'none'
  has_key: boolean
}
export interface ProviderPreset {
  provider_id: string
  display_name: string
  api_type: ModelConnection['api_type']
  base_url: string
  auth_type: ModelConnection['auth_type']
  models: string[]
}
export type ConnectionWrite = Omit<ModelConnection, 'has_key'> & { api_key: string | null }
export const newModel = (connectionId: string): ModelSettings => ({
  model_id: crypto.randomUUID(), connection_id: connectionId, display_name: '', purpose: 'chat', model_name: '',
  image_support: 'unknown', reasoning_enabled: false, enabled: true, is_default: false, sort_order: 0,
  generation_options: {}, chat_options: { max_tokens: null, temperature: null, top_p: null, stop: null, reasoning_effort: null, context_window: null, keep_alive: null },
})

/** 读取本人提供方和模型，串行提交配置并取消失效读取 */
export function useModelSettings(onChanged?: () => void) {
  const [models, setModels] = useState<ModelSettings[]>([])
  const [connections, setConnections] = useState<ModelConnection[]>([])
  const [presets, setPresets] = useState<ProviderPreset[]>([])
  const [loading, setLoading] = useState(true)
  const [loadFailed, setLoadFailed] = useState(false)
  const [revision, setRevision] = useState(0)
  const [saving, setSaving] = useState(false)
  const activeRequest = useRef<AbortController | null>(null)
  const loadRequest = useRef<AbortController | null>(null)
  useEffect(() => () => activeRequest.current?.abort(), [])
  useEffect(() => {
    const controller = new AbortController()
    loadRequest.current = controller
    setLoading(true); setLoadFailed(false)
    void requestJson<{ models: ModelSettings[]; connections: ModelConnection[]; providers: ProviderPreset[] }>('/api/models/settings', { signal: controller.signal })
      .then(value => {
        if (controller.signal.aborted) return
        setModels(value.models); setConnections(value.connections); setPresets(value.providers)
      }).catch(() => { if (!controller.signal.aborted) setLoadFailed(true) })
      .finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [revision])

  const write = async (path: string, method: 'PUT' | 'POST' | 'DELETE', body?: ModelSettings | ModelSettings[] | ConnectionWrite): Promise<boolean> => {
    if (activeRequest.current) return false
    const controller = new AbortController()
    activeRequest.current = controller; loadRequest.current?.abort(); setSaving(true)
    try {
      await requestJson<null>(path, { method, body, signal: controller.signal })
      if (controller.signal.aborted) return false
      onChanged?.()
      return true
    } catch {
      return false
    } finally {
      if (activeRequest.current === controller) activeRequest.current = null
      if (!controller.signal.aborted) { setSaving(false); setRevision(value => value + 1) }
    }
  }
  return {
    models, connections, presets, loading, loadFailed, saving,
    reload: () => setRevision(value => value + 1),
    addModels: (models: ModelSettings[]) => write('/api/models/configurations', 'POST', models),
    save: (model: ModelSettings) => write(`/api/models/configurations/${encodeURIComponent(model.model_id)}`, 'PUT', model),
    remove: (id: string) => write(`/api/models/configurations/${encodeURIComponent(id)}`, 'DELETE'),
    makeDefault: (id: string) => write(`/api/models/configurations/${encodeURIComponent(id)}/default`, 'PUT'),
    saveConnection: (connection: ConnectionWrite) => write(`/api/models/connections/${encodeURIComponent(connection.connection_id)}`, 'PUT', connection),
    removeConnection: (id: string) => write(`/api/models/connections/${encodeURIComponent(id)}`, 'DELETE'),
  }
}
