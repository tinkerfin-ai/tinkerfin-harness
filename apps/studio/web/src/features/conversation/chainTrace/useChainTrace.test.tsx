import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type {
  TraceGraphDelta,
  TraceGraphEvent,
  TraceGraphQueryPage,
} from '../../../api/conversation/traceGraph'
import { useChainTrace } from './useChainTrace'

const followTraceGraph = vi.hoisted(() => vi.fn())
vi.mock('../../../api/conversation/traceGraph', async (importOriginal) => ({
  ...await importOriginal<typeof import('../../../api/conversation/traceGraph')>(),
  followTraceGraph,
}))

const page: TraceGraphQueryPage = {
  generation: 'generation-test',
  headRunId: 'run-fixture',
  turns: [{
    id: 'turn-1',
    ordinal: 1,
    startedAt: '2026-08-31T00:00:00Z',
  }],
  nodes: [{
    agui: null,
    id: 'human-1',
    turnId: 'turn-1',
    parentSubagentId: null,
    modelCallId: null,
    kind: 'human_message',
    status: 'succeeded',
    name: 'HumanMessage',
    runId: 'run-1',
    graphNamespace: [],
    startedAt: '2026-08-31T00:00:00Z',
    completedAt: '2026-08-31T00:00:00Z',
    startedSeq: 1,
    updatedSeq: 1,
    content: 'Find data',
    contentOmitted: false,
    toolCallOnly: false,
    requestOmitted: false,
    resultOmitted: false,
    linkIssues: [],
  }],
  orderedNodeIds: ['human-1'],
  matchedNodeIds: ['human-1'],
  nextCursor: null,
  asOfSeq: 1,
  completeness: {
    callTrackingMissing: false,
    relationshipEvidenceMissing: false,
    detailsOmitted: false,
  },
}

const stableIdentityChanges: Array<[string, Partial<TraceGraphDelta>]> = [
  [
    'Turn',
    {
      turnUpserts: [{
        ...page.turns[0]!,
        startedAt: '2026-09-01T00:00:00Z',
      }],
    },
  ],
  [
    'kind',
    {
      nodeUpserts: [{
        ...page.nodes[0]!,
        kind: 'assistant_message',
        updatedSeq: 2,
      }],
    },
  ],
  [
    'name',
    { nodeUpserts: [{ ...page.nodes[0]!, name: 'Changed', updatedSeq: 2 }] },
  ],
  [
    'graphNamespace',
    {
      nodeUpserts: [{
        ...page.nodes[0]!,
        graphNamespace: ['changed'],
        updatedSeq: 2,
      }],
    },
  ],
]

const waitForAbort = (signal?: AbortSignal) => new Promise<void>((resolve) => {
  if (signal?.aborted) resolve()
  else signal?.addEventListener('abort', () => resolve(), { once: true })
})

function graphFeed() {
  const pending: Array<{ event: TraceGraphEvent; consumed: () => void }> = []
  let wake: (() => void) | undefined
  let markClosed!: () => void
  const closed = new Promise<void>(resolve => { markClosed = resolve })
  return {
    closed,
    push(event: TraceGraphEvent) {
      const consumed = new Promise<void>(resolve => { pending.push({ event, consumed: resolve }) })
      wake?.()
      return consumed
    },
    async *read(signal: AbortSignal) {
      const onAbort = () => wake?.()
      signal.addEventListener('abort', onAbort)
      try {
        while (!signal.aborted) {
          const next = pending.shift()
          if (!next) {
            await new Promise<void>(resolve => { wake = resolve })
            continue
          }
          try { yield next.event } finally { next.consumed() }
        }
      } finally {
        signal.removeEventListener('abort', onAbort)
        pending.splice(0).forEach(item => item.consumed())
        markClosed()
      }
    },
  }
}

const metadataUpdate = (asOfSeq: number): TraceGraphDelta => ({
  asOfSeq, nextCursor: null, turnUpserts: [], turnRemoves: [], nodeUpserts: [], nodeRemoves: [],
  orderedNodeIds: page.orderedNodeIds, matchedNodeIds: page.matchedNodeIds, completeness: page.completeness,
})

describe('useChainTrace', () => {
  beforeEach(() => {
    followTraceGraph.mockReset()
  })
  afterEach(() => vi.useRealTimers())

  it('stays idle without opening a follower until the view is active', () => {
    const { result } = renderHook(() => useChainTrace({
      threadId: 'thread-1',
      active: false,
      live: false,
      filter: {},
      limit: 1000,
    }))

    expect(result.current.state.phase).toBe('idle')
    expect(followTraceGraph).not.toHaveBeenCalled()
  })

  it('starts one follower after StrictMode replay and closes it on unmount', async () => {
    const feed = graphFeed()
    const signals: AbortSignal[] = []
    followTraceGraph.mockImplementation((
      _threadId: string,
      _filter: unknown,
      options: { signal: AbortSignal },
    ) => {
      signals.push(options.signal)
      return feed.read(options.signal)
    })
    const { result, unmount } = renderHook(() => useChainTrace({
      threadId: 'thread-1',
      active: true,
      live: true,
      filter: { kinds: ['tool'] },
      limit: 1000,
    }), { reactStrictMode: true })

    await act(async () => { await feed.push({ type: 'snapshot', snapshot: page }) })
    expect(result.current.state.phase).toBe('ready')
    expect(followTraceGraph).toHaveBeenCalledTimes(1)
    expect(signals).toHaveLength(1)
    expect(signals[0].aborted).toBe(false)
    unmount()
    await act(async () => { await feed.closed })
    expect(signals[0].aborted).toBe(true)
  })

  it('keeps one follower when only local presentation state rerenders', async () => {
    const feed = graphFeed()
    followTraceGraph.mockImplementation((
      _threadId: string,
      _filter: unknown,
      options: { signal: AbortSignal },
    ) => feed.read(options.signal))
    const { result, rerender, unmount } = renderHook(
      ({ compact }: { compact: boolean }) => {
        void compact
        return useChainTrace({
          threadId: 'thread-1',
          active: true,
          live: true,
          filter: {},
          limit: 1000,
        })
      },
      { initialProps: { compact: false } },
    )

    await act(async () => { await feed.push({ type: 'snapshot', snapshot: page }) })
    expect(result.current.state.phase).toBe('ready')
    await act(async () => rerender({ compact: true }))
    expect(followTraceGraph).toHaveBeenCalledTimes(1)
    unmount()
    await feed.closed
  })

  it('applies node changes in the framework-provided order', async () => {
    vi.useFakeTimers()
    let releaseUpdate: (() => void) | undefined
    let markSnapshotConsumed!: () => void
    const snapshotConsumed = new Promise<void>(resolve => { markSnapshotConsumed = resolve })
    followTraceGraph.mockImplementation((
      _threadId: string,
      _filter: unknown,
      options: { signal?: AbortSignal } = {},
    ) => (
      async function* (): AsyncGenerator<TraceGraphEvent> {
        yield { type: 'snapshot', snapshot: page }
        await new Promise<void>((resolve) => { releaseUpdate = resolve; markSnapshotConsumed() })
        yield {
          type: 'update',
          update: {
            asOfSeq: 2,
            nextCursor: 'fresh-older-page',
            turnUpserts: [],
            turnRemoves: [],
            nodeUpserts: [{
              ...page.nodes[0],
              id: 'assistant-1',
              parentSubagentId: null,
              modelCallId: 'model-1',
              kind: 'assistant_message',
              name: 'AssistantMessage',
              startedSeq: 2,
              updatedSeq: 2,
            }],
            nodeRemoves: [],
            orderedNodeIds: ['human-1', 'assistant-1'],
            matchedNodeIds: ['human-1', 'assistant-1'],
            completeness: page.completeness,
          },
        }
        await waitForAbort(options.signal)
      }
    )())
    const { result, unmount } = renderHook(() => useChainTrace({
      threadId: 'thread-1',
      active: true,
      live: true,
      filter: {},
      limit: 1000,
    }))

    await act(async () => { await snapshotConsumed })
    expect(result.current.state.phase).toBe('ready')
    await act(async () => releaseUpdate?.())
    await act(async () => vi.advanceTimersByTimeAsync(50))
    expect(result.current.state.phase).toBe('ready')
    if (result.current.state.phase === 'ready') {
      expect(result.current.state.page.nodes.map((node) => node.id)).toEqual([
        'human-1',
        'assistant-1',
      ])
      expect(result.current.state.page.nextCursor).toBe('fresh-older-page')
    }
    unmount()
  })

  it('applies the latest completeness cursor when no matching node changes', async () => {
    vi.useFakeTimers()
    let markUpdateConsumed!: () => void
    const updateConsumed = new Promise<void>(resolve => { markUpdateConsumed = resolve })
    followTraceGraph.mockImplementation((
      _threadId: string,
      _filter: unknown,
      options: { signal?: AbortSignal } = {},
    ) => (
      async function* (): AsyncGenerator<TraceGraphEvent> {
        yield {
          type: 'snapshot',
          snapshot: { ...page, nextCursor: 'stale-older-page' },
        }
        yield {
          type: 'update',
          update: {
            asOfSeq: 2,
            nextCursor: 'fresh-older-page',
            turnUpserts: [],
            turnRemoves: [],
            nodeUpserts: [],
            nodeRemoves: [],
            orderedNodeIds: page.orderedNodeIds,
            matchedNodeIds: page.matchedNodeIds,
            completeness: { ...page.completeness, detailsOmitted: true },
          },
        }
        markUpdateConsumed()
        await waitForAbort(options.signal)
      }
    )())
    const { result, unmount } = renderHook(() => useChainTrace({
      threadId: 'thread-1',
      active: true,
      live: true,
      filter: { kinds: ['model'] },
      limit: 1000,
    }))

    await act(async () => { await updateConsumed })
    await act(async () => vi.advanceTimersByTimeAsync(49))
    expect(result.current.state.phase === 'ready' && result.current.state.page.asOfSeq).toBe(1)
    await act(async () => vi.advanceTimersByTimeAsync(1))
    expect(result.current.state.phase).toBe('ready')
    if (result.current.state.phase === 'ready') {
      expect(result.current.state.page.asOfSeq).toBe(2)
      expect(result.current.state.page.nextCursor).toBe('fresh-older-page')
      expect(result.current.state.page.completeness.detailsOmitted).toBe(true)
      expect(result.current.state.page.nodes).toBe(page.nodes)
      expect(result.current.state.page.turns).toBe(page.turns)
      expect(result.current.state.page.orderedNodeIds).toBe(page.orderedNodeIds)
      expect(result.current.state.page.matchedNodeIds).toBe(page.matchedNodeIds)
    }
    unmount()
  })

  it('逐条应用展示窗口内的节点修改，切换查询与卸载取消待发布页', async () => {
    vi.useFakeTimers()
    const feeds = new Map([['thread-1', graphFeed()], ['thread-2', graphFeed()]])
    const signals: AbortSignal[] = []
    followTraceGraph.mockImplementation((threadId: string, _filter: unknown, { signal }: { signal: AbortSignal }) => {
      signals.push(signal)
      return feeds.get(threadId)!.read(signal)
    })
    const { result, rerender, unmount } = renderHook(({ threadId }) => useChainTrace({
      threadId, active: true, live: true, filter: {}, limit: 1000,
    }), { initialProps: { threadId: 'thread-1' } })
    await act(async () => {
      await feeds.get('thread-1')!.push({ type: 'snapshot', snapshot: page })
      for (const seq of [2, 3]) {
        await feeds.get('thread-1')!.push({ type: 'update', update: {
          ...metadataUpdate(seq), nodeUpserts: [{ ...page.nodes[0]!, updatedSeq: seq, content: `内容${seq}` }],
        } })
      }
    })
    expect(result.current.state.phase === 'ready' && result.current.state.page.asOfSeq).toBe(1)
    await act(async () => vi.advanceTimersByTimeAsync(50))
    expect(result.current.state.phase === 'ready' && result.current.state.page.nodes[0]?.content).toBe('内容3')
    await act(async () => { await feeds.get('thread-1')!.push({ type: 'update', update: metadataUpdate(4) }) })
    rerender({ threadId: 'thread-2' })
    expect(signals[0]?.aborted).toBe(true)
    await act(async () => { await feeds.get('thread-2')!.push({ type: 'snapshot', snapshot: { ...page, asOfSeq: 10 } }) })
    await act(async () => vi.advanceTimersByTimeAsync(50))
    expect(result.current.state.phase === 'ready' && result.current.state.page.asOfSeq).toBe(10)
    await act(async () => { await feeds.get('thread-2')!.push({ type: 'update', update: metadataUpdate(11) }) })
    unmount()
    expect(signals[1]?.aborted).toBe(true)
    expect(vi.getTimerCount()).toBe(0)
  })

  it.each(['error', 'invalid_sequence'] as const)('待展示增量不能覆盖立即生效的错误：%s', async outcome => {
    vi.useFakeTimers()
    const feed = graphFeed()
    let signal!: AbortSignal
    followTraceGraph.mockImplementation((_threadId: string, _filter: unknown, options: { signal: AbortSignal }) => {
      signal = options.signal
      return feed.read(signal)
    })
    const { result, unmount } = renderHook(() => useChainTrace({
      threadId: 'thread-1', active: true, live: true, filter: {}, limit: 1000,
    }))
    await act(async () => {
      await feed.push({ type: 'snapshot', snapshot: page })
      await feed.push({ type: 'update', update: metadataUpdate(2) })
      await feed.push(outcome === 'error'
        ? { type: 'error', code: 'trace_unavailable' }
        : { type: 'update', update: metadataUpdate(2) })
    })
    expect(result.current.state.phase).toBe('error')
    expect(signal.aborted).toBe(true)
    expect(vi.getTimerCount()).toBe(0)
    await act(async () => vi.advanceTimersByTimeAsync(50))
    expect(result.current.state.phase).toBe('error')
    unmount()
  })

  it('rejects a delta whose authoritative order references missing state', async () => {
    const feed = graphFeed()
    let signal: AbortSignal | undefined
    followTraceGraph.mockImplementation((
      _threadId: string,
      _filter: unknown,
      options: { signal: AbortSignal },
    ) => {
      signal = options.signal
      return feed.read(options.signal)
    })
    const { result } = renderHook(() => useChainTrace({
      threadId: 'thread-1',
      active: true,
      live: true,
      filter: {},
      limit: 1000,
    }))
    await act(async () => {
      await feed.push({ type: 'snapshot', snapshot: page })
      await feed.push({ type: 'update', update: {
        ...metadataUpdate(2), orderedNodeIds: ['missing'], matchedNodeIds: ['missing'],
      } })
      await feed.closed
    })
    expect(result.current.state.phase).toBe('error')
    expect(signal?.aborted).toBe(true)
  })

  it('rejects a second snapshot from the same follower and closes it', async () => {
    const feed = graphFeed()
    let signal: AbortSignal | undefined
    followTraceGraph.mockImplementation((
      _threadId: string,
      _filter: unknown,
      options: { signal: AbortSignal },
    ) => {
      signal = options.signal
      return feed.read(options.signal)
    })
    const { result } = renderHook(() => useChainTrace({
      threadId: 'thread-1',
      active: true,
      live: true,
      filter: {},
      limit: 1000,
    }))

    await act(async () => {
      await feed.push({ type: 'snapshot', snapshot: page })
      await feed.push({ type: 'snapshot', snapshot: { ...page, asOfSeq: 2 } })
      await feed.closed
    })
    expect(result.current.state.phase).toBe('error')
    expect(signal?.aborted).toBe(true)
  })

  it.each(stableIdentityChanges)(
    'rejects a Delta that changes stable %s identity',
    async (_label, changes) => {
      const feed = graphFeed()
      followTraceGraph.mockImplementation((
        _threadId: string, _filter: unknown, { signal }: { signal: AbortSignal },
      ) => feed.read(signal))
      const { result } = renderHook(() => useChainTrace({
        threadId: 'thread-1',
        active: true,
        live: true,
        filter: {},
        limit: 1000,
      }))

      await act(async () => {
        await feed.push({ type: 'snapshot', snapshot: page })
        await feed.push({ type: 'update', update: { ...metadataUpdate(2), ...changes } })
        await feed.closed
      })
      expect(result.current.state.phase).toBe('error')
    },
  )

  it('rejects a node revision that moves backwards', async () => {
    const feed = graphFeed()
    const current = {
      ...page,
      asOfSeq: 2,
      nodes: [{ ...page.nodes[0]!, updatedSeq: 2 }],
    }
    followTraceGraph.mockImplementation((
      _threadId: string, _filter: unknown, { signal }: { signal: AbortSignal },
    ) => feed.read(signal))
    const { result } = renderHook(() => useChainTrace({
      threadId: 'thread-1',
      active: true,
      live: true,
      filter: {},
      limit: 1000,
    }))

    await act(async () => {
      await feed.push({ type: 'snapshot', snapshot: current })
      await feed.push({ type: 'update', update: {
        ...metadataUpdate(3), nodeUpserts: [{ ...page.nodes[0]!, updatedSeq: 1 }],
      } })
      await feed.closed
    })
    expect(result.current.state.phase).toBe('error')
  })

  it('replaces a follower and ignores its late snapshot', async () => {
    let releaseStale: (() => void) | undefined
    let markFirstConsumed!: () => void
    let markReplacementConsumed!: () => void
    let markStaleClosed!: () => void
    const firstConsumed = new Promise<void>(resolve => { markFirstConsumed = resolve })
    const replacementConsumed = new Promise<void>(resolve => { markReplacementConsumed = resolve })
    const staleClosed = new Promise<void>(resolve => { markStaleClosed = resolve })
    const signals: AbortSignal[] = []
    let calls = 0
    followTraceGraph.mockImplementation((
      _threadId: string,
      _filter: unknown,
      options: { signal?: AbortSignal } = {},
    ) => {
      calls += 1
      const call = calls
      if (options.signal) signals.push(options.signal)
      return (async function* (): AsyncGenerator<TraceGraphEvent> {
        if (call === 1) {
          yield { type: 'snapshot', snapshot: page }
          await new Promise<void>((resolve) => { releaseStale = resolve; markFirstConsumed() })
          try {
            yield { type: 'snapshot', snapshot: { ...page, asOfSeq: 3 } }
          } finally {
            markStaleClosed()
          }
          return
        }
        yield { type: 'snapshot', snapshot: { ...page, asOfSeq: 2 } }
        markReplacementConsumed()
        await waitForAbort(options.signal)
      })()
    })
    const { result, rerender, unmount } = renderHook(({
      kinds,
    }: { kinds: Array<'tool' | 'model'> }) => useChainTrace({
      threadId: 'thread-1',
      active: true,
      live: true,
      filter: { kinds },
      limit: 1000,
    }), { initialProps: { kinds: ['tool'] } })

    await act(async () => { await firstConsumed })
    expect(result.current.state.phase).toBe('ready')
    rerender({ kinds: ['model'] })
    await act(async () => { await replacementConsumed })
    expect(followTraceGraph).toHaveBeenCalledTimes(2)
    expect(signals[0].aborted).toBe(true)
    expect(result.current.state.phase).toBe('ready')
    if (result.current.state.phase === 'ready') {
      expect(result.current.state.page.asOfSeq).toBe(2)
    }
    await act(async () => {
      releaseStale?.()
      await staleClosed
    })
    if (result.current.state.phase === 'ready') {
      expect(result.current.state.page.asOfSeq).toBe(2)
    }
    unmount()
  })

  it.each(['event', 'throw', 'eof'] as const)(
    'enters the error phase after a terminal %s outcome',
    async (outcome) => {
      let markClosed!: () => void
      const closed = new Promise<void>(resolve => { markClosed = resolve })
      followTraceGraph.mockImplementation(() => (
        async function* (): AsyncGenerator<TraceGraphEvent> {
          try {
            yield { type: 'snapshot', snapshot: page }
            if (outcome === 'event') yield { type: 'error', code: 'trace_unavailable' }
            else if (outcome === 'throw') throw new Error('follow failed')
          } finally {
            markClosed()
          }
        }
      )())
      const { result } = renderHook(() => useChainTrace({
        threadId: 'thread-1',
        active: true,
        live: true,
        filter: {},
        limit: 1000,
      }))
      await act(async () => { await closed })
      expect(result.current.state.phase).toBe('error')
    },
  )
})
