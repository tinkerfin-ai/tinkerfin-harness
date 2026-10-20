import { getAuthorizationHeader, subscribeAuthSession } from '../../auth/session'
import { getServerAddress, subscribeServerAddress } from './config'
import { requestEventStream } from './http'
import { parseJsonSseStream, SseError, type JsonSseFrame } from './sse'

export type ResourceFeedState = 'connecting' | 'ready' | 'disconnected' | 'hidden'

/** 页面拥有一个可重连通知源；身份、服务器或可见性变化会先取消旧连接 */
export function startResourceFeed({ path, onFrame, onState }: {
  path: string
  onFrame: (frame: JsonSseFrame) => void
  onState?: (state: ResourceFeedState, error?: unknown) => void
}): () => void {
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
    onState?.('connecting')
    let failure: unknown
    try {
      const response = await requestEventStream(path, { signal: request.signal, suppressGlobalError: true })
      if (!current()) { await response.body?.cancel(); return }
      if (!response.body) throw new SseError('stream_data_invalid')
      for await (const frame of parseJsonSseStream(response.body, request.signal)) {
        if (!current()) break
        if (frame.event === 'ready' || frame.event === 'resync') {
          retryMs = 1000
          onState?.('ready')
        }
        onFrame(frame)
      }
    } catch (error) {
      failure = error
    } finally {
      if (controller === request) controller = undefined
      if (current()) {
        onState?.('disconnected', failure)
        reconnect = setTimeout(() => { reconnect = undefined; void connect() }, retryMs)
        retryMs = Math.min(retryMs * 2, 30_000)
      }
    }
  }
  const reset = () => {
    stop()
    identity = currentIdentity()
    retryMs = 1000
    if (document.hidden) onState?.('hidden')
    void connect()
  }
  const releaseAuth = subscribeAuthSession(() => { if (identity !== currentIdentity()) reset() })
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
