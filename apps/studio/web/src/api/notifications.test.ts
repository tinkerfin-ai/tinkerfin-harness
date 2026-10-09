import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { clearAuthSession, saveAuthSession } from '../auth/session'
import { getServerAddress, setServerAddress } from './shared/config'
import { startNotificationFeed, subscribeResourceChanges, type ResourceNotice } from './notifications'

const closes: (() => void)[] = []
const streams: ReadableStreamDefaultController<Uint8Array>[] = []
const signals: AbortSignal[] = []
const encoder = new TextEncoder()
const frame = (index: number, event: string, data: unknown = {}) => streams[index].enqueue(encoder.encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`))
const settle = () => vi.advanceTimersByTimeAsync(0)
function signIn(token = 'one') {
  saveAuthSession({ serverAddress: getServerAddress(), token, tokenType: 'Bearer', expiresAt: '2100-01-01T00:00:00Z', user: {
    user_id: token === 'one' ? 1 : 2, username: token, roles: [], disabled: false, avatar_url: null,
  } })
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2030-01-01T00:00:00Z'))
  clearAuthSession()
  streams.length = 0
  signals.length = 0
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (_url, init) => {
    signals.push(init!.signal!)
    return new Response(new ReadableStream<Uint8Array>({ start(controller) { streams.push(controller) } }), { headers: { 'Content-Type': 'text/event-stream' } })
  })
  signIn()
})
afterEach(async () => {
  for (const close of closes.splice(0)) close()
  await settle()
  clearAuthSession()
  vi.restoreAllMocks()
  vi.useRealTimers()
})

it('同页使用者共享一条Bearer连接，ready和变化通过同一有界解析器交付', async () => {
  const notices: ResourceNotice[] = []
  closes.push(subscribeResourceChanges(notice => notices.push(notice)))
  const first = startNotificationFeed()
  const second = startNotificationFeed()
  closes.push(first, second)
  await settle()
  expect(fetch).toHaveBeenCalledTimes(1)
  const [url, options] = vi.mocked(fetch).mock.calls[0]
  expect(String(url)).toContain('/api/notifications')
  expect(new Headers(options?.headers).get('Authorization')).toBe('Bearer one')
  frame(0, 'ready')
  frame(0, 'change', { scope: { namespace: 'ns_1', owner_id: null }, topic: 'studio.conversation.changed', key: 'thread', details: {} })
  await settle()
  expect(notices.map(notice => notice.kind)).toEqual(['resync', 'change'])
  first()
  expect(signals[0].aborted).toBe(false)
  second()
  expect(signals[0].aborted).toBe(true)
})

it('断连重建基线，隐藏断开且不重试，重新可见只建立一条连接', async () => {
  const visible = vi.spyOn(document, 'hidden', 'get').mockReturnValue(false)
  const notices: ResourceNotice[] = []
  closes.push(subscribeResourceChanges(notice => notices.push(notice)), startNotificationFeed())
  await settle()
  frame(0, 'ready')
  streams[0].close()
  await settle()
  await vi.advanceTimersByTimeAsync(1000)
  expect(fetch).toHaveBeenCalledTimes(2)
  frame(1, 'ready')
  await settle()
  expect(notices).toEqual([{ kind: 'resync' }, { kind: 'resync' }])
  visible.mockReturnValue(true)
  document.dispatchEvent(new Event('visibilitychange'))
  expect(signals[1].aborted).toBe(true)
  await vi.advanceTimersByTimeAsync(60_000)
  expect(fetch).toHaveBeenCalledTimes(2)
  visible.mockReturnValue(false)
  document.dispatchEvent(new Event('visibilitychange'))
  await settle()
  expect(fetch).toHaveBeenCalledTimes(3)
})

it('账号和服务器变更取消旧连接，旧响应的内容不能通知当前页面', async () => {
  let resolve!: (response: Response) => void
  vi.mocked(fetch).mockImplementationOnce(() => new Promise(reply => { resolve = reply }))
  const notices: ResourceNotice[] = []
  closes.push(subscribeResourceChanges(notice => notices.push(notice)), startNotificationFeed())
  await settle()
  setServerAddress('http://localhost:4567')
  signIn('two')
  await settle()
  expect(fetch).toHaveBeenCalledTimes(2)
  const discarded = vi.fn()
  resolve(new Response(new ReadableStream({
    start(controller) { controller.enqueue(encoder.encode('event: ready\ndata: {}\n\n')) },
    cancel: discarded,
  }), { headers: { 'Content-Type': 'text/event-stream' } }))
  await settle()
  expect(discarded).toHaveBeenCalledOnce()
  expect(notices).toEqual([])
  frame(0, 'ready')
  await settle()
  expect(notices).toEqual([{ kind: 'resync' }])
})
