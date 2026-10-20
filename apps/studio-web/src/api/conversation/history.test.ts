import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import frameworkResume from '../../../../../packages/tinkerfin/tests/fixtures/agui-history-resume.json'
import { applyConversationEvent, prepareResumeSubmission } from '../../features/conversation/agui'
import { restoreConversationFromTrace } from '../../features/conversation/trace/runtime'
import { parseConversationAgUiEvent } from './eventParser'

import { clearAuthSession, saveAuthSession } from '../../auth/session'
import { emptyTraceGraph } from '../../test/traceFixtures'
import {
  fetchConversationHistoryDetail,
  followConversationRun,
  parseInteractionAvailability,
  parseSubmissionResult,
  type ConversationHistoryDetail,
} from './history'

it('未保存证明与当前权限分别解析，空结果明确表示没有证明', () => {
  const proof = { submissionRunId: 'failed-run', interruptIds: ['first', 'second'], state: 'not_saved' }
  expect(parseSubmissionResult(proof)).toEqual(proof)
  expect(parseSubmissionResult(null)).toBeNull()
  expect(parseInteractionAvailability([{ interruptId: 'first', state: 'confirming', submissionRunId: 'current-run' }]))
    .toEqual([{ interruptId: 'first', state: 'confirming', submissionRunId: 'current-run' }])
})

function envelope(data: unknown, code = 0, message = 'success', status = 200) {
  return new Response(JSON.stringify({ code, message, data }), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

const detail = (): ConversationHistoryDetail => ({projectId: 'project-1', archived: false,  accessMode: 'write_approval',
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
  submissionResult: null, planResults: [], interactionAvailability: [], interactions: [],
  status: { execution: 'succeeded', headRunId: 'run-1' },
  completeness: { missingPrefix: false, missingTail: false, payloadOmitted: false },
  taskTrace: { status: 'ready', todoGroups: [] },
  createdAt: '2026-08-28T00:00:00',
  updatedAt: '2026-08-28T00:01:00',
})

const frameworkDetail = (history: typeof frameworkResume.history | typeof frameworkResume.finalHistory) => ({
  ...detail(),
  ...history,
  graph: {
    ...history.graph,
    nodes: history.graph.nodes.map(node => {
      if (node.kind !== 'model') return node
      const { request: _request, ...model } = node
      void _request
      return model
    }),
  },
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
      serverAddress: 'http://127.0.0.1:8090', tokenType: 'Bearer',
      expiresAt: '2099-01-01T00:00:00.000Z',
      user: {
        user_id: 7,
        username: 'yunsan',
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

  it('续播缺失基线不能把完整旧视图与增量混合', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => streamResponse({ type: 'RUN_STARTED', threadId: 'thread-trace', runId: 'run-1' })))
    const stream = followConversationRun('thread-trace', 'run-1', { includeTaskTrace: true, signal: new AbortController().signal })
    await expect(stream.next()).rejects.toMatchObject({ code: 'stream_event_invalid' })
  })

  it('原生文件审批历史保留原始提案并展示实际审批参数', async () => {
    const sourceArgs = { file_path: 'report.txt', content: 'approved result' }
    const effectiveArgs = { ...sourceArgs, file_path: '/project/report.txt' }
    const wire: ConversationHistoryDetail = {
      ...detail(),
      status: { execution: 'waiting', headRunId: 'run-1' },
      interactionAvailability: [{ interruptId: 'file-review', state: 'available', submissionRunId: null }],
      interactions: [{
        id: 'interaction-file-review', traceSeq: 4, sourceId: 'file-review',
        graphNamespace: [], runId: 'run-1', kind: 'tool_review', toolCallIds: ['file-call'],
        status: 'pending', payloadOmitted: false, openedAt: '2026-09-05T00:00:00.000000Z',
        agui: [{
          id: 'file-review', reason: 'tool_call', toolCallId: 'file-call',
          metadata: {
            deepagents: {
              schema: 'tinkerfin.deepagents.tool-review', nativeInterruptId: 'file-review',
              actionIndex: 0, toolName: 'write_file', allowedDecisions: ['approve', 'edit', 'reject'],
              originalArgs: effectiveArgs,
            },
            langgraphValue: {
              action_requests: [{
                name: 'write_file', args: effectiveArgs, source_args: sourceArgs, description: 'Write report',
              }],
              review_configs: [{ action_name: 'write_file', allowed_decisions: ['approve', 'edit', 'reject'] }],
            },
          },
        }],
      }],
    }
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(envelope(wire)))
    const loaded = await fetchConversationHistoryDetail(wire.threadId, { includeTaskTrace: true })
    const conversation = restoreConversationFromTrace(loaded, { model: 'main', includeTaskTrace: true })
    expect(conversation.runStatus).toBe('waiting_approval')
    expect(conversation.approval?.items).toHaveLength(1)
    expect(conversation.approval?.items[0]).toMatchObject({
      interruptId: 'file-review', originalArgs: effectiveArgs,
    })
    expect(JSON.parse(conversation.approval!.items[0].params)).toEqual(effectiveArgs)
    expect(loaded.interactions[0].agui?.[0].metadata?.langgraphValue).toEqual(
      wire.interactions[0].agui?.[0].metadata?.langgraphValue,
    )
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

})
