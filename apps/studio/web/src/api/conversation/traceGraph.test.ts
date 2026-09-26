import frameworkResume from '../../../../../../packages/tinkerfin/tests/fixtures/agui-history-resume.json'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { clearAuthSession, saveAuthSession } from '../../auth/session'
import {
  compareTraceGraphIds,
  followTraceGraph,
  parseTraceGraph,
  parseTraceGraphDelta,
  parseTraceGraphPage,
  parseTraceGraphQueryPage,
  queryTraceGraph,
  type TraceGraphPage,
} from './traceGraph'

const page = (): TraceGraphPage => ({
  turns: [{
    id: 'turn-1',
    ordinal: 1,
    startedAt: '2026-08-31T00:00:00Z',
  }],
  nodes: [
    {
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
      startedSeq: 2,
      updatedSeq: 2,
      content: 'Show the current trace',
      contentOmitted: false,
      toolCallOnly: false,
      requestOmitted: false,
      resultOmitted: false,
      linkIssues: [],
    },
    {
      agui: null,
      id: 'model-call',
      turnId: 'turn-1',
      parentSubagentId: null,
      modelCallId: null,
      kind: 'model',
      status: 'succeeded',
      name: 'deepseek-chat',
      runId: 'run-1',
      graphNamespace: [],
      provider: 'deepseek',
      model: 'deepseek-chat',
      startedAt: '2026-08-31T00:00:00Z',
      firstOutputAt: '2026-08-31T00:00:00.200Z',
      completedAt: '2026-08-31T00:00:01Z',
      startedSeq: 4,
      updatedSeq: 6,
      contentOmitted: false,
      toolCallOnly: false,
      request: { messages: [] },
      requestOmitted: false,
      resultOmitted: false,
      usage: { input_tokens: 2, output_tokens: 1 },
      linkIssues: [],
    },
  ],
  orderedNodeIds: ['human-1', 'model-call'],
  matchedNodeIds: ['model-call'],
  nextCursor: null,
  asOfSeq: 8,
  completeness: {
    callTrackingMissing: false,
    relationshipEvidenceMissing: false,
    detailsOmitted: false,
  },
})

const eventStream = (...events: unknown[]) => {
  const encoder = new TextEncoder()
  return new Response(new ReadableStream({
    start(controller) {
      events.forEach((event) => {
        controller.enqueue(encoder.encode(`event: trace\ndata: ${JSON.stringify(event)}\n\n`))
      })
      controller.close()
    },
  }), { headers: { 'Content-Type': 'text/event-stream' } })
}

describe('Trace Graph client', () => {
  beforeEach(() => {
    saveAuthSession({
      token: 'trace-token',
      serverAddress: 'http://127.0.0.1:8090', tokenType: 'Bearer',
      expiresAt: '2099-01-01T00:00:00.000Z',
      user: {
        user_id: 7,
        username: 'trace-user',
        display_name: 'Trace User',
        avatar_url: null,
        roles: [],
        disabled: false,
      },
    })
  })

  afterEach(() => {
    clearAuthSession()
    vi.unstubAllGlobals()
  })

  it('pushes every selected filter to the current Graph route', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = input instanceof Request
        ? input
        : new Request(new URL(String(input), window.location.origin), init)
      const url = new URL(request.url)
      expect(request.headers.get('Authorization')).toBe('Bearer trace-token')
      expect(url.pathname).toBe('/api/conversation/thread-1/trace/graph/follow')
      expect(url.searchParams.getAll('kind')).toEqual(['model', 'subagent'])
      expect(url.searchParams.getAll('status')).toEqual(['failed'])
      expect(url.searchParams.getAll('provider')).toEqual(['deepseek'])
      expect(url.searchParams.get('graph_namespace')).toBe('root')
      expect(url.searchParams.get('query')).toBe('deepseek')
      expect(url.searchParams.has('includeTechnicalNodes')).toBe(false)
      expect(url.searchParams.has('includeAncestorNodes')).toBe(false)
      expect(url.searchParams.get('limit')).toBe('1000')
      return eventStream({ type: 'snapshot', snapshot: { ...page(), generation: 'generation-test', headRunId: 'run-fixture' } })
    }))

    const events = []
    for await (const event of followTraceGraph('thread-1', {
      kinds: ['model', 'subagent'],
      statuses: ['failed'],
      providers: ['deepseek'],
      graphNamespaces: [[]],
      query: 'deepseek',
    }, { limit: 1000 })) events.push(event)

    expect(events[0]).toHaveProperty('snapshot.nodes.0.name', 'HumanMessage')
  })

  it('查询页要求固定身份且保留完整模型请求，基础图不接受查询身份', () => {
    const source = { ...page(), generation: 'generation-test', headRunId: 'run-fixture' }
    expect(parseTraceGraphQueryPage(source)).toEqual(source)
    expect(() => parseTraceGraphQueryPage(page())).toThrow('stream_event_invalid')
    expect(() => parseTraceGraphPage(source)).toThrow('stream_event_invalid')
    expect(() => parseTraceGraphQueryPage({ ...source, headRunId: '' })).toThrow('stream_event_invalid')
    const model = source.nodes.find(node => node.kind === 'model')!
    model.request = { messages: [{ content: '独立链路中的模型原始输入' }] }
    expect(parseTraceGraphQueryPage(source).nodes.find(node => node.kind === 'model')?.request).toEqual(model.request)
  })

  it('queries one model response from the direct Graph route', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = input instanceof Request
        ? input
        : new Request(new URL(String(input), window.location.origin), init)
      const url = new URL(request.url)
      expect(url.pathname).toBe('/api/conversation/thread-1/trace/graph')
      expect(url.searchParams.get('modelCallId')).toBe('model-call')
      expect(url.searchParams.getAll('kind')).toEqual([
        'assistant_message',
        'tool',
        'subagent',
      ])
      expect(url.searchParams.get('limit')).toBe('1000')
      return new Response(JSON.stringify({ code: 0, message: 'ok', data: { ...page(), generation: 'generation-test', headRunId: 'run-fixture' } }), {
        headers: { 'Content-Type': 'application/json' },
      })
    }))

    const result = await queryTraceGraph('thread-1', {
      modelCallId: 'model-call',
      kinds: ['assistant_message', 'tool', 'subagent'],
    }, { limit: 1000 })

    expect(result.nodes[0]?.name).toBe('HumanMessage')
  })

  it('parses authoritative ordering and removal updates', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => eventStream(
      { type: 'snapshot', snapshot: { ...page(), generation: 'generation-test', headRunId: 'run-fixture' } },
      {
        type: 'update',
        update: {
          asOfSeq: 9,
          nextCursor: 'current-older-page',
          turnUpserts: [],
          turnRemoves: [],
          nodeUpserts: [],
          nodeRemoves: ['model-call'],
          orderedNodeIds: ['human-1'],
          matchedNodeIds: [],
          completeness: page().completeness,
        },
      },
    )))

    const events = []
    for await (const event of followTraceGraph('thread-1', { kinds: ['model'] })) {
      events.push(event)
    }

    expect(events.map((event) => event.type)).toEqual(['snapshot', 'update'])
    expect(events[1]).toMatchObject({
      update: {
        nextCursor: 'current-older-page',
        nodeRemoves: ['model-call'],
      },
    })
  })

  it('rejects unknown event-envelope fields', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => eventStream({
      type: 'snapshot',
      snapshot: { ...page(), generation: 'generation-test', headRunId: 'run-fixture' },
      legacyEntries: [],
    })))

    await expect(async () => {
      for await (const _event of followTraceGraph('thread-1', {})) {
        void _event
      }
    }).rejects.toThrow()
  })

  it('rejects malformed nodes, ordering and completeness', () => {
    const invalid = page()
    Reflect.deleteProperty(invalid.nodes[0] as object, 'startedSeq')
    expect(() => parseTraceGraphPage(invalid)).toThrow()

    const invalidOrder = page()
    invalidOrder.orderedNodeIds = ['model-call']
    expect(() => parseTraceGraphPage(invalidOrder)).toThrow()

    const invalidMatch = page()
    invalidMatch.matchedNodeIds = ['unknown']
    expect(() => parseTraceGraphPage(invalidMatch)).toThrow()

    const invalidMatchOrder = page()
    invalidMatchOrder.matchedNodeIds = ['model-call', 'human-1']
    expect(() => parseTraceGraphPage(invalidMatchOrder)).toThrow()

    const invalidParent = page()
    invalidParent.nodes[1].parentSubagentId = 'unknown'
    expect(() => parseTraceGraphPage(invalidParent)).toThrow()

    const invalidCompleteness = page()
    Reflect.set(invalidCompleteness.completeness, 'detailsOmitted', 'false')
    expect(() => parseTraceGraphPage(invalidCompleteness)).toThrow()

    const unknownNodeField = page()
    Reflect.set(unknownNodeField.nodes[0], 'legacyEntryId', 'entry-1')
    expect(() => parseTraceGraphPage(unknownNodeField)).toThrow()

    const graphWithoutCursor = page()
    Reflect.deleteProperty(graphWithoutCursor, 'nextCursor')
    expect(() => parseTraceGraphPage(graphWithoutCursor)).toThrow()
    expect(parseTraceGraph(graphWithoutCursor).asOfSeq).toBe(8)
    expect(() => parseTraceGraph(page())).toThrow()
  })

  it('uses Python-compatible Unicode code point ordering', () => {
    expect(compareTraceGraphIds('node-\uE000', 'node-😀')).toBeLessThan(0)
    expect(compareTraceGraphIds('node-😀', 'node-\uE000')).toBeGreaterThan(0)
    expect(compareTraceGraphIds('node-😀', 'node-😀')).toBe(0)
  })

  it('parses the four-thousand-node Store boundary iteratively', () => {
    const source = page()
    const nodes = Array.from({ length: 4000 }, (_, index) => ({
      ...source.nodes[0]!,
      id: `human-${String(index).padStart(4, '0')}`,
      startedSeq: index + 1,
      updatedSeq: index + 1,
    }))
    const ids = nodes.map((node) => node.id)

    expect(parseTraceGraphPage({
      ...source,
      nodes,
      orderedNodeIds: ids,
      matchedNodeIds: ids,
      asOfSeq: 4000,
    }).nodes).toHaveLength(4000)
  })

  it('accepts 64 Subagent levels and rejects level 65 without recursion', () => {
    const source = page()
    const nodes = Array.from({ length: 64 }, (_, index) => ({
      ...source.nodes[0]!,
      id: `subagent-${index}`,
      parentSubagentId: index === 0 ? null : `subagent-${index - 1}`,
      kind: 'subagent' as const,
      name: `subagent-${index}`,
      graphNamespace: Array.from({ length: index + 1 }, (__, part) => `tools:${part}`),
      startedSeq: index + 1,
      updatedSeq: index + 1,
    }))
    const ids = nodes.map((node) => node.id)
    expect(parseTraceGraphPage({
      ...source,
      nodes,
      orderedNodeIds: ids,
      matchedNodeIds: ids,
      asOfSeq: 64,
    }).nodes).toHaveLength(64)

    const tooDeep = [
      ...nodes,
      {
        ...nodes[63]!,
        id: 'subagent-64',
        parentSubagentId: 'subagent-63',
        name: 'subagent-64',
        startedSeq: 65,
        updatedSeq: 65,
      },
    ]
    expect(() => parseTraceGraphPage({
      ...source,
      nodes: tooDeep,
      orderedNodeIds: tooDeep.map((node) => node.id),
      matchedNodeIds: tooDeep.map((node) => node.id),
      asOfSeq: 65,
    })).toThrow()
  })

  it('rejects cyclic, non-Subagent, cross-Turn and future references', () => {
    const source = page()
    const first = {
      ...source.nodes[0]!,
      id: 'subagent-a',
      parentSubagentId: 'subagent-b',
      kind: 'subagent' as const,
      name: 'subagent-a',
      graphNamespace: ['tools:a'],
      startedSeq: 1,
      updatedSeq: 1,
    }
    const second = {
      ...first,
      id: 'subagent-b',
      parentSubagentId: 'subagent-a',
      name: 'subagent-b',
      graphNamespace: ['tools:a', 'tools:b'],
      startedSeq: 2,
      updatedSeq: 2,
    }
    expect(() => parseTraceGraph({
      ...source,
      nodes: [first, second],
      orderedNodeIds: [first.id, second.id],
      matchedNodeIds: [first.id, second.id],
      asOfSeq: 2,
    })).toThrow()

    const toolParent = { ...source.nodes[0]!, id: 'tool-parent', kind: 'tool' as const }
    const nonSubagentChild = { ...first, parentSubagentId: toolParent.id }
    expect(() => parseTraceGraph({
      ...source,
      nodes: [toolParent, nonSubagentChild],
      orderedNodeIds: [toolParent.id, nonSubagentChild.id],
      matchedNodeIds: [toolParent.id, nonSubagentChild.id],
      asOfSeq: 2,
    })).toThrow()

    const secondTurn = {
      id: 'turn-2',
      ordinal: 2,
      startedAt: source.turns[0]!.startedAt,
    }
    expect(() => parseTraceGraph({
      ...source,
      turns: [...source.turns, secondTurn],
      nodes: [
        { ...first, parentSubagentId: null },
        { ...second, parentSubagentId: first.id, turnId: secondTurn.id },
      ],
      orderedNodeIds: [first.id, second.id],
      matchedNodeIds: [first.id, second.id],
      asOfSeq: 2,
    })).toThrow()

    expect(() => parseTraceGraph({
      ...source,
      nodes: [{ ...source.nodes[0]!, updatedSeq: 9 }],
      orderedNodeIds: ['human-1'],
      matchedNodeIds: ['human-1'],
      asOfSeq: 8,
    })).toThrow()
  })

  it('rejects overlapping, duplicate, unordered and future Delta references', () => {
    const source = page()
    const valid = {
      asOfSeq: 9,
      nextCursor: null,
      turnUpserts: [],
      turnRemoves: [],
      nodeUpserts: [],
      nodeRemoves: ['model-call'],
      orderedNodeIds: ['human-1'],
      matchedNodeIds: ['human-1'],
      completeness: source.completeness,
    }
    expect(parseTraceGraphDelta(valid).asOfSeq).toBe(9)
    expect(() => parseTraceGraphDelta({
      ...valid,
      nodeRemoves: ['model-call', 'model-call'],
    })).toThrow()
    expect(() => parseTraceGraphDelta({
      ...valid,
      nodeUpserts: [source.nodes[1]],
      nodeRemoves: ['model-call'],
      orderedNodeIds: ['human-1', 'model-call'],
    })).toThrow()
    expect(() => parseTraceGraphDelta({
      ...valid,
      nodeRemoves: [],
      matchedNodeIds: ['model-call', 'human-1'],
      orderedNodeIds: ['human-1', 'model-call'],
    })).toThrow()
    expect(() => parseTraceGraphDelta({
      ...valid,
      nodeUpserts: [{ ...source.nodes[1]!, updatedSeq: 10 }],
      nodeRemoves: [],
      orderedNodeIds: ['human-1', 'model-call'],
    })).toThrow()
  })
})


it('图页和增量保留框架提供的关联，同时保持原生节点顺序和游标', () => {
  const graph = frameworkResume.history.graph
  const page = parseTraceGraphPage({ ...graph, nextCursor: 'opaque-next-page' })
  const delta = parseTraceGraphDelta({
    asOfSeq: graph.asOfSeq, nextCursor: 'opaque-next-page',
    turnUpserts: graph.turns, turnRemoves: [], nodeUpserts: graph.nodes, nodeRemoves: [],
    orderedNodeIds: graph.orderedNodeIds, matchedNodeIds: graph.matchedNodeIds, completeness: graph.completeness,
  })
  expect(page.nodes.map(node => node.agui)).toEqual(delta.nodeUpserts.map(node => node.agui))
  expect(page.nodes.find(node => node.kind === 'tool')?.agui).toMatchObject({ kind: 'tool' })
  expect(page.orderedNodeIds).toEqual(graph.orderedNodeIds)
  expect(delta.orderedNodeIds).toEqual(graph.orderedNodeIds)
  expect(page.nextCursor).toBe('opaque-next-page')
  expect(delta.nextCursor).toBe('opaque-next-page')
  const invalidNode = { ...graph.nodes.find(node => node.kind === 'tool'), agui: { kind: 'tool', toolCallId: 'valid', unknown: 'field' } }
  expect(() => parseTraceGraphDelta({ ...delta, nodeUpserts: [invalidNode] })).toThrow('stream_event_invalid')
})
