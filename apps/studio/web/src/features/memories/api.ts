import { requestJson } from '../../api/shared/http'

export interface MemoryItem { path: string; etag: string; sizeBytes: number; updatedAt: string; editable: boolean; preview: string }
export interface MemoryDetail extends MemoryItem { content: string }
export interface MemoryPage { items: MemoryItem[]; nextOffset: number | null }
const base = (projectId: string) => `/api/projects/${encodeURIComponent(projectId)}/memories`
export const listMemories = (projectId: string, query: string, offset: number, signal: AbortSignal) => requestJson<MemoryPage>(`${base(projectId)}?${new URLSearchParams({ query, offset: String(offset) })}`, { signal, suppressGlobalError: true })
export const readMemory = (projectId: string, path: string, signal: AbortSignal) => requestJson<MemoryDetail>(`${base(projectId)}/file?${new URLSearchParams({ path })}`, { signal, suppressGlobalError: true })
export const saveMemory = (projectId: string, path: string, content: string, etag: string | null, signal: AbortSignal) => requestJson<MemoryDetail>(`${base(projectId)}${etag ? '/file' : ''}`, { method: etag ? 'PUT' : 'POST', body: { path, content, ...(etag ? { etag } : {}) }, signal, suppressGlobalError: true })
export const deleteMemory = (projectId: string, item: MemoryItem, signal: AbortSignal) => requestJson<null>(`${base(projectId)}/file?${new URLSearchParams({ path: item.path, etag: item.etag })}`, { method: 'DELETE', signal, suppressGlobalError: true })
