import type {
  ChatRequestPayload,
  CompactRequestPayload,
  ConversationRunMode,
  ConversationRunPayload,
  ChatResumeEntry,
} from '../../../api/conversation/types'
import type { JsonObject, JsonValue } from '../../../types'

const ACTIVE_RUN_STORAGE_KEY = 'tinkerfin:active-conversation-run'

export interface ActiveRunSession {
  threadId: string
  payload: ConversationRunPayload
  mode: ConversationRunMode
  lastSeq: number
}

const isRecord = (value: unknown): value is Record<string, unknown> => (
  value != null && typeof value === 'object' && !Array.isArray(value)
)

const isJsonValue = (value: unknown): value is JsonValue => {
  if (
    value == null
    || typeof value === 'string'
    || typeof value === 'number'
    || typeof value === 'boolean'
  ) return true
  if (Array.isArray(value)) return value.every(isJsonValue)
  return isRecord(value) && Object.values(value).every(isJsonValue)
}

const isJsonObject = (value: unknown): value is JsonObject => (
  isRecord(value) && Object.values(value).every(isJsonValue)
)

const isResumeEntry = (value: unknown): value is ChatResumeEntry => {
  if (!isRecord(value)) return false
  if (typeof value.interruptId !== 'string') return false
  if (value.status !== 'resolved' && value.status !== 'cancelled') return false
  return value.payload === undefined || isJsonValue(value.payload)
}

const isChatRequestPayload = (value: unknown): value is ChatRequestPayload => {
  if (!isRecord(value)) return false
  if (typeof value.threadId !== 'string' || typeof value.runId !== 'string') return false
  if (!isJsonObject(value.state) || !isJsonObject(value.forwardedProps)) return false
  if (value.forwardedProps.accessMode !== "full" && value.forwardedProps.accessMode !== "write_approval") return false
  if (!Array.isArray(value.tools) || !value.tools.every(isJsonValue)) return false
  if (!Array.isArray(value.context) || !value.context.every(isJsonValue)) return false
  if (!Array.isArray(value.messages) || !value.messages.every((message) => (
    isRecord(message)
    && Object.keys(message).every((key) => key === 'id' || key === 'role' || key === 'content')
    && typeof message.id === 'string'
    && Boolean(message.id.trim())
    && message.role === 'user'
    && typeof message.content === 'string'
  ))) return false
  return value.resume === undefined
    || (Array.isArray(value.resume) && value.resume.every(isResumeEntry))
}

const parseActiveRunSession = (value: unknown): ActiveRunSession | null => {
  if (!isRecord(value)) return null
  const keys = Object.keys(value).sort()
  if (keys.join('\0') !== ['lastSeq', 'mode', 'payload', 'threadId'].join('\0')) return null
  if (typeof value.threadId !== 'string') return null
  if (value.mode !== 'start' && value.mode !== 'resume' && value.mode !== 'compact') return null
  if (!Number.isSafeInteger(value.lastSeq) || Number(value.lastSeq) < 0) return null
  let payload: ConversationRunPayload
  if (value.mode === 'compact') {
    if (!isCompactRequestPayload(value.payload)) return null
    payload = value.payload
  } else {
    if (!isChatRequestPayload(value.payload) || !value.payload.runId.trim()) return null
    payload = value.payload
  }
  return {
    threadId: value.threadId,
    payload,
    mode: value.mode,
    lastSeq: Number(value.lastSeq),
  }
}

const isCompactRequestPayload = (value: unknown): value is CompactRequestPayload => isRecord(value)
  && typeof value.threadId === 'string' && Boolean(value.threadId.trim())
  && typeof value.runId === 'string' && Boolean(value.runId.trim())
  && typeof value.model === 'string' && Boolean(value.model.trim())

export const readActiveRunSessions = (): ActiveRunSession[] => {
  try {
    const raw = window.sessionStorage.getItem(ACTIVE_RUN_STORAGE_KEY)
    if (!raw) return []
    const value: unknown = JSON.parse(raw)
    if (Array.isArray(value)) {
      const sessions = value.map(parseActiveRunSession)
      if (sessions.every((item): item is ActiveRunSession => item !== null)) return sessions
    }
    window.sessionStorage.removeItem(ACTIVE_RUN_STORAGE_KEY)
  } catch {
    // 缓存不可用不阻断页面；会话正文仍由服务端历史恢复
  }
  return []
}

export const readActiveRunSession = (threadId: string): ActiveRunSession | null => (
  readActiveRunSessions().find((session) => session.threadId === threadId) ?? null
)

const saveActiveRunSessions = (sessions: ActiveRunSession[]) => {
  try {
    if (sessions.length) window.sessionStorage.setItem(ACTIVE_RUN_STORAGE_KEY, JSON.stringify(sessions))
    else window.sessionStorage.removeItem(ACTIVE_RUN_STORAGE_KEY)
  } catch {
    // 浏览器禁用存储时，当前连接仍继续接收
  }
}

export const writeActiveRunSession = (session: ActiveRunSession): void => {
  saveActiveRunSessions([
    ...readActiveRunSessions().filter((item) => item.payload.runId !== session.payload.runId),
    session,
  ])
}

export const clearActiveRunSession = (runId?: string): void => {
  saveActiveRunSessions(runId ? readActiveRunSessions().filter((item) => item.payload.runId !== runId) : [])
}
