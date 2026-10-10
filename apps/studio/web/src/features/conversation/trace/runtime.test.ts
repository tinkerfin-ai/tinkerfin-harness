import { describe, expect, it } from 'vitest'
import type { ConversationAgUiEvent } from '../../../api/conversation/types'
import { toolReviewInterrupts } from '../../../test/aguiFixtures'
import { applyConversationEvent, prepareResumeSubmission } from '../agui'

import type { ConversationHistoryDetail } from '../../../api/conversation/history'
import { restoreConversationFromTrace } from './runtime'

const approvalDetail = (): ConversationHistoryDetail => {
  const source = detail()
  source.status = { execution: 'waiting', headRunId: source.headRunId }
  const actions = toolReviewInterrupts('review-group', [
    { toolCallId: 'call-first', args: { file_path: '/first.txt' } },
    { toolCallId: 'call-second', args: { file_path: '/second.txt' } },
  ])
  source.interactions = [{
    id: 'group', traceSeq: 5, sourceId: 'review-group', graphNamespace: [], runId: 'run-1',
    kind: 'tool_approval', toolCallIds: ['call-first', 'call-second'], status: 'pending',
    payloadOmitted: false, openedAt: '2026-08-28T00:00:04Z', agui: actions,
  }]
  source.interactionAvailability = actions.map(action => ({ interruptId: action.id, state: 'available', submissionRunId: null }))
  return source
}

it('只有当前提交的完整未保存确认可恢复审批，原输入无需重填', () => {
  const source = approvalDetail()
  const initial = restoreConversationFromTrace(source, { model: 'main', includeTaskTrace: false })
  initial.approval!.items[0].decision = 'rejected'
  initial.approval!.items[0].rejectionReason = '保留该文件'
  const submitted = applyConversationEvent(prepareResumeSubmission(initial), {
    type: 'RUN_STARTED', threadId: source.threadId, runId: 'resume-run',
  })
  const stale = restoreConversationFromTrace(source, { previous: submitted, model: 'main', includeTaskTrace: false })
  expect(stale.approval?.submitted).toBe(true)
  const released = { ...source, submissionResult: {
    submissionRunId: 'resume-run', interruptIds: source.interactionAvailability.map(item => item.interruptId), state: 'not_saved' as const,
  } }
  const restored = restoreConversationFromTrace(released, { previous: stale, model: 'main', includeTaskTrace: false })
  expect(restored.approval?.submitted).toBe(false)
  expect(restored.approval?.items[0]).toMatchObject({ decision: 'rejected', rejectionReason: '保留该文件' })
})

const detail = (): ConversationHistoryDetail => ({projectId: 'project-1', archived: false,  accessMode: 'write_approval',
  titleSource: 'default',
  titleGenerationStatus: 'idle',
  titleSeq: 0,
  id: 1,
  threadId: 'thread-trace',
  title: 'Trace 会话',
  lastModel: 'main',
  pinned: false,
  asOfSeq: 8,
  generation: 'generation-test',
  observedAt: '2026-09-05T00:00:00.000000Z',
  headRunId: 'run-1',
  runFailures: [],
  availableHeads: ['run-1'],
  historyCursor: null,
  messageCount: 2,
  toolCallCount: 1,
  messages: [
    {
      agui: { kind: 'message', messageId: 'public-user-1' },
      id: 'message:user-1',
      traceSeq: 1,
      sourceId: 'user-1',
      graphNamespace: [],
      runId: 'run-1',
      role: 'user',
      content: '执行任务',
      contentOmitted: false,
      status: 'completed',
      createdAt: '2026-08-28T00:00:00Z',
      completedAt: '2026-08-28T00:00:00Z',
    },
    {
      agui: { kind: 'message', messageId: 'public-assistant-1' },
      id: 'message:assistant-1',
      traceSeq: 2,
      sourceId: 'assistant-1',
      graphNamespace: [],
      runId: 'run-1',
      role: 'assistant',
      content: '完成',
      contentOmitted: false,
      status: 'completed',
      createdAt: '2026-08-28T00:00:01Z',
      completedAt: '2026-08-28T00:00:02Z',
    },
    {
      agui: { kind: 'tool_message', messageId: 'public-result', toolCallId: 'public-call-write' },
      id: 'message:tool-result',
      traceSeq: 4,
      sourceId: 'tool-result',
      graphNamespace: [],
      runId: 'run-1',
      role: 'tool',
      content: 'written',
      contentOmitted: false,
      name: 'write_file',
      toolCallId: 'call-write',
      status: 'completed',
      createdAt: '2026-08-28T00:00:03Z',
      completedAt: '2026-08-28T00:00:03Z',
    },
  ],
  reasoning: [
    {
      id: 'reasoning-1',
      traceSeq: 3,
      messageId: 'message:assistant-1',
      graphNamespace: [],
      runId: 'run-1',
      extractor: 'deepseek.reasoning_content',
      content: '已授权推理',
      contentOmitted: false,
      status: 'completed',
      createdAt: '2026-08-28T00:00:01Z',
      completedAt: '2026-08-28T00:00:02Z',
    },
  ],
  graph: {
    turns: [{
      id: 'turn-1',
      ordinal: 1,
      startedAt: '2026-08-28T00:00:02Z',
    }],
    nodes: [{
      agui: { kind: 'tool', toolCallId: 'public-call-write' },
      id: 'tool-node',
      turnId: 'turn-1',
      parentSubagentId: null,
      modelCallId: null,
      kind: 'tool',
      status: 'succeeded',
      name: 'write_file',
      runId: 'run-1',
      graphNamespace: [],
      sourceId: 'call-write',
      startedAt: '2026-08-28T00:00:02Z',
      completedAt: '2026-08-28T00:00:03Z',
      startedSeq: 3,
      updatedSeq: 4,
      contentOmitted: false,
      toolCallOnly: false,
      request: { file_path: '/result.txt' },
      requestOmitted: false,
      result: 'written',
      resultOmitted: false,
      linkIssues: [],
    }],
    orderedNodeIds: ['tool-node'],
    matchedNodeIds: ['tool-node'],
    asOfSeq: 8,
    completeness: {
      callTrackingMissing: false,
      relationshipEvidenceMissing: false,
      detailsOmitted: false,
    },
  },
  state: {
    root: {
      todos: [{ content: '验证结果', status: 'completed' }],
      tinkerfin_plan: { effectiveMode: 'plan' },
    },
    subgraphs: {},
  },
  submissionResult: null, planResults: [], interactionAvailability: [], interactions: [],
  status: { execution: 'succeeded', headRunId: 'run-1' },
  completeness: { missingPrefix: false, missingTail: false, payloadOmitted: false },
  taskTrace: { status: 'ready', todoGroups: [] },
  createdAt: '2026-08-28T00:00:00',
  updatedAt: '2026-08-28T00:00:03',
})

const freezeSnapshot = (value: unknown): void => {
  if (value === null || typeof value !== 'object' || Object.isFrozen(value)) return
  Object.values(value).forEach(freezeSnapshot)
  Object.freeze(value)
}

describe('权威快照的共享与隔离', () => {

  it('冻结的历史根支持嵌套增量，失败批次和根替换不修改已有快照', () => {
    const restored = restoreConversationFromTrace(detail(), { model: 'main', includeTaskTrace: false })
    freezeSnapshot(restored.trace)
    const addition = { values: [{ count: 1 }] }
    const changed = applyConversationEvent(restored, {
      type: 'STATE_DELTA', delta: [
        { op: 'replace', path: '/todos/0/content', value: '实时任务' },
        { op: 'add', path: '/extra', value: addition },
      ],
    })
    addition.values[0].count = 2
    expect(changed.historySynchronized).toBe(false)
    expect(changed.trace).toBe(restored.trace)
    expect(changed.serverState).not.toBe(restored.serverState)
    expect(changed.serverState?.extra).toEqual({ values: [{ count: 1 }] })
    expect(changed.todos[0].content).toBe('实时任务')
    expect(restored.todos[0].content).toBe('验证结果')
    expect(restored.serverState?.todos).toEqual([{ content: '验证结果', status: 'completed' }])

    freezeSnapshot(changed.serverState)
    expect(() => applyConversationEvent(changed, {
      type: 'STATE_DELTA', delta: [
        { op: 'replace', path: '/todos/0/content', value: '不能提交' },
        { op: 'remove', path: '/missing' },
      ],
    })).toThrow()
    expect(changed.serverState?.todos).toEqual([{ content: '实时任务', status: 'completed' }])
    const replacement = { todos: [] }
    const replaced = applyConversationEvent(changed, {
      type: 'STATE_DELTA', delta: [{ op: 'replace', path: '', value: replacement }],
    })
    expect(replaced.serverState).toEqual(replacement)
    expect(replaced.serverState).not.toBe(replacement)
    expect(changed.serverState?.todos).toEqual([{ content: '实时任务', status: 'completed' }])
    expect(restored.serverState).toBe(restored.trace?.state.root)
  })
})

describe('Trace conversation projection', () => {

  it('rejects conflicting contents at an identical observation', () => {
    const source = detail()
    const current = restoreConversationFromTrace(source, { model: 'main', includeTaskTrace: true })
    for (const conflicting of [
      { ...source, messageCount: source.messageCount + 1 },
      { ...source, state: { root: { changed: true }, subgraphs: {} } },
      { ...source, messages: [{ ...source.messages[0]!, content: '矛盾内容' }, ...source.messages.slice(1)] },
    ]) {
      expect(() => restoreConversationFromTrace(conflicting, {
        previous: current, model: 'main', includeTaskTrace: true,
      })).toThrow('stream_event_invalid')
    }
  })

  it('preserves a caller-owned Messaging cursor across Trace projection', () => {
    const restored = restoreConversationFromTrace(detail(), {
      model: 'fallback',
      includeTaskTrace: true,
      lastDeliveredSeq: 73,
    })

    expect(restored.lastSeq).toBe(73)
  })

  it('correlates Tool results by full graphNamespace and source ID', () => {
    const source = detail()
    source.messages = [
      ...source.messages.filter((message) => message.role !== 'tool'),
      {
        ...source.messages[2]!,
        id: 'result-subgraph',
        graphNamespace: ['tools:child'],
        toolCallId: 'call-shared',
        content: 'subgraph-result',
      },
      {
        ...source.messages[2]!,
        id: 'result-root',
        graphNamespace: [],
        toolCallId: 'call-shared',
        content: 'root-result',
      },
      {
        ...source.messages[2]!,
        id: 'result-subagent',
        graphNamespace: [],
        toolCallId: 'call-task',
        content: 'subagent-result',
      },
    ]
    source.graph.nodes = [
      {
        ...source.graph.nodes[0]!,
        id: 'tool-root',
        graphNamespace: [],
        sourceId: 'call-shared',
      },
      {
        ...source.graph.nodes[0]!,
        id: 'subagent-child',
        kind: 'subagent',
        name: 'researcher',
        parentSubagentId: null,
        graphNamespace: ['tools:child'],
        sourceId: 'call-task',
        request: {
          description: '研究当前契约',
          subagent_type: 'researcher',
        },
        result: null,
      },
      {
        ...source.graph.nodes[0]!,
        id: 'tool-subgraph',
        startedSeq: 5,
        parentSubagentId: 'subagent-child',
        graphNamespace: ['tools:child'],
        sourceId: 'call-shared',
      },
    ]

    const restored = restoreConversationFromTrace(source, { model: 'fallback', includeTaskTrace: true })
    const results = Object.fromEntries(
      restored.messages
        .filter((message) => message.role === 'tool')
        .map((message) => [message.id, message.meta?.result]),
    )

    expect(results).toEqual({
      'tool-root': 'root-result',
      'tool-subgraph': 'subgraph-result',
    })
    expect(restored.messages.find((message) => message.role === 'subagent')?.meta)
      .toMatchObject({
        agentName: 'researcher',
        input: '研究当前契约',
        result: 'subagent-result',
      })
  })

  it('rejects mixed pending interaction groups instead of hiding approvals', () => {
    const source = detail()
    source.status = { execution: 'waiting', headRunId: 'run-1' }
    source.interactions = [
      {
      agui: toolReviewInterrupts('interrupt-tool', [{ toolCallId: 'public-call-write', args: {}, allowedDecisions: ['approve'] }]),
        id: 'interaction-tool',
        traceSeq: 5,
        sourceId: 'interrupt-tool',
        graphNamespace: [],
        runId: 'run-1',
        kind: 'tool_approval',
        toolCallIds: ['call-write'],
        status: 'pending',
        payloadOmitted: false,
        payload: {
          action_requests: [{ name: 'write_file', arguments: {} }],
          review_configs: [{
            action_name: 'write_file',
            allowed_decisions: ['approve'],
          }],
        },
        openedAt: '2026-08-28T00:00:04Z',
      },
      {
      agui: [{ id: 'unknown', reason: 'host_input' }],
        id: 'interaction-unknown',
        traceSeq: 6,
        sourceId: 'unknown',
        graphNamespace: [],
        runId: 'run-1',
        kind: 'host_input',
        toolCallIds: [],
        status: 'pending',
        payloadOmitted: false,
        payload: {},
        openedAt: '2026-08-28T00:00:05Z',
      },
    ]

    expect(() => restoreConversationFromTrace(source, { model: 'fallback', includeTaskTrace: true }))
      .toThrowError('stream_event_invalid')
  })
})

describe('历史恢复与实时续流联合验证', () => {
  it('审批恢复后的成功工具不会被后续失败覆盖，也不会新增无名工具卡片', () => {
    const snapshot = detail()
    snapshot.status.execution = 'waiting'
    snapshot.messages = snapshot.messages.filter(message => message.role !== 'tool')
    snapshot.graph.nodes[0] = { ...snapshot.graph.nodes[0]!, status: 'waiting', completedAt: null, result: null }
    snapshot.interactions = [{
      id: 'review', traceSeq: 5, sourceId: 'native-review', graphNamespace: [], runId: 'run-1',
      kind: 'tool_approval', toolCallIds: ['call-write'], status: 'pending', payloadOmitted: true,
      openedAt: '2026-08-28T00:00:04Z',
      agui: toolReviewInterrupts('native-review', [{ toolCallId: 'public-call-write', args: { file_path: '/result.txt' } }]),
    }]
    let conversation = restoreConversationFromTrace(snapshot, { model: 'main', includeTaskTrace: true })
    conversation = prepareResumeSubmission(conversation)
    for (const event of [
      { type: 'RUN_STARTED', threadId: snapshot.threadId, runId: 'run-resume' },
      { type: 'TOOL_CALL_RESULT', toolCallId: 'public-call-write', messageId: 'public-result', role: 'tool', content: 'written' },
      { type: 'TOOL_CALL_START', toolCallId: 'public-deliver', toolCallName: 'deliver_file' },
      { type: 'RUN_ERROR', message: '交付失败', code: 'tool_error' },
    ] as ConversationAgUiEvent[]) conversation = applyConversationEvent(conversation, event)
    const tools = conversation.messages.filter(message => message.role === 'tool')
    expect(tools).toHaveLength(2)
    expect(tools[0]).toMatchObject({ id: 'tool-node', content: 'write_file', meta: { status: 'completed', result: 'written', toolCallId: 'public-call-write' } })
    expect(tools[1]).toMatchObject({ content: 'deliver_file', meta: { status: 'failed' } })
    expect(conversation.runStatus).toBe('error')
    expect(snapshot.graph.nodes[0]?.sourceId).toBe('call-write')
  })

  it('助手续流以公开消息ID更新同一条历史消息，重复快照不复制消息', () => {
    const snapshot = detail()
    snapshot.messages[1] = { ...snapshot.messages[1]!, status: 'streaming', content: '部分' }
    let conversation = restoreConversationFromTrace(snapshot, { model: 'main', includeTaskTrace: true })
    conversation = applyConversationEvent(conversation, { type: 'TEXT_MESSAGE_CONTENT', messageId: 'public-assistant-1', delta: '完成' })
    const event: ConversationAgUiEvent = { type: 'MESSAGES_SNAPSHOT', messages: [{ id: 'public-assistant-1', role: 'assistant', content: '部分完成' }] }
    conversation = applyConversationEvent(applyConversationEvent(conversation, event), event)
    expect(conversation.messages.filter(message => message.role === 'assistant')).toHaveLength(1)
    expect(conversation.messages.find(message => message.role === 'assistant')?.content).toBe('部分完成')
    expect(conversation.trace?.messages[1]?.id).toBe('message:assistant-1')
  })
})
