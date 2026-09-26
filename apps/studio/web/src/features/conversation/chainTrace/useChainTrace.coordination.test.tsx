import { act, cleanup, renderHook } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { TraceGraphQueryPage } from '../../../api/conversation/traceGraph'
import { ApiError } from '../../../api/shared/http'
import { emptyTraceGraph } from '../../../test/traceFixtures'
import type { HistoryActivationRefresh, HistoryRefreshResult } from '../trace/historyRefresh'
import { useChainTrace } from './useChainTrace'

const query = vi.hoisted(() => vi.fn())
vi.mock('../../../api/conversation/traceGraph', async (original) => ({
  ...await original<typeof import('../../../api/conversation/traceGraph')>(),
  queryTraceGraph: query,
}))

const page = (asOfSeq: number, headRunId = 'run-1', generation = 'generation-1'): TraceGraphQueryPage => ({
  ...emptyTraceGraph(asOfSeq), nextCursor: null, headRunId, generation,
})

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((accept, decline) => { resolve = accept; reject = decline })
  return { promise, resolve, reject }
}

function queuedGraph() {
  const requested = deferred<AbortSignal>()
  const response = deferred<TraceGraphQueryPage>()
  query.mockImplementationOnce((_threadId: string, _filter: unknown, options: { signal: AbortSignal }) => {
    requested.resolve(options.signal)
    return response.promise
  })
  return { requested, response }
}

describe('链路与会话观测协调', () => {
  beforeEach(() => query.mockReset())
  afterEach(cleanup)

  it.each(['history', 'graph'] as const)('%s 先到时各自完成读取，同身份较新图不被同序号状态刷新重查', async (first) => {
    const request = queuedGraph()
    const { result, rerender } = renderHook(({ pending, observation }) => useChainTrace({
      threadId: 'thread-1', active: true, live: false, filter: {}, limit: 1000, observation,
      historyRefresh: { epoch: 0, phase: pending ? 'pending' : 'ready' },
    }), { initialProps: { pending: true, observation: page(1) } })
    await act(async () => { await request.requested.promise })
    if (first === 'history') rerender({ pending: false, observation: page(2) })
    await act(async () => request.response.resolve(page(3)))
    expect(result.current.state).toEqual({ phase: 'ready', page: page(3) })
    if (first === 'graph') expect(result.current.historyStatus).toBe('pending')
    rerender({ pending: false, observation: page(2) })
    rerender({ pending: false, observation: { ...page(2) } })
    expect(result.current.historyStatus).toBe('ready')
    expect(query).toHaveBeenCalledOnce()
  })

  it('图落后最多校正一次，第二次仍落后明确失败且不会自行循环', async () => {
    const initial = queuedGraph()
    const correction = queuedGraph()
    const { result, rerender } = renderHook(({ sequence }) => useChainTrace({
      threadId: 'thread-1', active: true, live: false, filter: {}, limit: 1000,
      observation: page(sequence), historyRefresh: { epoch: 0, phase: 'ready' },
    }), { initialProps: { sequence: 5 } })
    await act(async () => { await initial.requested.promise })
    await act(async () => { initial.response.resolve(page(1)); await correction.requested.promise })
    await act(async () => correction.response.resolve(page(4)))
    expect(result.current.state.phase).toBe('error')
    rerender({ sequence: 6 })
    expect(query).toHaveBeenCalledTimes(2)
  })

  it.each([true, false])('head 冲突仅重验一次历史，并至多校正一次图，校正匹配=%s', async (matches) => {
    const initial = queuedGraph()
    const correction = queuedGraph()
    const recheckStarted = deferred<void>()
    const refreshed = deferred<HistoryRefreshResult>()
    const recheck = vi.fn(() => { recheckStarted.resolve(); return refreshed.promise })
    const { result, rerender } = renderHook(({ observation }) => useChainTrace({
      threadId: 'thread-1', active: true, live: false, filter: {}, limit: 1000,
      observation, historyRefresh: { epoch: 0, phase: 'ready' }, onRecheckHistory: recheck,
    }), { initialProps: { observation: page(2, 'run-2') } })
    await act(async () => { await initial.requested.promise })
    await act(async () => { initial.response.resolve(page(20)); await recheckStarted.promise })
    expect(result.current.state.phase).toBe('loading')
    await act(async () => {
      refreshed.resolve({ phase: 'ready', observation: page(3, 'run-2') })
      await correction.requested.promise
    })
    await act(async () => correction.response.resolve(page(3, matches ? 'run-2' : 'run-3')))
    expect(result.current.state.phase).toBe(matches ? 'ready' : 'error')
    rerender({ observation: page(3, 'run-2') })
    expect(query).toHaveBeenCalledTimes(2)
    expect(recheck).toHaveBeenCalledOnce()
  })

  it('历史重验与已读图对齐时不再补查图', async () => {
    const request = queuedGraph()
    const recheck = vi.fn(async (): Promise<HistoryRefreshResult> => ({ phase: 'ready', observation: page(3, 'run-2') }))
    const { result } = renderHook(() => useChainTrace({
      threadId: 'thread-1', active: true, live: false, filter: {}, limit: 1000,
      observation: page(1), historyRefresh: { epoch: 0, phase: 'ready' }, onRecheckHistory: recheck,
    }))
    await act(async () => { await request.requested.promise })
    await act(async () => request.response.resolve(page(3, 'run-2')))
    expect(result.current.state).toEqual({ phase: 'ready', page: page(3, 'run-2') })
    expect(query).toHaveBeenCalledOnce()
    expect(recheck).toHaveBeenCalledOnce()
  })

  it('不同 generation 不比较序号，不用更多查询掩盖身份冲突', async () => {
    const request = queuedGraph()
    const recheck = vi.fn()
    const { result } = renderHook(() => useChainTrace({
      threadId: 'thread-1', active: true, live: false, filter: {}, limit: 1000,
      observation: page(1), onRecheckHistory: recheck,
    }))
    await act(async () => { await request.requested.promise })
    await act(async () => request.response.resolve(page(100, 'run-1', 'another-generation')))
    expect(result.current.state.phase).toBe('error')
    expect(query).toHaveBeenCalledOnce()
    expect(recheck).not.toHaveBeenCalled()
  })

  it('已经对齐之后出现真实新 head，启动新运行的独立读取', async () => {
    const initial = queuedGraph()
    const nextRun = queuedGraph()
    const recheck = vi.fn()
    const { result, rerender } = renderHook(({ observation }) => useChainTrace({
      threadId: 'thread-1', active: true, live: false, filter: {}, limit: 1000,
      observation, onRecheckHistory: recheck,
    }), { initialProps: { observation: page(1) } })
    await act(async () => { await initial.requested.promise })
    await act(async () => initial.response.resolve(page(1)))
    rerender({ observation: page(2, 'run-2') })
    await act(async () => { await nextRun.requested.promise })
    await act(async () => nextRun.response.resolve(page(2, 'run-2')))
    expect(result.current.state).toEqual({ phase: 'ready', page: page(2, 'run-2') })
    expect(query).toHaveBeenCalledTimes(2)
    expect(recheck).not.toHaveBeenCalled()
  })

  it('普通历史失败保留图和失败提示，权限失败则清除图', async () => {
    const request = queuedGraph()
    const { result, rerender } = renderHook(({ phase }: { phase: HistoryActivationRefresh['phase'] }) => useChainTrace({
      threadId: 'thread-1', active: true, live: false, filter: {}, limit: 1000,
      observation: page(1), historyRefresh: { epoch: 0, phase },
    }), { initialProps: { phase: 'pending' } })
    await act(async () => { await request.requested.promise })
    await act(async () => request.response.resolve(page(1)))
    rerender({ phase: 'failed' })
    expect(result.current.state).toEqual({ phase: 'ready', page: page(1) })
    expect(result.current.historyStatus).toBe('failed')
    rerender({ phase: 'unavailable' })
    expect(result.current.state.phase).toBe('error')
    expect(result.current.historyStatus).toBe('unavailable')
    expect(query).toHaveBeenCalledOnce()
  })

  it.each([401, 403, 404])('图返回 %s 时清除已展示详情并终止当前请求', async (status) => {
    const initial = queuedGraph()
    const refresh = queuedGraph()
    const { result, rerender } = renderHook(({ epoch }) => useChainTrace({
      threadId: 'thread-1', active: true, live: false, filter: {}, limit: 1000,
      historyRefresh: { epoch, phase: 'ready' }, observation: page(1),
    }), { initialProps: { epoch: 0 } })
    await act(async () => { await initial.requested.promise })
    await act(async () => initial.response.resolve(page(1)))
    rerender({ epoch: 1 })
    const signal = await refresh.requested.promise
    await act(async () => refresh.response.reject(new ApiError('unavailable', { status })))
    expect(result.current.state.phase).toBe('error')
    expect(result.current.historyStatus).toBe('unavailable')
    expect(signal.aborted).toBe(true)
  })

  it('会话不可访问后的显式重试先重验历史，确认可访问后重新读取图', async () => {
    const refreshed = deferred<HistoryRefreshResult>()
    const recheckStarted = deferred<void>()
    const request = queuedGraph()
    const { result } = renderHook(() => {
      const [phase, setPhase] = useState<HistoryActivationRefresh['phase']>('unavailable')
      return useChainTrace({
        threadId: 'thread-1', active: true, live: false, filter: {}, limit: 1000,
        historyRefresh: { epoch: 0, phase }, observation: page(1),
        onRecheckHistory: async () => {
          setPhase('pending')
          recheckStarted.resolve()
          const response = await refreshed.promise
          setPhase(response.phase)
          return response
        },
      })
    })
    expect(result.current.state.phase).toBe('error')
    await act(async () => {
      result.current.retry()
      await recheckStarted.promise
    })
    expect(query).not.toHaveBeenCalled()
    expect(result.current.state.phase).toBe('error')
    await act(async () => refreshed.resolve({ phase: 'ready', observation: page(1) }))
    await act(async () => { await request.requested.promise; request.response.resolve(page(1)) })
    expect(result.current.state).toEqual({ phase: 'ready', page: page(1) })
    expect(query).toHaveBeenCalledOnce()
  })

  it('不可访问后的历史重验归会话持有，离开视图后不能重启图查询', async () => {
    const refreshed = deferred<HistoryRefreshResult>()
    const recheck = vi.fn(() => refreshed.promise)
    const { result, rerender } = renderHook(({ active }) => useChainTrace({
      threadId: 'thread-1', active, live: false, filter: {}, limit: 1000,
      historyRefresh: { epoch: 0, phase: 'unavailable' }, observation: page(1), onRecheckHistory: recheck,
    }), { initialProps: { active: true } })
    act(() => result.current.retry())
    expect(recheck).toHaveBeenCalledOnce()
    rerender({ active: false })
    await act(async () => refreshed.resolve({ phase: 'ready', observation: page(1) }))
    expect(query).not.toHaveBeenCalled()
    expect(result.current.state.phase).toBe('idle')
  })
})
