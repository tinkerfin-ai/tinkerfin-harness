import type { JsonObject } from '../../types'
import { parseServiceOptions, type ImageOutputFormat } from './modelOptions'

export type ServiceCapability = 'web_search' | 'image_generation'
export type ServicePreset = 'tavily' | 'openai' | 'fal' | 'custom'
export interface HttpServiceRequest {
  method: 'GET' | 'POST'
  auth: 'bearer' | 'header' | 'none'
  header: string
  prefix: string
  parameters: JsonObject
  items_pointer: string
  value_pointer: string
  title_pointer: string
  url_pointer: string
  score_pointer: string
  response_type: 'url' | 'base64' | 'binary'
}
interface ServiceConfigurationBase {
  endpoint: string
  extra: JsonObject
  request: HttpServiceRequest | null
}
export type ServiceConfiguration = ServiceConfigurationBase & (
  { capability: 'web_search'; provider_id: 'tavily' | 'custom'; depth: 'basic' | 'advanced'; max_results: number }
  | { capability: 'image_generation'; provider_id: 'openai' | 'fal' | 'custom'; model: string; size: string; output_formats: ImageOutputFormat[] }
)
export interface ServiceSettings {
  id: string
  configuration: ServiceConfiguration
  enabled: boolean
  has_key: boolean
  test_status: 'success' | 'failed' | null
  test_code: string | null
  tested_at: string | null
}
export type SavedServices = Record<ServiceCapability, ServiceSettings | null>
export interface ServiceDraft {
  capability: ServiceCapability
  preset: ServicePreset
  endpoint: string
  enabled: boolean
  apiKey: string
  model: string
  size: string
  formats: ImageOutputFormat[]
  depth: 'basic' | 'advanced'
  maxResults: string
  method: HttpServiceRequest['method']
  auth: HttpServiceRequest['auth']
  header: string
  prefix: string
  body: string
  responseType: HttpServiceRequest['response_type']
  itemsPointer: string
  valuePointer: string
  titlePointer: string
  urlPointer: string
  scorePointer: string
  options: string
}

export const serviceLabels: Record<ServiceCapability, string> = { web_search: '网页搜索', image_generation: '图片生成' }
export const servicePresetLabels: Record<ServicePreset, string> = { tavily: 'Tavily', openai: 'OpenAI 兼容', fal: 'fal', custom: '自定义 HTTP' }
export const emptyServices = (): SavedServices => ({ web_search: null, image_generation: null })

export function newServiceDraft(capability: ServiceCapability): ServiceDraft {
  const search = capability === 'web_search'
  return {
    capability, preset: search ? 'tavily' : 'openai', endpoint: search ? 'https://api.tavily.com' : 'https://api.openai.com/v1',
    enabled: true, apiKey: '', model: '', size: '', formats: [], depth: 'basic', maxResults: '5',
    method: 'POST', auth: 'bearer', header: 'Authorization', prefix: 'Bearer ',
    body: JSON.stringify(search ? { query: '${query}', max_results: '${max_results}' } : { prompt: '${prompt}', num_images: 1 }, null, 2),
    responseType: 'url', itemsPointer: search ? '/results' : '/images', valuePointer: search ? '/content' : '/url', titlePointer: '/title', urlPointer: '/url', scorePointer: '', options: '{}',
  }
}

export function serviceDraft(saved: ServiceSettings | null, capability: ServiceCapability): ServiceDraft {
  const draft = newServiceDraft(capability)
  if (!saved) return draft
  const config = saved.configuration
  const request = config.request
  return {
    ...draft, preset: config.provider_id, endpoint: config.endpoint, enabled: saved.enabled,
    ...(config.capability === 'web_search' ? { depth: config.depth, maxResults: String(config.max_results) } : { model: config.model, size: config.size, formats: config.output_formats }),
    options: JSON.stringify(config.extra, null, 2),
    ...(request ? { method: request.method, auth: request.auth, header: request.header, prefix: request.prefix, body: JSON.stringify(request.parameters, null, 2), responseType: request.response_type, itemsPointer: request.items_pointer, valuePointer: request.value_pointer, titlePointer: request.title_pointer, urlPointer: request.url_pointer, scorePointer: request.score_pointer } : {}),
  }
}

export function serviceDirty(draft: ServiceDraft, saved: ServiceSettings | null): boolean {
  return JSON.stringify(draft) !== JSON.stringify(serviceDraft(saved, draft.capability))
}

export function serviceCredentialChanged(draft: ServiceDraft, saved: ServiceSettings | null): boolean {
  if (!saved) return false
  const original = serviceDraft(saved, draft.capability)
  return ['preset', 'endpoint', 'auth', 'header', 'prefix'].some(key => draft[key as keyof ServiceDraft] !== original[key as keyof ServiceDraft])
}

export function serviceReservedKeys(draft: ServiceDraft): string[] {
  return draft.preset === 'custom' ? ['api_key', 'authorization'] : draft.capability === 'web_search'
    ? ['query', 'max_results', 'search_depth', 'api_key', 'authorization']
    : ['model', 'prompt', 'n', 'num_images', 'api_key', 'authorization', 'output_formats']
}

export function serviceDraftErrors(draft: ServiceDraft, saved: ServiceSettings | null): Record<string, string> {
  const errors: Record<string, string> = {}
  try {
    const url = new URL(draft.endpoint)
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) throw new Error('url')
  } catch { errors.endpoint = '填写不含账号、查询参数的 HTTP 或 HTTPS 地址' }
  if ((draft.preset !== 'custom' || draft.auth !== 'none') && !draft.apiKey.trim() && (!saved?.has_key || serviceCredentialChanged(draft, saved))) errors.apiKey = '请填写此服务的 API Key'
  if (draft.preset === 'openai' && !draft.model.trim()) errors.model = '请填写服务商提供的 Model ID'
  if (draft.capability === 'web_search' && (!Number.isInteger(Number(draft.maxResults)) || Number(draft.maxResults) < 1 || Number(draft.maxResults) > 10)) errors.maxResults = '结果数量须为 1 到 10'
  const jsonField = draft.preset === 'custom' ? 'body' : 'options'
  if (!parseServiceOptions(draft[jsonField], serviceReservedKeys(draft)).value) errors[jsonField] = '参数须为有效 JSON 对象，不能覆盖认证或受控字段'
  if (draft.preset === 'custom') {
    if (draft.auth === 'header' && !/^[A-Za-z0-9-]+$/.test(draft.header)) errors.header = '请填写有效的认证头名称'
    const pointers = draft.capability === 'web_search' ? ['itemsPointer', 'titlePointer', 'urlPointer', 'valuePointer', 'scorePointer'] as const : draft.responseType === 'binary' ? [] : ['itemsPointer', 'valuePointer'] as const
    for (const key of pointers) if (draft[key] && (!draft[key].startsWith('/') || /~(?![01])/.test(draft[key]))) errors[key] = '结果路径须使用以 / 开始的 JSON Pointer'
    if (draft.capability === 'web_search') for (const key of ['itemsPointer', 'titlePointer', 'urlPointer'] as const) if (!draft[key]) errors[key] = '请填写结果路径'
    if (draft.capability === 'image_generation' && draft.responseType !== 'binary' && !draft.valuePointer) errors.valuePointer = '请填写结果路径'
  }
  return errors
}

export function serviceWrite(draft: ServiceDraft): { configuration: ServiceConfiguration; enabled: boolean; api_key: string | null } {
  const custom = draft.preset === 'custom'
  const request: HttpServiceRequest | null = custom ? {
    method: draft.method, auth: draft.auth, header: draft.header, prefix: draft.prefix,
    parameters: JSON.parse(draft.body) as JsonObject,
    items_pointer: draft.itemsPointer, value_pointer: draft.valuePointer,
    title_pointer: draft.titlePointer, url_pointer: draft.urlPointer, score_pointer: draft.scorePointer,
    response_type: draft.responseType,
  } : null
  const base = { endpoint: draft.endpoint.trim(), extra: custom ? {} : JSON.parse(draft.options) as JsonObject, request }
  let configuration: ServiceConfiguration
  if (draft.capability === 'web_search') {
    if (draft.preset !== 'tavily' && draft.preset !== 'custom') throw new Error('搜索接入方式不正确')
    configuration = { ...base, capability: 'web_search', provider_id: draft.preset, depth: draft.depth, max_results: Number(draft.maxResults) }
  } else {
    if (draft.preset === 'tavily') throw new Error('生图接入方式不正确')
    configuration = { ...base, capability: 'image_generation', provider_id: draft.preset, model: draft.model.trim(), size: draft.size.trim(), output_formats: draft.formats }
  }
  return { configuration, enabled: draft.enabled, api_key: draft.apiKey.trim() || null }
}
