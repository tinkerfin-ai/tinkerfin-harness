import { getAuthorizationHeader, subscribeAuthSession } from '../../auth/session'
import { getResourceConnectionState, subscribeResourceChanges, type ResourceChange } from '../notifications'
import { getServerAddress, subscribeServerAddress } from './config'

interface ResourceWatch<T> {
  read: (signal: AbortSignal) => Promise<T>
  update: (value: T, signal: AbortSignal) => void
  matches: (change: ResourceChange) => boolean
  onError?: (error: unknown) => void
  minRefreshMs?: number
  refreshWhile?: (value: T) => boolean
}

/**
 * 先监听后读基线；读取期间再次失效会追加一轮读取，始终只有一个在途请求
 *
 * 空闲资源只响应变化与主动刷新；未完成结果可每30秒校准，隐藏或身份变化取消旧读取
 */
export function watchResource<T>(options: ResourceWatch<T>): { refresh: () => void; update(value: T): void; close: () => void } {
  let closed = false
  let dirty = true
  let pending: AbortController | null = null
  let latest: AbortController | null = null
  let timer: ReturnType<typeof setTimeout> | undefined
  let repair: ReturnType<typeof setTimeout> | undefined
  let nextReadAt = 0
  let identity = sessionIdentity()
  const current = (request: AbortController) => !closed && !request.signal.aborted
    && identity === sessionIdentity() && !document.hidden

  function schedule() {
    if (closed || pending || document.hidden || !dirty || timer !== undefined
      || getResourceConnectionState() === 'connecting') return
    const delay = Math.max(0, nextReadAt - Date.now())
    if (delay > 0) timer = setTimeout(() => { timer = undefined; schedule() }, delay)
    else void read()
  }
  function refresh() { dirty = true; schedule() }
  async function read() {
    dirty = false
    const request = new AbortController()
    pending = request
    latest = request
    nextReadAt = Date.now() + (options.minRefreshMs ?? 0)
    clearTimeout(repair)
    try {
      const value = await options.read(request.signal)
      publish(value, request)
    } catch (error) {
      if (current(request)) options.onError?.(error)
    } finally {
      if (pending === request) pending = null
      schedule()
    }
  }
  function publish(value: T, request: AbortController) {
    if (!current(request)) return
    clearTimeout(repair)
    options.update(value, request.signal)
    if (current(request) && options.refreshWhile?.(value)) repair = setTimeout(refresh, 30_000)
  }
  function reset() {
    latest?.abort()
    clearTimeout(timer)
    clearTimeout(repair)
    timer = undefined
    nextReadAt = 0
    identity = sessionIdentity()
    dirty = true
    schedule()
  }
  const releaseChanges = subscribeResourceChanges(notice => {
    if (notice.kind === 'connection') {
      if (notice.state === 'disconnected') schedule()
      return
    }
    if (notice.kind === 'resync' || options.matches(notice.change)) refresh()
  })
  const releaseAuth = subscribeAuthSession(() => { if (identity !== sessionIdentity()) reset() })
  const releaseServer = subscribeServerAddress(reset)
  document.addEventListener('visibilitychange', reset)
  schedule()
  return {
    refresh,
    // 追加页沿用当前基线，重新评估未完成结果；已开始的新读取拥有替换权
    update: value => { if (latest && !pending) publish(value, latest) },
    close: () => {
      closed = true
      latest?.abort()
      clearTimeout(timer)
      clearTimeout(repair)
      releaseChanges()
      releaseAuth()
      releaseServer()
      document.removeEventListener('visibilitychange', reset)
    },
  }
}

function sessionIdentity() {
  return JSON.stringify([getServerAddress(), getAuthorizationHeader()])
}
