import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { TraceGraphEvent, TraceGraphPage } from '../../../api/conversation/traceGraph'
import { clearAuthSession, saveAuthSession } from '../../../auth/session'
import { traceGraphNode, traceGraphWithNodes } from '../../../test/traceFixtures'
import { useChainTrace } from './useChainTrace'

const snapshot = (asOfSeq = 1): TraceGraphPage => ({
  ...traceGraphWithNodes([traceGraphNode({
    id: 'human-1',
    kind: 'human_message',
    name: 'HumanMessage',
    content: `结果 ${asOfSeq}`,
    updatedSeq: asOfSeq,
  })], asOfSeq),
  nextCursor: null,
})

const jsonResponse = (data: unknown) => Response.json({ code: 0, message: 'success', data })

function openStream() {
  let controller: ReadableStreamDefaultController<Uint8Array>
  let markClosed!: () => void
  const closed = new Promise<void>(resolve => { markClosed = resolve })
  const cancelled = vi.fn(() => markClosed())
  const response = new Response(new ReadableStream<Uint8Array>({
    start(value) { controller = value },
    cancel: cancelled,
  }), { headers: { 'Content-Type': 'text/event-stream' } })
  return {
    response,
    cancelled,
    closed,
    send: (event: TraceGraphEvent) => controller.enqueue(new TextEncoder().encode(
      `event: trace\ndata: ${JSON.stringify(event)}\n\n`,
    )),
  }
}

const requestFrom = (input: RequestInfo | URL, init?: RequestInit) => (
  input instanceof Request ? input : new Request(new URL(String(input), window.location.origin), init)
)

function fetchRequests() {
  type PendingRequest = { request: Request; respond: (response: Response) => void }
  const pending: PendingRequest[] = []
  let receive: ((request: PendingRequest) => void) | undefined
  const fetch = vi.fn((input: RequestInfo | URL, init?: RequestInit) => new Promise<Response>(respond => {
    const request = { request: requestFrom(input, init), respond }
    if (receive) {
      receive(request)
      receive = undefined
    } else pending.push(request)
  }))
  return {
    fetch,
    next: () => {
      const request = pending.shift()
      return request ? Promise.resolve(request) : new Promise<PendingRequest>(resolve => { receive = resolve })
    },
  }
}

describe('链路读取协议生命周期', () => {
  beforeEach(() => {
    saveAuthSession({
      token: 'chain-contract-token',
      serverAddress: 'http://127.0.0.1:8090', tokenType: 'Bearer',
      expiresAt: '2099-01-01T00:00:00.000Z',
      user: {
        user_id: 7,
        username: 'chain-contract',
        display_name: '链路契约',
        avatar_url: null,
        roles: [],
        disabled: false,
      },
    })
  })

  afterEach(() => {
    vi.useRealTimers()
    clearAuthSession()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('历史与部分历史只读取筛选快照，保持空闲且可重新激活', async () => {
    const page = { ...snapshot(), nextCursor: 'older-turns', completeness: {
      callTrackingMissing: true,
      relationshipEvidenceMissing: true,
      detailsOmitted: true,
    } }
    const requests = fetchRequests()
    vi.stubGlobal('fetch', requests.fetch)
    const { result, rerender } = renderHook(({ active }) => useChainTrace({
      threadId: 'thread-history', active, live: false,
      filter: { query: '结果' }, limit: 1000,
    }), { initialProps: { active: true }, reactStrictMode: true })

    const initial = await requests.next()
    await act(async () => initial.respond(jsonResponse(page)))
    expect(result.current.state).toEqual({ phase: 'ready', page })
    expect(requests.fetch).toHaveBeenCalledTimes(1)
    const url = new URL(initial.request.url)
    expect(url.pathname).toBe('/api/conversation/thread-history/trace/graph')
    expect(url.searchParams.get('query')).toBe('结果')
    expect(url.searchParams.get('limit')).toBe('1000')
    rerender({ active: false })
    expect(result.current.state.phase).toBe('idle')
    rerender({ active: true })
    const reactivated = await requests.next()
    await act(async () => reactivated.respond(jsonResponse(page)))
    expect(requests.fetch).toHaveBeenCalledTimes(2)
    expect(result.current.state.phase).toBe('ready')
  })

  it('运行中接收增量，结束时取消流并补齐最新权威观测后的最终快照', async () => {
    vi.useFakeTimers()
    const stream = openStream()
    const requests = fetchRequests()
    vi.stubGlobal('fetch', requests.fetch)
    const { result, rerender } = renderHook(({ live, observedAt }) => useChainTrace({
      threadId: 'thread-live', active: true, live, observedAt, filter: {}, limit: 1000,
    }), { initialProps: { live: true, observedAt: 'initial' } })
    const follow = await requests.next()
    await act(async () => follow.respond(stream.response))
    expect(requests.fetch).toHaveBeenCalledTimes(1)
    await act(async () => stream.send({ type: 'snapshot', snapshot: snapshot() }))
    expect(result.current.state.phase).toBe('ready')
    expect(follow.request.signal.aborted).toBe(false)
    await act(async () => stream.send({ type: 'update', update: {
      asOfSeq: 2, nextCursor: null, turnUpserts: [], turnRemoves: [],
      nodeUpserts: snapshot(2).nodes, nodeRemoves: [],
      orderedNodeIds: ['human-1'], matchedNodeIds: ['human-1'], completeness: snapshot().completeness,
    } }))
    await act(async () => vi.advanceTimersByTimeAsync(50))
    expect(result.current.state).toEqual({ phase: 'ready', page: snapshot(2) })
    rerender({ live: true, observedAt: 'another-live-observation' })
    expect(requests.fetch).toHaveBeenCalledTimes(1)

    rerender({ live: false, observedAt: 'another-live-observation' })
    const terminal = await requests.next()
    await act(async () => {
      terminal.respond(jsonResponse(snapshot(3)))
      await stream.closed
    })
    expect(result.current.state).toEqual({ phase: 'ready', page: snapshot(3) })
    expect(follow.request.signal.aborted).toBe(true)
    expect(stream.cancelled).toHaveBeenCalledTimes(1)
    rerender({ live: false, observedAt: 'final-authoritative-observation' })
    const final = await requests.next()
    await act(async () => final.respond(jsonResponse(snapshot(4))))
    expect(result.current.state).toEqual({ phase: 'ready', page: snapshot(4) })
    expect([follow, terminal, final].map(({ request }) => new URL(request.url).pathname)).toEqual([
      '/api/conversation/thread-live/trace/graph/follow',
      '/api/conversation/thread-live/trace/graph',
      '/api/conversation/thread-live/trace/graph',
    ])
  })

  it('恢复或新 Run 立即开启流，不依赖观测时间变化，并取消被隐藏的流', async () => {
    const stream = openStream()
    const nextStream = openStream()
    const requests = fetchRequests()
    vi.stubGlobal('fetch', requests.fetch)
    const { result, rerender, unmount } = renderHook(({ live }) => useChainTrace({
      threadId: 'thread-resumed', active: true, live, observedAt: 'same-observation', filter: {}, limit: 1000,
    }), { initialProps: { live: false } })
    const initial = await requests.next()
    await act(async () => initial.respond(jsonResponse(snapshot())))
    expect(result.current.state.phase).toBe('ready')
    rerender({ live: true })
    const follow = await requests.next()
    await act(async () => {
      follow.respond(stream.response)
      stream.send({ type: 'snapshot', snapshot: snapshot(2) })
    })
    expect(new URL(follow.request.url).pathname).toBe('/api/conversation/thread-resumed/trace/graph/follow')
    expect(result.current.state.phase).toBe('ready')

    const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden')
    act(() => document.dispatchEvent(new Event('visibilitychange')))
    await act(async () => { await stream.closed })
    expect(stream.cancelled).toHaveBeenCalledTimes(1)
    expect(result.current.state.phase).toBe('idle')
    visibility.mockReturnValue('visible')
    act(() => document.dispatchEvent(new Event('visibilitychange')))
    const resumed = await requests.next()
    await act(async () => {
      resumed.respond(nextStream.response)
      nextStream.send({ type: 'snapshot', snapshot: snapshot(3) })
    })
    expect(new URL(resumed.request.url).pathname).toBe('/api/conversation/thread-resumed/trace/graph/follow')
    expect(requests.fetch).toHaveBeenCalledTimes(3)
    expect(result.current.state).toEqual({ phase: 'ready', page: snapshot(3) })
    unmount()
    await act(async () => { await nextStream.closed })
    expect(nextStream.cancelled).toHaveBeenCalledTimes(1)
    expect(resumed.request.signal.aborted).toBe(true)
  })

  it('过滤与线程切换取消旧快照，迟到响应不能覆盖当前页面', async () => {
    const requests = fetchRequests()
    vi.stubGlobal('fetch', requests.fetch)
    const { result, rerender } = renderHook(({ threadId, query }) => useChainTrace({
      threadId, active: true, live: false, filter: { query }, limit: 1000,
    }), { initialProps: { threadId: 'thread-old', query: 'old' } })
    const stale = await requests.next()
    expect(requests.fetch).toHaveBeenCalledTimes(1)
    rerender({ threadId: 'thread-new', query: 'new' })
    const current = await requests.next()
    await act(async () => current.respond(jsonResponse(snapshot(2))))
    expect(result.current.state).toEqual({ phase: 'ready', page: snapshot(2) })
    expect(stale.request.signal.aborted).toBe(true)
    await act(async () => stale.respond(jsonResponse(snapshot(9))))
    expect(result.current.state).toEqual({ phase: 'ready', page: snapshot(2) })
    expect(new URL(current.request.url).searchParams.get('query')).toBe('new')
  })

  it('同一查询刷新期间保留结果，切换筛选重新加载，刷新失败允许重试', async () => {
    const requests = fetchRequests()
    vi.stubGlobal('fetch', requests.fetch)
    const { result, rerender } = renderHook(({ observedAt, query }) => useChainTrace({
      threadId: 'thread-refresh', active: true, live: false, observedAt, filter: { query }, limit: 1000,
    }), { initialProps: { observedAt: 'initial', query: '' } })
    const initial = await requests.next()
    expect(requests.fetch).toHaveBeenCalledTimes(1)
    expect(result.current.state.phase).toBe('loading')
    await act(async () => initial.respond(jsonResponse(snapshot())))
    expect(result.current.state).toEqual({ phase: 'ready', page: snapshot() })

    rerender({ observedAt: 'updated', query: '' })
    const updated = await requests.next()
    expect(requests.fetch).toHaveBeenCalledTimes(2)
    expect(result.current.state).toEqual({ phase: 'ready', page: snapshot() })
    await act(async () => updated.respond(jsonResponse(snapshot(2))))
    expect(result.current.state).toEqual({ phase: 'ready', page: snapshot(2) })

    rerender({ observedAt: 'updated', query: 'different' })
    const filtered = await requests.next()
    expect(requests.fetch).toHaveBeenCalledTimes(3)
    expect(result.current.state.phase).toBe('loading')
    await act(async () => filtered.respond(jsonResponse(snapshot(3))))
    expect(result.current.state.phase).toBe('ready')

    rerender({ observedAt: 'latest', query: 'different' })
    const failed = await requests.next()
    expect(requests.fetch).toHaveBeenCalledTimes(4)
    expect(result.current.state).toEqual({ phase: 'ready', page: snapshot(3) })
    await act(async () => failed.respond(jsonResponse({ invalid: true })))
    expect(result.current.state.phase).toBe('error')
    act(() => result.current.retry())
    const retried = await requests.next()
    expect(requests.fetch).toHaveBeenCalledTimes(5)
    expect(result.current.state.phase).toBe('loading')
    await act(async () => retried.respond(jsonResponse(snapshot(4))))
    expect(result.current.state).toEqual({ phase: 'ready', page: snapshot(4) })
  })

  it('快照失败显式报错，用户重试只创建新的快照请求', async () => {
    const requests = fetchRequests()
    vi.stubGlobal('fetch', requests.fetch)
    const { result } = renderHook(() => useChainTrace({
      threadId: 'thread-retry', active: true, live: false, filter: {}, limit: 1000,
    }))
    const initial = await requests.next()
    await act(async () => initial.respond(jsonResponse({ invalid: true })))
    expect(result.current.state.phase).toBe('error')
    expect(requests.fetch).toHaveBeenCalledTimes(1)
    act(() => result.current.retry())
    const retried = await requests.next()
    await act(async () => retried.respond(jsonResponse(snapshot())))
    expect(result.current.state).toEqual({ phase: 'ready', page: snapshot() })
    expect(requests.fetch).toHaveBeenCalledTimes(2)
  })

  it('等待会话重验时取消未完成的图，迟到快照不能覆盖随后查询', async () => {
    const requests = fetchRequests()
    vi.stubGlobal('fetch', requests.fetch)
    const { result, rerender } = renderHook(({ waitingForHistory, observedAt }) => useChainTrace({
      threadId: 'thread-waiting', active: true, live: false, waitingForHistory, observedAt,
      filter: {}, limit: 1000,
    }), { initialProps: { waitingForHistory: false, observedAt: 'initial' } })
    const old = await requests.next()
    rerender({ waitingForHistory: true, observedAt: 'initial' })
    expect(old.request.signal.aborted).toBe(true)
    expect(result.current.state.phase).toBe('loading')
    expect(requests.fetch).toHaveBeenCalledOnce()
    rerender({ waitingForHistory: false, observedAt: 'refreshed' })
    const current = await requests.next()
    await act(async () => current.respond(jsonResponse(snapshot(2))))
    await act(async () => old.respond(jsonResponse(snapshot(1))))
    expect(requests.fetch).toHaveBeenCalledTimes(2)
    expect(result.current.state).toEqual({ phase: 'ready', page: snapshot(2) })
  })
})
