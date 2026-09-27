import { getAuthorizationHeader, subscribeAuthSession } from '../../auth/session'
import { subscribeResourceChanges, type ResourceChange } from '../notifications'
import { getServerAddress, subscribeServerAddress } from './config'

interface ResourceWatch<T> {
  read: (signal: AbortSignal) => Promise<T>
  update: (value: T, signal: AbortSignal) => void
  matches: (change: ResourceChange) => boolean
  onError?: (error: unknown) => void
  minRefreshMs?: number
  initialRead?: boolean
  repairWhen?: () => boolean
}

/**
 * 先监听后读基线；读取期间再次失效会追加一轮读取，始终只有一个在途请求
 *
 * 可见资源每30秒校准，隐藏时取消读取；账号、令牌和服务器变化使旧响应失效
 */
export function watchResource<T>(options: ResourceWatch<T>): { refresh: () => void; close: () => void } {
  let closed = false
  let dirty = options.initialRead !== false
  let pending: AbortController | null = null
  let latest: AbortController | null = null
  let timer: ReturnType<typeof setTimeout> | undefined
  let repair: ReturnType<typeof setTimeout> | undefined
  let nextReadAt = 0
  let identity = sessionIdentity()
  const current = (request: AbortController) => !closed && !request.signal.aborted
    && identity === sessionIdentity() && !document.hidden

  function schedule() {
    if (closed || pending || document.hidden || !dirty || timer !== undefined) return
    const delay = Math.max(0, nextReadAt - Date.now())
    if (delay > 0) timer = setTimeout(() => { timer = undefined; schedule() }, delay)
    else void read()
  }
  function refresh() { dirty = true; schedule() }
  function scheduleRepair() {
    clearTimeout(repair)
    if (!closed && !document.hidden) repair = setTimeout(() => {
      if (options.repairWhen?.() ?? true) refresh()
      else scheduleRepair()
    }, 30_000)
  }
  async function read() {
    dirty = false
    const request = new AbortController()
    pending = request
    latest = request
    nextReadAt = Date.now() + (options.minRefreshMs ?? 0)
    clearTimeout(repair)
    try {
      const value = await options.read(request.signal)
      if (current(request)) options.update(value, request.signal)
    } catch (error) {
      if (current(request)) options.onError?.(error)
    } finally {
      if (pending === request) pending = null
      scheduleRepair()
      schedule()
    }
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
    if (notice.kind === 'resync' || options.matches(notice.change)) refresh()
  })
  const releaseAuth = subscribeAuthSession(() => { if (identity !== sessionIdentity()) reset() })
  const releaseServer = subscribeServerAddress(reset)
  document.addEventListener('visibilitychange', reset)
  schedule()
  scheduleRepair()
  return {
    refresh,
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
