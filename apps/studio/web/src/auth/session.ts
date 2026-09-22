import { getServerAddress } from '../api/shared/config'
import type { AuthSessionResponse, AuthUser, LoginResponse } from '../api/auth/types'

export const AUTH_SESSION_STORAGE_KEY = 'tinkerfin.auth.session'

export interface AuthSession {
  serverAddress: string
  token: string
  tokenType: string
  expiresAt: string
  user: AuthUser
}

export interface AuthSessionLifecycleOptions {
  onExternalSession?: (session: AuthSession) => void
}

export interface AuthFailureEvent {
  code: number
  message: string
}

type SessionListener = (session: AuthSession | null) => void
type FailureListener = (event: AuthFailureEvent) => void

const sessionListeners = new Set<SessionListener>()
const failureListeners = new Set<FailureListener>()

let currentSession: AuthSession | null | undefined

/** 浏览器无法持久化登录会话 */
export class AuthSessionStorageError extends Error {
  constructor(cause?: unknown) {
    super('浏览器无法保存登录状态，请检查隐私或存储设置后重试', { cause })
    this.name = 'AuthSessionStorageError'
  }
}

function normalizeExpiresAt(value: string): string {
  const timestamp = Date.parse(value)
  if (!Number.isFinite(timestamp)) throw new TypeError('登录接口返回的固定到期时间无效')
  return new Date(timestamp).toISOString()
}

function sessionsEqual(first: AuthSession | null | undefined, second: AuthSession | null) {
  if (first == null || second == null) return first == null && second == null
  return first.serverAddress === second.serverAddress
    && first.token === second.token
    && first.tokenType === second.tokenType
    && first.expiresAt === second.expiresAt
    && first.user.user_id === second.user.user_id
    && first.user.username === second.user.username
    && first.user.display_name === second.user.display_name
    && first.user.avatar_url === second.user.avatar_url
    && first.user.disabled === second.user.disabled
    && first.user.roles.length === second.user.roles.length
    && first.user.roles.every((role, index) => role === second.user.roles[index])
}

function getLocalStorage(): Storage | null {
  if (typeof window === 'undefined') return null
  try {
    return window.localStorage
  } catch {
    return null
  }
}

function normalizeAuthUser(value: unknown): AuthUser | null {
  if (!value || typeof value !== 'object') return null
  if (Object.keys(value).sort().join('\0') !== [
    'avatar_url',
    'disabled',
    'display_name',
    'roles',
    'user_id',
    'username',
  ].join('\0')) return null
  const candidate = value as Partial<AuthUser>
  if (!(typeof candidate.user_id === 'number'
    && typeof candidate.username === 'string'
    && typeof candidate.display_name === 'string'
    && (candidate.avatar_url === null || typeof candidate.avatar_url === 'string')
    && Array.isArray(candidate.roles)
    && candidate.roles.every((role) => typeof role === 'string')
    && typeof candidate.disabled === 'boolean')) return null
  return {
    user_id: candidate.user_id,
    username: candidate.username,
    display_name: candidate.display_name,
    avatar_url: candidate.avatar_url,
    roles: candidate.roles,
    disabled: candidate.disabled,
  }
}

function normalizeAuthSession(value: unknown): AuthSession | null {
  if (!value || typeof value !== 'object') return null
  if (Object.keys(value).sort().join('\0') !== [
    'expiresAt',
    'serverAddress',
    'token',
    'tokenType',
    'user',
  ].join('\0')) return null
  const candidate = value as Partial<AuthSession>
  const user = normalizeAuthUser(candidate.user)
  if (!(typeof candidate.serverAddress === 'string'
    && candidate.serverAddress === getServerAddress()
    && typeof candidate.token === 'string'
    && candidate.token.length > 0
    && typeof candidate.tokenType === 'string'
    && typeof candidate.expiresAt === 'string'
    && Number.isFinite(Date.parse(candidate.expiresAt))
    && user != null)) return null
  return {
    serverAddress: candidate.serverAddress,
    token: candidate.token,
    tokenType: candidate.tokenType,
    expiresAt: candidate.expiresAt,
    user,
  }
}

export function isAuthSessionExpired(session: AuthSession, now = Date.now()): boolean {
  return now >= Date.parse(session.expiresAt)
}

function readStoredSession(): AuthSession | null {
  const storage = getLocalStorage()
  if (!storage) return null
  try {
    const raw = storage.getItem(AUTH_SESSION_STORAGE_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw) as unknown
    const normalized = normalizeAuthSession(parsed)
    if (!normalized || isAuthSessionExpired(normalized)) {
      storage.removeItem(AUTH_SESSION_STORAGE_KEY)
      return null
    }
    return {
      ...normalized,
      expiresAt: normalizeExpiresAt(normalized.expiresAt),
    }
  } catch {
    try {
      storage.removeItem(AUTH_SESSION_STORAGE_KEY)
    } catch {
      // 存储不可用时按未登录处理，不让页面初始化失败
    }
    return null
  }
}

function persistSession(session: AuthSession) {
  const storage = getLocalStorage()
  if (!storage) throw new AuthSessionStorageError()
  try {
    storage.setItem(AUTH_SESSION_STORAGE_KEY, JSON.stringify(session))
  } catch (error) {
    throw new AuthSessionStorageError(error)
  }
}

function notifySessionListeners(session: AuthSession | null) {
  for (const listener of sessionListeners) listener(session)
}

export function getAuthSession(): AuthSession | null {
  if (currentSession === undefined) currentSession = readStoredSession()
  if (currentSession) {
    try {
      if (currentSession.serverAddress !== getServerAddress() || isAuthSessionExpired(currentSession)) clearAuthSession()
    } catch {
      // 无法确认服务器归属时退出会话；请求层仍报告地址配置错误
      clearAuthSession()
    }
  }
  return currentSession
}

export function saveAuthSession(session: AuthSession) {
  if (session.serverAddress !== getServerAddress()) return
  const normalized = {
    ...session,
    expiresAt: normalizeExpiresAt(session.expiresAt),
  }
  if (isAuthSessionExpired(normalized)) {
    clearAuthSession()
    return
  }
  persistSession(normalized)
  currentSession = normalized
  notifySessionListeners(normalized)
}

export function clearAuthSession() {
  const storage = getLocalStorage()
  let hadStoredSession = false
  if (storage) {
    try {
      hadStoredSession = storage.getItem(AUTH_SESSION_STORAGE_KEY) != null
      storage.removeItem(AUTH_SESSION_STORAGE_KEY)
    } catch {
      // 当前页面仍必须立即退出，持久层清理失败不得保留内存 token
    }
  }
  const hadSession = currentSession != null || hadStoredSession
  currentSession = null
  if (hadSession) notifySessionListeners(null)
}

export function updateAuthSession(
  payload: AuthSessionResponse,
  expectedToken?: string,
): AuthSession | null {
  const session = getAuthSession()
  if (!session || (expectedToken != null && session.token !== expectedToken)) return null
  saveAuthSession({
    ...session,
    expiresAt: normalizeExpiresAt(payload.expires_at),
    user: payload.user,
  })
  return getAuthSession()
}

export function subscribeAuthSession(listener: SessionListener) {
  sessionListeners.add(listener)
  return () => {
    sessionListeners.delete(listener)
  }
}

export function subscribeAuthFailure(listener: FailureListener) {
  failureListeners.add(listener)
  return () => {
    failureListeners.delete(listener)
  }
}

export function notifyAuthFailure(event: AuthFailureEvent) {
  for (const listener of failureListeners) listener(event)
}

export function getAuthorizationHeader(): string | null {
  const session = getAuthSession()
  if (!session) return null
  return `${session.tokenType || 'Bearer'} ${session.token}`
}

export function createAuthSession(payload: LoginResponse): AuthSession {
  return {
    serverAddress: getServerAddress(),
    token: payload.access_token,
    tokenType: payload.token_type || 'Bearer',
    expiresAt: normalizeExpiresAt(payload.expires_at),
    user: payload.user,
  }
}

export function startAuthSessionLifecycle(
  options: AuthSessionLifecycleOptions = {},
): () => void {
  let expiryTimer: number | null = null

  const clearExpiryTimer = () => {
    if (expiryTimer == null) return
    window.clearTimeout(expiryTimer)
    expiryTimer = null
  }

  const scheduleExpiry = (session: AuthSession | null) => {
    clearExpiryTimer()
    if (!session) return
    const delay = Date.parse(session.expiresAt) - Date.now()
    if (delay <= 0) {
      clearAuthSession()
      return
    }
    expiryTimer = window.setTimeout(() => {
      expiryTimer = null
      const current = getAuthSession()
      if (current) scheduleExpiry(current)
    }, Math.min(delay, 2_147_483_647))
  }

  const recheckExpiry = () => scheduleExpiry(getAuthSession())
  const handleVisibilityChange = () => {
    if (document.visibilityState === 'visible') recheckExpiry()
  }
  const handleStorage = (event: StorageEvent) => {
    if (event.key !== AUTH_SESSION_STORAGE_KEY) return
    const previous = currentSession
    const next = readStoredSession()
    if (sessionsEqual(previous, next)) return
    currentSession = next
    notifySessionListeners(next)
    if (next) options.onExternalSession?.(next)
  }

  const unsubscribe = subscribeAuthSession(scheduleExpiry)
  window.addEventListener('focus', recheckExpiry)
  window.addEventListener('storage', handleStorage)
  document.addEventListener('visibilitychange', handleVisibilityChange)
  scheduleExpiry(getAuthSession())

  return () => {
    clearExpiryTimer()
    unsubscribe()
    window.removeEventListener('focus', recheckExpiry)
    window.removeEventListener('storage', handleStorage)
    document.removeEventListener('visibilitychange', handleVisibilityChange)
  }
}
