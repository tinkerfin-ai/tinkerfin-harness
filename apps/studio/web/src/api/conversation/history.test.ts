import frameworkResume from '../../../../../../packages/tinkerfin/tests/fixtures/agui-history-resume.json'
import { restoreConversationFromTrace } from '../../features/conversation/trace/runtime'
import { applyConversationEvent, prepareResumeSubmission } from '../../features/conversation/agui'
import { parseConversationAgUiEvent } from './eventParser'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  deleteConversation,
  fetchConversationHistoryDetail,
  fetchConversationHistoryGroupConfig,
  fetchConversationHistoryList,
  followConversationTrace,
  patchConversation,
  type ConversationHistoryDetail,
} from './history'
import { clearAuthSession, saveAuthSession } from '../../auth/session'
import { emptyTraceGraph, emptyTraceGraphDelta } from '../../test/traceFixtures'

function envelope(data: unknown, code = 0, message = 'success', status = 200) {
  return new Response(JSON.stringify({ code, message, data }), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

const detail = (): ConversationHistoryDetail => ({
  titleSource: 'default',
  titleGenerationStatus: 'idle',
  titleSeq: 0,
  id: 1,
  threadId: 'thread-trace',
  title: 'Trace 会话',
  lastModel: 'main',
  pinned: false,
  asOfSeq: 4,
  generation: 'generation-test',
  observedAt: '2026-09-05T00:00:00.000000Z',
  headRunId: 'run-1',
  runFailures: [],
  availableHeads: ['run-1'],
  historyCursor: null,
  messageCount: 1,
  toolCallCount: 0,
  messages: [],
  reasoning: [],
  graph: emptyTraceGraph(4),
  state: { root: {}, subgraphs: {} },
  interactions: [],
  status: { execution: 'succeeded', headRunId: 'run-1' },
  completeness: { missingPrefix: false, missingTail: false, payloadOmitted: false },
  taskTrace: { status: 'ready', todoGroups: [] },
  createdAt: '2026-08-28T00:00:00',
  updatedAt: '2026-08-28T00:01:00',
})

const frameworkDetail = (history: typeof frameworkResume.history | typeof frameworkResume.finalHistory) => ({
  ...detail(),
  ...history,
  status: history.summary.status,
  completeness: history.summary.completeness,
  messageCount: history.summary.messageCount,
  toolCallCount: history.summary.toolCallCount,
})

const streamResponse = (...values: unknown[]) => {
  const encoder = new TextEncoder()
  return new Response(new ReadableStream<Uint8Array>({
    start(controller) {
      for (const value of values) {
        controller.enqueue(encoder.encode(
          'event: trace\ndata: ' + JSON.stringify(value) + '\n\n',
        ))
      }
      controller.close()
    },
  }), { headers: { 'Content-Type': 'text/event-stream' } })
}

describe('conversation Trace client', () => {
  beforeEach(() => {
    saveAuthSession({
      token: 'history-token',
      tokenType: 'Bearer',
      expiresAt: '2099-01-01T00:00:00.000Z',
      user: {
        user_id: 7,
        username: 'yunsan',
        display_name: '云杉',
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

  it.each([
    ['list', () => fetchConversationHistoryList()],
    ['config', () => fetchConversationHistoryGroupConfig()],
    ['detail', () => fetchConversationHistoryDetail('thread-auth', {
      includeTaskTrace: true,
    })],
    ['patch', () => patchConversation('thread-auth', { title: '新标题' })],
    ['delete', () => deleteConversation('thread-auth')],
  ])('sends the session bearer token for %s requests', async (_name, request) => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const sentRequest = input instanceof Request ? input : new Request(input)
      expect(sentRequest.headers.get('Authorization')).toBe('Bearer history-token')
      if (sentRequest.method === 'DELETE') return envelope(null)
      return envelope({ items: [], nextCursor: null, dayRanges: [7, 30], ...detail() })
    })
    vi.stubGlobal('fetch', fetchMock)

    await request()

    expect(fetchMock).toHaveBeenCalledOnce()
  })

  it('encodes list and fixed Trace history cursors independently', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const request = input instanceof Request ? input : new Request(input)
      const url = new URL(request.url)
      if (url.pathname.endsWith('/history') && url.pathname.includes('thread-trace')) {
        expect(url.searchParams.get('historyCursor')).toBe('opaque-trace-cursor')
        expect(url.searchParams.get('limit')).toBe('40')
        expect(url.searchParams.get('includeTaskTrace')).toBe('false')
        return envelope({ ...detail(), taskTrace: null })
      }
      expect(url.searchParams.get('pageSize')).toBe('5')
      expect(url.searchParams.get('cursor')).toBe('opaque-list-cursor')
      expect(url.searchParams.get('query')).toBe('目标会话')
      return envelope({ items: [], nextCursor: null })
    })
    vi.stubGlobal('fetch', fetchMock)

    await fetchConversationHistoryList({
      pageSize: 5,
      cursor: 'opaque-list-cursor',
      query: '目标会话',
    })
    await fetchConversationHistoryDetail('thread-trace', {
      includeTaskTrace: false,
      historyCursor: 'opaque-trace-cursor',
      limit: 40,
    })

    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('parses the mandatory snapshot before semantic Trace updates', async () => {
    const snapshot = { type: 'snapshot' as const, snapshot: detail() }
    const update = {
      type: 'update' as const,
      runFailures: [],
      taskTrace: null,
      update: {
        asOfSeq: 5,
        generation: 'generation-test',
        observedAt: '2026-09-05T00:00:00.000001Z',
        events: [],
        facts: [],
        messages: { upserts: [], removes: [] },
        reasoning: { upserts: [], removes: [] },
        graph: emptyTraceGraphDelta(5),
        interactions: { upserts: [], removes: [] },
        state: { root: {}, subgraphs: {} },
        status: { execution: 'succeeded', headRunId: 'run-1' },
        completeness: { missingPrefix: false, missingTail: false, payloadOmitted: false },
        messageCount: 1,
        toolCallCount: 0,
        projections: {}, runFailures: [],
      },
    }
    vi.stubGlobal('fetch', vi.fn(async () => streamResponse(snapshot, update)))

    const received = []
    for await (const event of followConversationTrace('thread-trace', {
      includeTaskTrace: true,
    })) received.push(event)

    expect(received).toEqual([snapshot, update])
  })

  it('rejects a non-Trace SSE event instead of coercing it', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => streamResponse({ type: 'RUN_STARTED' })))

    const consume = async () => {
      for await (const event of followConversationTrace('thread-trace', {
        includeTaskTrace: true,
      })) {
        // 消费完整流以触发边界校验
        void event
      }
    }

    await expect(consume()).rejects.toThrow()
  })

  it('rejects mismatched true and false task trace expectations', async () => {
    vi.stubGlobal('fetch', vi.fn()
      .mockResolvedValueOnce(envelope({ ...detail(), taskTrace: null }))
      .mockResolvedValueOnce(envelope(detail())))

    await expect(fetchConversationHistoryDetail('thread-trace', {
      includeTaskTrace: true,
    })).rejects.toThrow()
    await expect(fetchConversationHistoryDetail('thread-trace', {
      includeTaskTrace: false,
    })).rejects.toThrow()
  })

  it('preserves delete conflicts and accepts a null success envelope', async () => {
    vi.stubGlobal('fetch', vi.fn()
      .mockResolvedValueOnce(envelope(
        null,
        1_001_004_003,
        '会话仍在运行，请先停止并等待运行结束',
        409,
      ))
      .mockResolvedValueOnce(envelope(null)))

    await expect(deleteConversation('thread-running')).rejects.toThrow(
      '会话仍在运行，请先停止并等待运行结束',
    )
    await expect(deleteConversation('thread-idle')).resolves.toBeUndefined()
  })
  it('真实框架审批恢复流与失败后的历史重载保持相同工具结果', async () => {
    const wire = frameworkDetail(frameworkResume.history)
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(envelope(wire)))
    const loaded = await fetchConversationHistoryDetail(wire.threadId, { includeTaskTrace: true })
    let conversation = prepareResumeSubmission(restoreConversationFromTrace(loaded, { model: 'main', includeTaskTrace: true }))
    for (const event of frameworkResume.resumedEvents) {
      conversation = applyConversationEvent(conversation, parseConversationAgUiEvent(event))
    }
    const results = (value: typeof conversation) => value.messages.filter(message => message.role === 'tool')
      .map(message => ({ name: message.meta?.toolName, id: message.meta?.toolCallId, status: message.meta?.status, result: message.meta?.result }))
    expect(results(conversation)).toHaveLength(2)
    expect(results(conversation)[0]).toMatchObject({ name: 'save_report', status: 'completed' })
    expect(results(conversation)[1]).toMatchObject({ name: 'fail_delivery', status: 'failed' })
    const finalWire = frameworkDetail(frameworkResume.finalHistory)
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(envelope(finalWire)))
    const final = restoreConversationFromTrace(await fetchConversationHistoryDetail(finalWire.threadId, { includeTaskTrace: true }), { model: 'main', includeTaskTrace: true })
    expect(results(final).map(({ name, id, status }) => ({ name, id, status })))
      .toEqual(results(conversation).map(({ name, id, status }) => ({ name, id, status })))
    expect(results(final)[0]?.result).toBe(results(conversation)[0]?.result)
    expect(final.approval).toBeUndefined()
    expect(final.runStatus).toBe('error')
  })

  it.each([
    ['缺少消息关联字段', (wire: Record<string, unknown>) => { wire.messages = [{ role: 'assistant' }] }],
    ['消息使用工具结果关联', (wire: Record<string, unknown>) => { wire.messages = [{ role: 'assistant', agui: { kind: 'tool_message', messageId: 'm', toolCallId: 't' } }] }],
    ['消息关联含未知字段', (wire: Record<string, unknown>) => { wire.messages = [{ role: 'assistant', agui: { kind: 'message', messageId: 'm', extra: true } }] }],
    ['缺少交互关联字段', (wire: Record<string, unknown>) => { wire.interactions = [{ status: 'pending' }] }],
    ['待审批交互没有动作', (wire: Record<string, unknown>) => { wire.interactions = [{ status: 'pending', agui: [] }] }],
    ['工具节点缺少关联字段', (wire: Record<string, unknown>) => {
      const graph = wire.graph as { nodes: Array<Record<string, unknown>> }
      const tool = graph.nodes.find(node => node.kind === 'tool')!
      delete tool.agui
    }],
    ['工具节点使用子智能体关联', (wire: Record<string, unknown>) => {
      const graph = wire.graph as { nodes: Array<Record<string, unknown>> }
      graph.nodes.find(node => node.kind === 'tool')!.agui = { kind: 'subagent', parentToolCallId: 'p', subagentInvocationId: 'c' }
    }],
  ])('拒绝不符合实体关联契约的历史：%s', async (_name, mutate) => {
    const wire = structuredClone(frameworkDetail(frameworkResume.history))
    mutate(wire)
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(envelope(wire)))
    await expect(fetchConversationHistoryDetail(wire.threadId, { includeTaskTrace: true })).rejects.toThrow('stream_event_invalid')
  })

  it('允许明确缺失的关联与已解决交互空动作，拒绝把缺失当作旧格式', async () => {
    const wire = structuredClone(frameworkDetail(frameworkResume.history))
    const messages: Array<{ agui: unknown }> = wire.messages
    messages.forEach(message => { message.agui = null })
    const interaction = wire.interactions[0]!
    interaction.status = 'resolved'
    interaction.agui = []
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(envelope(wire)))
    const loaded = await fetchConversationHistoryDetail(wire.threadId, { includeTaskTrace: true })
    expect(loaded.messages.every(message => message.agui === null)).toBe(true)
    expect(loaded.interactions[0]?.agui).toEqual([])
  })

  it('订阅增量携带消息关联，缺少关联字段时拒绝该增量', async () => {
    const base = detail()
    const message = frameworkResume.history.messages[0]!
    const update = {
      type: 'update', runFailures: [], taskTrace: null,
      update: {
        asOfSeq: 5, generation: base.generation, observedAt: base.observedAt,
        messages: { upserts: [message], removes: [] }, reasoning: { upserts: [], removes: [] },
        interactions: { upserts: [], removes: [] }, graph: emptyTraceGraphDelta(5),
        state: base.state, status: base.status, completeness: base.completeness,
        events: [], facts: [], messageCount: 1, toolCallCount: 0, projections: {},
      },
    }
    vi.stubGlobal('fetch', vi.fn(async () => streamResponse(update)))
    const events = []
    for await (const event of followConversationTrace(base.threadId, { includeTaskTrace: true })) events.push(event)
    expect(events[0]).toHaveProperty('update.messages.upserts.0.agui', message.agui)
    const invalid: Record<string, unknown> = { ...message }
    delete invalid.agui
    vi.stubGlobal('fetch', vi.fn(async () => streamResponse({ ...update, update: { ...update.update, messages: { upserts: [invalid], removes: [] } } })))
    await expect(async () => {
      for await (const event of followConversationTrace(base.threadId, { includeTaskTrace: true })) events.push(event)
    }).rejects.toThrow('stream_event_invalid')
  })

})
