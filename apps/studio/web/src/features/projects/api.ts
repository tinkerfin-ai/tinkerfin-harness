import { requestJson } from '../../api/shared/http'

export interface Project { id: string; name: string; createdAt: string; updatedAt: string }
export const listProjects = (signal: AbortSignal) => requestJson<Project[]>('/api/projects', { signal, suppressGlobalError: true })
export const saveProject = (name: string, projectId: string | null, signal: AbortSignal) => requestJson<Project>(projectId ? `/api/projects/${encodeURIComponent(projectId)}` : '/api/projects', {
  method: projectId ? 'PATCH' : 'POST', body: { name }, signal, suppressGlobalError: true,
})
