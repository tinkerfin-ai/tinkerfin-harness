import { act, renderHook, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type {
  TraceGraphDelta,
  TraceGraphEvent,
  TraceGraphPage,
} from '../../../api/conversation/traceGraph'
import { useChainTrace } from './useChainTrace'

const followTraceGraph = vi.hoisted(() => vi.fn())
vi.mock('../../../api/conversation/traceGraph', async (importOriginal) => ({
  ...await importOriginal<typeof import('../../../api/conversation/traceGraph')>(),
  followTraceGraph,
}))

const page: TraceGraphPage = {
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

describe('useChainTrace', () => {
  beforeEach(() => {
    followTraceGraph.mockReset()
  })

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
    const signals: AbortSignal[] = []
    followTraceGraph.mockImplementation((
      _threadId: string,
      _filter: unknown,
      options: { signal?: AbortSignal } = {},
    ) => {
      if (options.signal) signals.push(options.signal)
      return (async function* (): AsyncGenerator<TraceGraphEvent> {
        yield { type: 'snapshot', snapshot: page }
        await waitForAbort(options.signal)
      })()
    })
    const { result, unmount } = renderHook(() => useChainTrace({
      threadId: 'thread-1',
      active: true,
      live: true,
      filter: { kinds: ['tool'] },
      limit: 1000,
    }), { reactStrictMode: true })

    await waitFor(() => expect(result.current.state.phase).toBe('ready'))
    expect(followTraceGraph).toHaveBeenCalledTimes(1)
    expect(signals).toHaveLength(1)
    expect(signals[0].aborted).toBe(false)
    unmount()
    await waitFor(() => expect(signals[0].aborted).toBe(true))
  })

  it('keeps one follower when only local presentation state rerenders', async () => {
    followTraceGraph.mockImplementation((
      _threadId: string,
      _filter: unknown,
      options: { signal?: AbortSignal } = {},
    ) => (
      async function* (): AsyncGenerator<TraceGraphEvent> {
        yield { type: 'snapshot', snapshot: page }
        await waitForAbort(options.signal)
      }
    )())
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

    await waitFor(() => expect(result.current.state.phase).toBe('ready'))
    rerender({ compact: true })
    await Promise.resolve()
    expect(followTraceGraph).toHaveBeenCalledTimes(1)
    unmount()
  })

  it('applies node changes in the framework-provided order', async () => {
    let releaseUpdate: (() => void) | undefined
    followTraceGraph.mockImplementation((
      _threadId: string,
      _filter: unknown,
      options: { signal?: AbortSignal } = {},
    ) => (
      async function* (): AsyncGenerator<TraceGraphEvent> {
        yield { type: 'snapshot', snapshot: page }
        await new Promise<void>((resolve) => { releaseUpdate = resolve })
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

    await waitFor(() => expect(result.current.state.phase).toBe('ready'))
    act(() => releaseUpdate?.())
    await waitFor(() => {
      expect(result.current.state.phase).toBe('ready')
      if (result.current.state.phase === 'ready') {
        expect(result.current.state.page.nodes.map((node) => node.id)).toEqual([
          'human-1',
          'assistant-1',
        ])
        expect(result.current.state.page.nextCursor).toBe('fresh-older-page')
      }
    })
    unmount()
  })

  it('applies the latest completeness cursor when no matching node changes', async () => {
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
      filter: { kinds: ['model'] },
      limit: 1000,
    }))

    await waitFor(() => {
      expect(result.current.state.phase).toBe('ready')
      if (result.current.state.phase === 'ready') {
        expect(result.current.state.page.asOfSeq).toBe(2)
        expect(result.current.state.page.nextCursor).toBe('fresh-older-page')
      }
    })
    unmount()
  })

  it('rejects a delta whose authoritative order references missing state', async () => {
    let signal: AbortSignal | undefined
    followTraceGraph.mockImplementation((
      _threadId: string,
      _filter: unknown,
      options: { signal?: AbortSignal } = {},
    ) => {
      signal = options.signal
      return (
      async function* (): AsyncGenerator<TraceGraphEvent> {
        yield { type: 'snapshot', snapshot: page }
        yield {
          type: 'update',
          update: {
            asOfSeq: 2,
            nextCursor: null,
            turnUpserts: [],
            turnRemoves: [],
            nodeUpserts: [],
            nodeRemoves: [],
            orderedNodeIds: ['missing'],
            matchedNodeIds: ['missing'],
            completeness: page.completeness,
          },
        }
      }
      )()
    })
    const { result } = renderHook(() => useChainTrace({
      threadId: 'thread-1',
      active: true,
      live: true,
      filter: {},
      limit: 1000,
    }))
    await waitFor(() => expect(result.current.state.phase).toBe('error'))
    expect(signal?.aborted).toBe(true)
  })

  it('rejects a second snapshot from the same follower and closes it', async () => {
    let signal: AbortSignal | undefined
    followTraceGraph.mockImplementation((
      _threadId: string,
      _filter: unknown,
      options: { signal?: AbortSignal } = {},
    ) => {
      signal = options.signal
      return (async function* (): AsyncGenerator<TraceGraphEvent> {
        yield { type: 'snapshot', snapshot: page }
        yield { type: 'snapshot', snapshot: { ...page, asOfSeq: 2 } }
      })()
    })
    const { result } = renderHook(() => useChainTrace({
      threadId: 'thread-1',
      active: true,
      live: true,
      filter: {},
      limit: 1000,
    }))

    await waitFor(() => expect(result.current.state.phase).toBe('error'))
    expect(signal?.aborted).toBe(true)
  })

  it.each(stableIdentityChanges)(
    'rejects a Delta that changes stable %s identity',
    async (_label, changes) => {
      followTraceGraph.mockImplementation(() => (
        async function* (): AsyncGenerator<TraceGraphEvent> {
          yield { type: 'snapshot', snapshot: page }
          yield {
            type: 'update',
            update: {
              asOfSeq: 2,
              nextCursor: null,
              turnUpserts: [],
              turnRemoves: [],
              nodeUpserts: [],
              nodeRemoves: [],
              orderedNodeIds: page.orderedNodeIds,
              matchedNodeIds: page.matchedNodeIds,
              completeness: page.completeness,
              ...changes,
            },
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

      await waitFor(() => expect(result.current.state.phase).toBe('error'))
    },
  )

  it('rejects a node revision that moves backwards', async () => {
    const current = {
      ...page,
      asOfSeq: 2,
      nodes: [{ ...page.nodes[0]!, updatedSeq: 2 }],
    }
    followTraceGraph.mockImplementation(() => (
      async function* (): AsyncGenerator<TraceGraphEvent> {
        yield { type: 'snapshot', snapshot: current }
        yield {
          type: 'update',
          update: {
            asOfSeq: 3,
            nextCursor: null,
            turnUpserts: [],
            turnRemoves: [],
            nodeUpserts: [{ ...page.nodes[0]!, updatedSeq: 1 }],
            nodeRemoves: [],
            orderedNodeIds: page.orderedNodeIds,
            matchedNodeIds: page.matchedNodeIds,
            completeness: page.completeness,
          },
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

    await waitFor(() => expect(result.current.state.phase).toBe('error'))
  })

  it('replaces a follower and ignores its late snapshot', async () => {
    let releaseStale: (() => void) | undefined
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
          await new Promise<void>((resolve) => { releaseStale = resolve })
          yield { type: 'snapshot', snapshot: { ...page, asOfSeq: 3 } }
          return
        }
        yield { type: 'snapshot', snapshot: { ...page, asOfSeq: 2 } }
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

    await waitFor(() => expect(result.current.state.phase).toBe('ready'))
    rerender({ kinds: ['model'] })
    await waitFor(() => {
      expect(followTraceGraph).toHaveBeenCalledTimes(2)
      expect(signals[0].aborted).toBe(true)
      expect(result.current.state.phase).toBe('ready')
      if (result.current.state.phase === 'ready') {
        expect(result.current.state.page.asOfSeq).toBe(2)
      }
    })
    await act(async () => {
      releaseStale?.()
      await Promise.resolve()
    })
    if (result.current.state.phase === 'ready') {
      expect(result.current.state.page.asOfSeq).toBe(2)
    }
    unmount()
  })

  it.each(['event', 'throw', 'eof'] as const)(
    'enters the error phase after a terminal %s outcome',
    async (outcome) => {
      followTraceGraph.mockImplementation(() => (
        async function* (): AsyncGenerator<TraceGraphEvent> {
          yield { type: 'snapshot', snapshot: page }
          if (outcome === 'event') yield { type: 'error', code: 'trace_unavailable' }
          else if (outcome === 'throw') throw new Error('follow failed')
        }
      )())
      const { result } = renderHook(() => useChainTrace({
        threadId: 'thread-1',
        active: true,
        live: true,
        filter: {},
        limit: 1000,
      }))
      await waitFor(() => expect(result.current.state.phase).toBe('error'))
    },
  )
})
