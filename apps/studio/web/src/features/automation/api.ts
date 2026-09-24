import { requestJson } from '../../api/shared/http'
import type { TraceMessage } from '../../api/conversation/history'
import type { Attachment } from '../conversation/attachments/content'
import { presentRun, type AutomationDraft, type AutomationRun, type AutomationRunRecord, type AutomationTask } from './model'

export interface Page<T> { items: T[]; nextCursor: string | null }
interface TaskResponse extends Omit<AutomationTask, 'startsOn' | 'endsOn'> { startsOn: string | null; endsOn: string | null }
export interface RunDetail extends AutomationRunRecord { messages: TraceMessage[]; outputFiles: Attachment[]; resultAvailable: boolean }
export interface BatchResult { taskId: string; succeeded: boolean; error: string | null }
const taskView = (task: TaskResponse): AutomationTask => ({ ...task, startsOn: task.startsOn ?? '', endsOn: task.endsOn ?? '' })
const path = (suffix: string, params: Record<string, string | undefined> = {}) => {
  const query = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) if (value) query.set(key, value)
  return `/api/automation/${suffix}?${query}`
}
export const fetchTaskPage = async (params: Record<string, string | undefined>, signal: AbortSignal): Promise<Page<AutomationTask>> => {
  const result = await requestJson<Page<TaskResponse>>(path('tasks', params), { signal, suppressGlobalError: true })
  return { ...result, items: result.items.map(taskView) }
}
export const fetchRunPage = async (params: Record<string, string | undefined>, signal: AbortSignal): Promise<Page<AutomationRun>> => {
  const result = await requestJson<Page<AutomationRunRecord>>(path('runs', params), { signal, suppressGlobalError: true })
  return { ...result, items: result.items.map(presentRun) }
}
export const fetchCounts = (kind: 'tasks' | 'runs', params: Record<string, string | undefined>, signal: AbortSignal) => requestJson<Record<string, number>>(path(`${kind}/counts`, params), { signal, suppressGlobalError: true })
export const saveTask = async (draft: AutomationDraft, requestId: string, task: AutomationTask | undefined, signal: AbortSignal) => {
  const { name, prompt, schedule, modelId, accessMode, attachments, startsOn, endsOn } = draft
  return taskView(await requestJson<TaskResponse>(path(task ? `tasks/${encodeURIComponent(task.id)}` : 'tasks'), {
    method: task ? 'PUT' : 'POST', signal, suppressGlobalError: true,
    body: { requestId, expectedRevision: task?.revision ?? null, configuration: { name, prompt, schedule, modelId, accessMode, attachments, startsOn: startsOn || null, endsOn: endsOn || null } },
  }))
}
export const commandTask = (task: AutomationTask, operation: 'pause' | 'enable' | 'run', requestId: string, signal: AbortSignal) => requestJson<TaskResponse | AutomationRunRecord>(path(`tasks/${encodeURIComponent(task.id)}/${operation}`), { method: 'POST', signal, suppressGlobalError: true, body: { requestId, expectedRevision: task.revision } })
export const batchTasks = (tasks: AutomationTask[], operation: 'pause' | 'delete', requestIds: Record<string, string>, signal: AbortSignal) => requestJson<BatchResult[]>(path('tasks/batch'), { method: 'POST', signal, suppressGlobalError: true, body: { operation, items: tasks.map(task => ({ taskId: task.id, expectedRevision: task.revision, requestId: requestIds[task.id] })) } })
export const fetchRunDetail = (id: string, signal: AbortSignal) => requestJson<RunDetail>(path(`runs/${encodeURIComponent(id)}`), { signal, suppressGlobalError: true })
