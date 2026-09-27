import { getAuthorizationHeader, subscribeAuthSession } from '../auth/session'
import { getServerAddress, subscribeServerAddress } from './shared/config'
import { requestEventStream } from './shared/http'
import { parseJsonSseStream, SseError } from './shared/sse'

export interface ResourceChange {
  scope: { namespace: string; owner_id: string | null }
  topic: string
  key: string
  details: Record<string, unknown>
}
export type ResourceNotice = { kind: 'resync' } | { kind: 'change'; change: ResourceChange }
const listeners = new Set<(notice: ResourceNotice) => void>()
let owners = 0
let stopFeed: (() => void) | undefined

function isResourceChange(value: unknown): value is ResourceChange {
  if (!value || typeof value !== 'object') return false
  const item = value as Partial<ResourceChange>
  return typeof item.topic === 'string' && item.topic.length > 0
    && typeof item.key === 'string' && item.key.length > 0
    && Boolean(item.scope && typeof item.scope.namespace === 'string'
      && (item.scope.owner_id === null || typeof item.scope.owner_id === 'string'))
    && Boolean(item.details && typeof item.details === 'object' && !Array.isArray(item.details))
}

/** 注册本页面的资源失效监听；正文始终通过已有鉴权查询读取 */
export function subscribeResourceChanges(listener: (notice: ResourceNotice) => void): () => void {
  listeners.add(listener)
  return () => { listeners.delete(listener) }
}

function notify(notice: ResourceNotice) {
  for (const listener of listeners) listener(notice)
}

/** 已登录工作区拥有通知连接；同页共享一条连接，最后一个使用者退出时关闭 */
export function startNotificationFeed(): () => void {
  owners += 1
  if (owners === 1) stopFeed = connectFeed()
  let closed = false
  return () => {
    if (closed) return
    closed = true
    owners -= 1
    if (owners === 0) { stopFeed?.(); stopFeed = undefined }
  }
}

function connectFeed(): () => void {
  let closed = false
  let generation = 0
  let controller: AbortController | undefined
  let reconnect: ReturnType<typeof setTimeout> | undefined
  let retryMs = 1000
  let identity = ''
  const currentIdentity = () => JSON.stringify([getServerAddress(), getAuthorizationHeader()])
  const stop = () => {
    generation += 1
    clearTimeout(reconnect)
    reconnect = undefined
    controller?.abort()
    controller = undefined
  }
  const connect = async () => {
    if (closed || document.hidden || !getAuthorizationHeader()) return
    const request = new AbortController()
    const requestGeneration = generation
    controller = request
    const current = () => !closed && !request.signal.aborted && generation === requestGeneration
      && identity === currentIdentity() && !document.hidden
    try {
      const response = await requestEventStream('/api/notifications', {
        signal: request.signal, suppressGlobalError: true,
      })
      if (!current()) { await response.body?.cancel(); return }
      if (!response.body) throw new SseError('stream_data_invalid')
      for await (const frame of parseJsonSseStream(response.body, request.signal)) {
        if (!current()) break
        if (frame.event === 'ready' || frame.event === 'resync') {
          retryMs = 1000
          notify({ kind: 'resync' })
        } else if (frame.event === 'change') {
          if (!isResourceChange(frame.data)) throw new SseError('stream_data_invalid')
          notify({ kind: 'change', change: frame.data })
        }
      }
    } catch {
      // 重连后的基线及可见资源校准负责补齐丢失提示，不让辅助通知打断操作
    } finally {
      if (controller === request) controller = undefined
      if (current()) {
        reconnect = setTimeout(() => { reconnect = undefined; void connect() }, retryMs)
        retryMs = Math.min(retryMs * 2, 30_000)
      }
    }
  }
  const reset = () => {
    stop()
    identity = currentIdentity()
    retryMs = 1000
    void connect()
  }
  const authChanged = () => { if (identity !== currentIdentity()) reset() }
  const releaseAuth = subscribeAuthSession(authChanged)
  const releaseServer = subscribeServerAddress(reset)
  document.addEventListener('visibilitychange', reset)
  reset()
  return () => {
    closed = true
    stop()
    releaseAuth()
    releaseServer()
    document.removeEventListener('visibilitychange', reset)
  }
}
