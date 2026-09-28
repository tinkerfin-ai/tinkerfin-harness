import { requestJson } from '../../api/shared/http'
import type { ImportPreview, InstalledSkill, RemoteSkillDetail, RemoteSkillPage, SkillDetail, SkillSource, SkillChangeResult, SkillReplacement } from './model'

const base = '/api/skills'
export const readRunSkillSelection = (thread: string, run: string, signal?: AbortSignal) => requestJson<{ id: string; name: string }[]>(`${base}/selection?${new URLSearchParams({ thread_id: thread, run_id: run })}`, options(signal))
const options = (signal?: AbortSignal) => ({ signal, suppressGlobalError: true })
export const listSkillSources = (signal?: AbortSignal) => requestJson<SkillSource[]>(`${base}/sources`, options(signal))
export const listInstalledSkills = (signal?: AbortSignal) => requestJson<InstalledSkill[]>(`${base}/installations`, options(signal))
export function browseSkills(source: string, query: string, cursor: string | null, signal?: AbortSignal) {
  const params = new URLSearchParams({ source_id: source, q: query })
  if (cursor) params.set('cursor', cursor)
  return requestJson<RemoteSkillPage>(`${base}/catalog?${params}`, options(signal))
}
export function readRemoteSkill(source: string, id: string, revision: string | null, signal?: AbortSignal) {
  const params = new URLSearchParams({ source_id: source, skill_id: id })
  if (revision) params.set('revision', revision)
  return requestJson<RemoteSkillDetail>(`${base}/catalog/detail?${params}`, options(signal))
}
export const readInstalledSkill = (id: string, signal?: AbortSignal) => requestJson<SkillDetail>(`${base}/installations/${encodeURIComponent(id)}`, options(signal))
export const installSkill = (source: string, id: string, revision: string, requestId: string, signal?: AbortSignal) => requestJson<InstalledSkill>(`${base}/installations`, { ...options(signal), method: 'POST', body: { source_id: source, skill_id: id, revision, request_id: requestId } })
export const setSkillEnabled = (id: string, enabled: boolean, requestId: string, signal?: AbortSignal) => requestJson<InstalledSkill>(`${base}/installations/${encodeURIComponent(id)}`, { ...options(signal), method: 'PATCH', body: { enabled, request_id: requestId } })
export const uninstallSkill = (id: string, requestId: string, signal?: AbortSignal) => requestJson<null>(`${base}/installations/${encodeURIComponent(id)}`, { ...options(signal), method: 'DELETE', body: { request_id: requestId } })
export const updateSkill = (id: string, requestId: string, replacement?: SkillReplacement, signal?: AbortSignal) => requestJson<SkillChangeResult>(`${base}/installations/${encodeURIComponent(id)}/update`, { ...options(signal), method: 'POST', body: { request_id: requestId, replacement } })
export const previewGitHubSkills = (url: string, signal?: AbortSignal) => requestJson<ImportPreview>(`${base}/imports/github`, { ...options(signal), method: 'POST', body: { url } })
export const previewZipSkills = (file: File, signal?: AbortSignal) => requestJson<ImportPreview>(`${base}/imports/zip`, { ...options(signal), method: 'POST', body: file, headers: { 'Content-Type': 'application/zip' } })
export const confirmSkillImport = (id: string, digests: string[], requestId: string, signal?: AbortSignal) => requestJson<string[]>(`${base}/imports/${encodeURIComponent(id)}/confirm`, { ...options(signal), method: 'POST', body: { digests, request_id: requestId } })
