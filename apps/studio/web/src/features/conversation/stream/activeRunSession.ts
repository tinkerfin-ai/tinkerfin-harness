import type {
  ChatRequestPayload,
  CompactRequestPayload,
  ConversationRunMode,
  ConversationRunPayload,
  ChatResumeEntry,
} from '../../../api/conversation/types'
import type { JsonObject, JsonValue } from '../../../types'
import { getAuthSession } from '../../../auth/session'

const ACTIVE_RUN_STORAGE_KEY = 'tinkerfin:active-conversation-run'
let ownerRevision = 0

/** 登录凭证只用于内存中的所有权核验，不写入运行恢复缓存 */
export interface ActiveRunOwner {
  serverAddress: string
  userId: number
  token: string
  revision: number
}

/** 捕获当前登录归属，全局清空后先前捕获的归属同样失效 */
export const captureActiveRunOwner = (): ActiveRunOwner | null => {
  const auth = getAuthSession()
  return auth ? {
    serverAddress: auth.serverAddress,
    userId: auth.user.user_id,
    token: auth.token,
    revision: ownerRevision,
  } : null
}

const sameOwner = (first: ActiveRunOwner | null, second: ActiveRunOwner | null) => (
  first === null || second === null ? first === second
    : first.serverAddress === second.serverAddress && first.userId === second.userId
      && first.token === second.token && first.revision === second.revision
)

export const isActiveRunOwnerCurrent = (owner: ActiveRunOwner | null): boolean => (
  owner !== null && sameOwner(owner, captureActiveRunOwner())
)

export interface ActiveRunSession {
  projectId: string
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
  if (typeof value.forwardedProps.projectId !== 'string' || !value.forwardedProps.projectId) return false
  if (value.forwardedProps.accessMode !== "full" && value.forwardedProps.accessMode !== "write_approval") return false
  if (!Array.isArray(value.tools) || !value.tools.every(isJsonValue)) return false
  if (!Array.isArray(value.context) || !value.context.every(isJsonValue)) return false
  if (!Array.isArray(value.messages) || !value.messages.every((message) => (
    isRecord(message)
    && Object.keys(message).every((key) => key === 'id' || key === 'role' || key === 'content')
    && typeof message.id === 'string'
    && Boolean(message.id.trim())
    && message.role === 'user'
    && (typeof message.content === 'string'
      || (Array.isArray(message.content) && message.content.every(isJsonObject)))
  ))) return false
  return value.resume === undefined
    || (Array.isArray(value.resume) && value.resume.every(isResumeEntry))
}

const parseActiveRunSession = (value: unknown): ActiveRunSession | null => {
  if (!isRecord(value)) return null
  const keys = Object.keys(value).sort()
  if (keys.join('\0') !== ['lastSeq', 'mode', 'payload', 'projectId', 'threadId'].join('\0')) return null
  if (typeof value.projectId !== 'string' || !value.projectId) return null
  if (typeof value.threadId !== 'string') return null
  if (value.mode !== 'start' && value.mode !== 'resume' && value.mode !== 'compact') return null
  if (!Number.isSafeInteger(value.lastSeq) || Number(value.lastSeq) < 0) return null
  let payload: ConversationRunPayload
  if (value.mode === 'compact') {
    if (!isCompactRequestPayload(value.payload)) return null
    payload = value.payload
  } else {
    if (!isChatRequestPayload(value.payload) || !value.payload.runId.trim()) return null
    if (value.payload.forwardedProps.projectId !== value.projectId) return null
    payload = value.payload
  }
  return {
    projectId: value.projectId,
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

export const readActiveRunSessions = (owner = captureActiveRunOwner()): ActiveRunSession[] => {
  const current = captureActiveRunOwner()
  if (!sameOwner(owner, current)) return []
  try {
    if (!owner) {
      window.sessionStorage.removeItem(ACTIVE_RUN_STORAGE_KEY)
      return []
    }
    const raw = window.sessionStorage.getItem(ACTIVE_RUN_STORAGE_KEY)
    if (!raw) return []
    const value: unknown = JSON.parse(raw)
    if (isRecord(value)
      && Object.keys(value).sort().join('\0') === ['runs', 'serverAddress', 'userId'].join('\0')
      && value.serverAddress === owner.serverAddress && value.userId === owner.userId
      && Array.isArray(value.runs)) {
      const sessions = value.runs.map(parseActiveRunSession)
      if (sessions.every((item): item is ActiveRunSession => item !== null)) return sessions
    }
    window.sessionStorage.removeItem(ACTIVE_RUN_STORAGE_KEY)
  } catch {
    // 缓存不可用不阻断页面；会话正文仍由服务端历史恢复
  }
  return []
}

export const readActiveRunSession = (threadId: string, projectId: string, owner = captureActiveRunOwner()): ActiveRunSession | null => (
  readActiveRunSessions(owner).find((session) => session.threadId === threadId && session.projectId === projectId) ?? null
)

const saveActiveRunSessions = (sessions: ActiveRunSession[], owner: ActiveRunOwner | null) => {
  try {
    if (sessions.length && owner) window.sessionStorage.setItem(ACTIVE_RUN_STORAGE_KEY, JSON.stringify({
      serverAddress: owner.serverAddress, userId: owner.userId, runs: sessions,
    }))
    else window.sessionStorage.removeItem(ACTIVE_RUN_STORAGE_KEY)
  } catch {
    // 浏览器禁用存储时，当前连接仍继续接收
  }
}

export const writeActiveRunSession = (session: ActiveRunSession, owner = captureActiveRunOwner()): void => {
  if (!isActiveRunOwnerCurrent(owner)) return
  saveActiveRunSessions([
    ...readActiveRunSessions(owner).filter((item) => item.payload.runId !== session.payload.runId),
    session,
  ], owner)
}

export const clearActiveRunSession = (runId?: string, owner = captureActiveRunOwner()): void => {
  if (!sameOwner(owner, captureActiveRunOwner())) return
  if (!runId) ownerRevision += 1
  saveActiveRunSessions(runId ? readActiveRunSessions(owner).filter((item) => item.payload.runId !== runId) : [], owner)
}
