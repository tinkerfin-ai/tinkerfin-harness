import { startResourceFeed, type ResourceFeedState } from './shared/resourceFeed'
import { SseError } from './shared/sse'

export interface ResourceChange {
  scope: { namespace: string; owner_id: string | null }
  topic: string
  key: string
  details: Record<string, unknown>
}
export type ResourceNotice = { kind: 'resync' } | { kind: 'change'; change: ResourceChange }
  | { kind: 'connection'; state: ResourceFeedState | 'inactive' }
const listeners = new Set<(notice: ResourceNotice) => void>()
const feeds = new Map<string, { owners: number; readonly state: ResourceFeedState; close: () => void }>()

/** 已启动的通知源连接中时，资源先等待订阅生效再读取基线 */
export function getResourceConnectionState(): ResourceFeedState | 'inactive' {
  const states = [...feeds.values()].map(feed => feed.state)
  if (states.includes('connecting')) return 'connecting'
  if (states.includes('ready')) return 'ready'
  if (states.includes('hidden')) return 'hidden'
  return states.length ? 'disconnected' : 'inactive'
}

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
export function startNotificationFeed(projectId?: string): () => void {
  const key = projectId ?? ''
  const feed = feeds.get(key) ?? connectFeed(projectId)
  feeds.set(key, feed)
  feed.owners += 1
  let closed = false
  return () => {
    if (closed) return
    closed = true
    feed.owners -= 1
    if (feed.owners === 0) {
      feed.close(); feeds.delete(key)
      notify({ kind: 'connection', state: getResourceConnectionState() })
    }
  }
}

function connectFeed(projectId?: string) {
  let state: ResourceFeedState = 'connecting'
  const close = startResourceFeed({
    path: `/api/notifications${projectId ? `?${new URLSearchParams({ projectId })}` : ''}`,
    onState(value) {
      state = value
      // ready 与随后同一帧的 resync 合并通知，避免一帧触发两次基线
      if (value !== 'ready') notify({ kind: 'connection', state: value })
    },
    onFrame(frame) {
      if (frame.event === 'ready' || frame.event === 'resync') notify({ kind: 'resync' })
      else if (frame.event === 'change') {
        if (!isResourceChange(frame.data)) throw new SseError('stream_data_invalid')
        notify({ kind: 'change', change: frame.data })
      }
    },
  })
  return { owners: 0, get state() { return state }, close }
}
