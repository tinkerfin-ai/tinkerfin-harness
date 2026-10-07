import { ApiError, requestJson } from '../../api/shared/http'

export interface WorkspaceFile {
  path: string
  name: string
  kind: 'file' | 'directory' | 'symlink' | 'other'
  sizeBytes: number | null
  modifiedAt: string
  etag: string
}
export interface WorkspaceDirectory {
  state: 'ready' | 'uninitialized'
  path: string
  entries: WorkspaceFile[]
  nextCursor: string | null
}
export type WorkspacePreview = { kind: 'text'; file: WorkspaceFile; text: string; truncated: boolean }
  | { kind: 'unsupported'; file: WorkspaceFile }

export const WORKSPACE_ERRORS = {
  uninitialized: 1001011000, notFound: 1001011001, invalid: 1001011002,
  changed: 1001011003, paused: 1001011004, unavailable: 1001011005, forbidden: 1001011006,
} as const

export const workspaceEndpoint = (projectId: string) => `/api/projects/${encodeURIComponent(projectId)}/workspace`

function isFile(value: unknown): value is WorkspaceFile {
  if (!value || typeof value !== 'object') return false
  const item = value as Partial<WorkspaceFile>
  return typeof item.path === 'string' && item.path.startsWith('/') && typeof item.name === 'string'
    && ['file', 'directory', 'symlink', 'other'].includes(item.kind ?? '')
    && (item.sizeBytes === null || (typeof item.sizeBytes === 'number' && Number.isFinite(item.sizeBytes) && item.sizeBytes >= 0))
    && typeof item.modifiedAt === 'string' && Number.isFinite(Date.parse(item.modifiedAt))
    && typeof item.etag === 'string' && item.etag.length > 0
}

const invalid = () => new ApiError('接口返回格式不合法')

export async function readWorkspaceDirectory(projectId: string, path: string, cursor: string | null, signal: AbortSignal): Promise<WorkspaceDirectory> {
  const query = new URLSearchParams({ path })
  if (cursor) query.set('cursor', cursor)
  const value = await requestJson<unknown>(`${workspaceEndpoint(projectId)}/entries?${query}`, { signal, suppressGlobalError: true })
  if (!value || typeof value !== 'object') throw invalid()
  const page = value as Partial<WorkspaceDirectory>
  if (!['ready', 'uninitialized'].includes(page.state ?? '') || page.path !== path || !Array.isArray(page.entries)
    || page.entries.length > 200 || !page.entries.every(isFile) || (page.nextCursor !== null && typeof page.nextCursor !== 'string')) throw invalid()
  if (!page.entries.every(file => file.name !== '' && !['.', '..'].includes(file.name) && !file.name.includes('/')
    && file.path === `${path === '/' ? '' : path}/${file.name}`)) throw invalid()
  return page as WorkspaceDirectory
}

export async function readWorkspaceFileInfo(projectId: string, path: string, signal: AbortSignal): Promise<WorkspaceFile> {
  const value = await requestJson<unknown>(`${workspaceEndpoint(projectId)}/file?${new URLSearchParams({ path })}`, { signal, suppressGlobalError: true })
  if (!isFile(value) || value.path !== path) throw invalid()
  return value
}

export async function readWorkspacePreview(projectId: string, path: string, signal: AbortSignal): Promise<WorkspacePreview> {
  const value = await requestJson<unknown>(`${workspaceEndpoint(projectId)}/preview?${new URLSearchParams({ path })}`, { signal, suppressGlobalError: true })
  if (!value || typeof value !== 'object') throw invalid()
  const preview = value as Partial<WorkspacePreview>
  if (!isFile(preview.file) || preview.file.path !== path) throw invalid()
  if (preview.kind === 'unsupported') return { kind: 'unsupported', file: preview.file }
  if (preview.kind !== 'text' || typeof preview.text !== 'string' || preview.text.length > 102400 || typeof preview.truncated !== 'boolean') throw invalid()
  return { kind: 'text', file: preview.file, text: preview.text, truncated: preview.truncated }
}
