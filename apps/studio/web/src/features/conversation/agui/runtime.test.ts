import type { JsonObject } from '../../../types'
import { planInterrupt } from '../../../test/aguiFixtures'
import { describe, expect, it } from 'vitest'
import mediaFixture from './contracts/message-attachments.fixture.json'
import checkpointReplay from '../../../../../../../packages/tinkerfin/tests/fixtures/checkpoint-message-replay.json'
import { parseConversationAgUiEvent } from '../../../api/conversation/eventParser'
import { messageAttachments, messageText } from '../attachments/content'

import type { ConversationAgUiEvent, InterruptEvent } from '../../../api/conversation/types'
import { buildEmptyConversation } from '../../../lib/workspace'
import type { ApprovalAllowedDecision, ApprovalItem, Conversation } from '../../../types'
import {
  applyConversationEvent,
  buildPlanResumePayload,
  buildResumePayload,
  markConversationDetached,
  planInteractionFromInterrupts,
  prepareResumeSubmission,
} from './runtime'

const THREAD_ID = 'thread-order-check'
const RUN_ID = 'run-order-check'

it('失败后新提问使用真实框架流，历史消息修复不重复追加旧正文', () => {
  const initial = buildEmptyConversation({ threadId: 'thread', now: '2026-09-13T00:00:00Z' })
  const failed = checkpointReplay.before.map(parseConversationAgUiEvent).reduce(applyConversationEvent, initial)
  const finished = checkpointReplay.after.map(parseConversationAgUiEvent).reduce(applyConversationEvent, failed)
  expect(finished.messages.filter(message => message.role === 'assistant').map(message => message.content)).toEqual([
    'The report is prepared.',
    'I will revise the delivery.',
  ])
  expect(finished.runStatus).toBe('idle')
})

it('真实工具输出在流式文字之后仍保留图片，快照可以替换附件', () => {
  const events = mediaFixture.events.map(parseConversationAgUiEvent)
  let conversation = events.reduce(applyConversationEvent, buildEmptyConversation({ now: "2026-09-07T00:00:00Z" }))
  const assistant = conversation.messages.find(message => message.role === 'assistant')
  expect(assistant?.content).toBe('Generated chart')
  expect(assistant?.attachments).toEqual([mediaFixture.attachment])
  expect(conversation.messages.find(message => message.role === 'tool')?.attachments).toEqual([mediaFixture.attachment])
  expect(messageAttachments(mediaFixture.toolContent)).toEqual([mediaFixture.attachment])
  expect(messageText(mediaFixture.toolContent)).toBe('')
  expect(assistant).toBeDefined()
  conversation = applyConversationEvent(conversation, {
    type: 'MESSAGES_SNAPSHOT',
    messages: [{ id: assistant!.id, role: 'assistant', content: 'Generated chart', attachments: [] }],
  })
  expect(conversation.messages.find(message => message.id === assistant!.id)?.attachments).toEqual([])
})

it('框架生成的历史快照保留工具和助手附件描述', () => {
  const initial = buildEmptyConversation({ now: '2026-09-07T00:00:00Z' })
  const live = mediaFixture.events.map(parseConversationAgUiEvent).reduce(applyConversationEvent, initial)
  const snapshots = mediaFixture.snapshots.map(parseConversationAgUiEvent)
  const replay = snapshots.reduce(applyConversationEvent, initial)
  for (const role of ['assistant', 'tool']) {
    const liveMessage = live.messages.find(message => message.role === role)
    const replayMessage = replay.messages.find(message => message.role === role)
    expect(replayMessage?.id).toEqual(liveMessage?.id)
    expect(replayMessage?.attachments).toEqual([mediaFixture.attachment])
    if (role === 'tool') expect(replayMessage?.meta?.result).toEqual(liveMessage?.meta?.result)
    else expect(replayMessage?.content).toEqual(liveMessage?.content)
  }
})

it('工具快照按工具调用 ID 替换附件并保留已有卡片的状态和来源', () => {
  const initial = buildEmptyConversation({ now: '2026-09-07T00:00:00Z' })
  const live = mediaFixture.events.map(parseConversationAgUiEvent).reduce(applyConversationEvent, initial)
  const tool = live.messages.find(message => message.role === 'tool')!
  const snapshot = structuredClone(mediaFixture.snapshots[0])
  const toolSnapshot = snapshot.messages.find(message => message.role === 'tool')!
  expect(toolSnapshot.id).not.toBe(tool.id)
  toolSnapshot.attachments = []
  const event = parseConversationAgUiEvent(snapshot)
  const replay = applyConversationEvent(applyConversationEvent(live, event), event)
  const updated = replay.messages.find(message => message.id === tool.id)!
  expect(updated.attachments).toEqual([])
  expect(updated.meta).toEqual(tool.meta)
  expect(updated.content).toEqual(tool.content)
  expect(replay.messages.length).toBe(live.messages.length)
})

it('工具快照必须提供稳定工具调用 ID 和合法附件', () => {
  for (const patch of [{ toolCallId: undefined }, { toolCallId: '' }, { attachments: [{}] }]) {
    const snapshot = structuredClone(mediaFixture.snapshots[0])
    const tool = snapshot.messages.find(message => message.role === 'tool')!
    Object.assign(tool, patch)
    expect(() => parseConversationAgUiEvent(snapshot)).toThrow()
  }
})

it('子智能体图片留在对应卡片中，重复附件事件不会复制图片', () => {
  const events = nativeContractEvents()
  let conversation = events.slice(0, 8).reduce(applyConversationEvent, buildEmptyConversation({ now: "2026-09-07T00:00:00Z" }))
  const subagent = conversation.messages.find(message => message.role === 'subagent')
  const sourceEvent = events.find(event => event.type === 'TOOL_CALL_START' && event.toolCallId === 'call-read')
  if (sourceEvent?.type !== 'TOOL_CALL_START' || !subagent) throw new Error('缺少子智能体场景')
  const event: ConversationAgUiEvent = {
    type: 'CUSTOM', name: 'tinkerfin.message.attachments',
    value: { messageId: 'child-picture', attachments: [mediaFixture.attachment] },
    rawEvent: sourceEvent.rawEvent,
  }
  conversation = applyConversationEvent(applyConversationEvent(conversation, event), event)
  expect(conversation.messages.find(message => message.id === subagent.id)?.attachments).toEqual([mediaFixture.attachment])
  expect(conversation.messages.some(message => message.id === 'child-picture')).toBe(false)
})

function nativeContractEvents(): ConversationAgUiEvent[] {
  const subRunId = 'subagent-11111111-1111-5111-8111-111111111111'
  const mainSource = { kind: 'root' as const, agentType: 'main' as const, agentName: 'main', graphNamespace: [] }
  const provenance = {
    schema: 'tinkerfin.subagent-provenance' as const,
    subagentInvocationId: subRunId,
    graphNamespace: ['tools:graph-research'],
    parentGraphNamespace: [],
    graphTaskId: 'graph-research',
    agentName: 'researcher',
    parentToolCallId: 'call-task',
    description: '研究百度与 Google',
    requestRunId: RUN_ID,
  }
  const subSource = {
    kind: 'deep_agent_subagent' as const,
    agentType: 'subagent' as const,
    agentName: 'researcher',
    graphNamespace: [...provenance.graphNamespace],
    parentGraphNamespace: [],
    graphTaskId: 'graph-research',
    parentToolCallId: 'call-task',
    subagentInput: provenance.description,
    subagentInvocationId: subRunId,
  }
  return [
    { type: 'RUN_STARTED', threadId: THREAD_ID, runId: RUN_ID },
    {
      type: 'TOOL_CALL_START',
      toolCallId: 'call-todos',
      toolCallName: 'write_todos',
      rawEvent: { streamMode: 'messages', source: mainSource, runId: RUN_ID },
    },
    {
      type: 'TOOL_CALL_RESULT',
      toolCallId: 'call-todos',
      messageId: 'message-todos',
      content: 'Updated todo list to three completed items',
      role: 'tool',
      rawEvent: { streamMode: 'messages', source: mainSource, runId: RUN_ID },
    },
    {
      type: 'STATE_SNAPSHOT',
      snapshot: {
        todos: [
          { content: '读取资料', status: 'completed' },
          { content: '研究百度', status: 'completed' },
          { content: '研究 Google', status: 'completed' },
        ],
      },
      rawEvent: { streamMode: 'values', source: mainSource, runId: RUN_ID },
    },
    {
      type: 'TOOL_CALL_START',
      toolCallId: 'call-task',
      toolCallName: 'task',
      rawEvent: { streamMode: 'messages', source: mainSource, runId: RUN_ID },
    },
    {
      type: 'TOOL_CALL_ARGS',
      toolCallId: 'call-task',
      delta: JSON.stringify({ description: '研究百度与 Google', subagent_type: 'researcher' }),
      rawEvent: { streamMode: 'messages', source: mainSource, runId: RUN_ID },
    },
    {
      type: 'RAW',
      source: 'langgraph.tasks',
      rawEvent: { type: 'tasks', phase: 'start', ns: [] },
      event: {
        data: { id: 'graph-research', name: 'tools' },
        provenance: {
          kind: 'root',
          graphNamespace: [],
          agentType: 'main',
          agentName: 'main',
          subagents: [provenance],
        },
      },
    },
    {
      type: 'TOOL_CALL_START',
      toolCallId: 'call-read',
      toolCallName: 'read_file',
      rawEvent: { streamMode: 'messages', source: subSource, runId: RUN_ID },
    },
    {
      type: 'TOOL_CALL_RESULT',
      toolCallId: 'call-read',
      messageId: 'message-read',
      content: '百度与 Google 调研资料',
      role: 'tool',
      rawEvent: { streamMode: 'messages', source: subSource, runId: RUN_ID },
    },
    {
      type: 'TOOL_CALL_RESULT',
      toolCallId: 'call-task',
      messageId: 'message-task',
      content: '百度与 Google 调研完成',
      role: 'tool',
      rawEvent: {
        streamMode: 'messages',
        source: mainSource,
        runId: RUN_ID,
        relatedSubagentInvocationId: subRunId,
      },
    },
    { type: 'TEXT_MESSAGE_START', messageId: 'message-final', role: 'assistant' },
    {
      type: 'TEXT_MESSAGE_CONTENT',
      messageId: 'message-final',
      delta: '已完成 **Google** 调研并写入 result1.txt',
    },
    { type: 'TEXT_MESSAGE_END', messageId: 'message-final' },
    { type: 'RUN_FINISHED', threadId: THREAD_ID, runId: RUN_ID, outcome: { type: 'success' } },
  ]
}

const planFromEnvelope = (id: string, envelope: JsonObject) => planInteractionFromInterrupts([planInterrupt(id, envelope)])

it('parses bounded time clarification and submits an RFC time answer', () => {
  const interaction = planFromEnvelope('plan-time', {
    schema: 'tinkerfin.runtime-interrupt',
    kind: 'tinkerfin:plan_clarification',
    message: 'Choose a time',
    responseSchema: {},
    metadata: {
      origin: 'plan',
      clarification: {
        form: {
          title: '确认时间',
          description: '用于安排执行窗口',
          questions: [{
            id: 'deployment-time',
            answerType: 'time',
            prompt: '何时执行？',
            required: true,
            timeZone: 'Asia/Shanghai',
            minimum: '09:00:00',
            maximum: '18:00:00',
          }],
        },
      },
    },
  })
  if (!interaction || interaction.kind !== 'questions') {
    throw new Error('测试夹具必须产生 Plan 时间澄清')
  }
  expect(interaction.questions[0]).toMatchObject({
    answerType: 'time',
    timeZone: 'Asia/Shanghai',
    minimum: '09:00',
    maximum: '18:00',
  })
  const answered = {
    ...interaction,
    questions: interaction.questions.map((question) => question.answerType === 'time'
      ? { ...question, time: '09:30' }
      : question),
  }
  const payload = buildPlanResumePayload({
    ...buildEmptyConversation({
      threadId: THREAD_ID,
      now: '2026-08-30T00:00:00.000Z',
      model: 'main',
    }),
    planInteraction: answered,
  })

  expect(payload.resume?.[0]?.payload).toEqual({
    type: 'respond',
    answers: {
      'deployment-time': {
        status: 'answered',
        answerType: 'time',
        time: '09:30:00',
      },
    },
  })
})

it('parses a zoned datetime clarification and submits one local minute', () => {
  const interaction = planFromEnvelope('plan-datetime', {
    schema: 'tinkerfin.runtime-interrupt',
    kind: 'tinkerfin:plan_clarification',
    responseSchema: {},
    metadata: {
      origin: 'plan',
      clarification: {
        form: {
          title: '确认执行时间',
          description: '用于安排唯一执行时间点',
          questions: [{
            id: 'deployment-at',
            answerType: 'datetime',
            prompt: '何时执行？',
            required: true,
            timeZone: 'Asia/Shanghai',
            minimum: '2026-08-30T09:00:00',
            maximum: '2026-09-30T18:00:00',
          }],
        },
      },
    },
  })
  if (!interaction || interaction.kind !== 'questions') {
    throw new Error('测试夹具必须产生 Plan 日期时间澄清')
  }
  expect(interaction.questions[0]).toMatchObject({
    answerType: 'datetime',
    timeZone: 'Asia/Shanghai',
    minimum: '2026-08-30T09:00',
    maximum: '2026-09-30T18:00',
  })
  const answered = {
    ...interaction,
    questions: interaction.questions.map((question) => question.answerType === 'datetime'
      ? { ...question, dateTime: '2026-08-30T09:30' }
      : question),
  }

  const payload = buildPlanResumePayload({
    ...buildEmptyConversation({
      threadId: THREAD_ID,
      now: '2026-08-30T00:00:00.000Z',
      model: 'main',
    }),
    planInteraction: answered,
  })

  expect(payload.resume?.[0]?.payload).toEqual({
    type: 'respond',
    answers: {
      'deployment-at': {
        status: 'answered',
        answerType: 'datetime',
        dateTime: '2026-08-30T09:30:00',
      },
    },
  })
})

it('uses each Plan review interrupt response Schema as its action authority', () => {
  const interaction = planFromEnvelope('plan-review-actions', {
    schema: 'tinkerfin.runtime-interrupt',
    kind: 'tinkerfin:plan_review',
    responseSchema: {
      discriminator: {
        propertyName: 'type',
        mapping: {
          approve: '#/$defs/ApprovePlan',
          reject: '#/$defs/RejectPlan',
          cancel: '#/$defs/CancelPlan',
        },
      },
    },
    metadata: {
      origin: 'plan',
      review: {
        draft: {
          revision: 1,
          contentSchema: {
            fingerprint: '0'.repeat(64),
            mediaType: 'text/markdown',
          },
          content: {
            description: 'Review one Plan',
            markdown: '# Plan',
          },
        },
      },
    },
  })

  expect(interaction).toMatchObject({
    kind: 'review',
    allowedActions: ['approve', 'reject', 'cancel'],
  })
})

it('keeps reject and cancel in Plan mode while only approval exits', () => {
  const conversation = buildEmptyConversation({
    threadId: THREAD_ID,
    now: '2026-08-30T00:00:00.000Z',
    model: 'main',
  })
  conversation.planInteraction = {
    kind: 'review',
    interruptId: 'plan-review-decision',
    revision: 2,
    allowedActions: ['approve', 'reject', 'cancel'],
    submitted: false,
    action: 'reject',
    message: 'Use a smaller scope',
    draft: {
      revision: 2,
      contentSchema: {
        fingerprint: '0'.repeat(64),
        mediaType: 'text/markdown',
      },
      content: {
        description: 'Review the current execution Plan',
        markdown: '# Original Plan',
      },
    },
  }

  const rejected = buildPlanResumePayload(conversation)

  expect(rejected.forwardedProps).toEqual({ model: 'main', command: { plan: 'on' } })
  expect(rejected.resume?.[0]).toEqual({
    interruptId: 'plan-review-decision',
    status: 'resolved',
    payload: {
      type: 'reject',
      baseRevision: 2,
      message: 'Use a smaller scope',
    },
  })
  if (!conversation.planInteraction || conversation.planInteraction.kind !== 'review') {
    throw new Error('测试夹具必须保留 Plan 审阅')
  }
  const review = conversation.planInteraction
  const cancelled = buildPlanResumePayload({
    ...conversation,
    planInteraction: {
      ...review,
      action: 'cancel',
      message: undefined,
    },
  })
  expect(cancelled.forwardedProps).toEqual({ model: 'main', command: { plan: 'on' } })
  expect(cancelled.resume?.[0]).toEqual({
    interruptId: 'plan-review-decision',
    status: 'resolved',
    payload: { type: 'cancel', baseRevision: 2 },
  })
  const approved = buildPlanResumePayload({
    ...conversation,
    planInteraction: { ...review, action: 'approve', message: undefined },
  })
  expect(approved.forwardedProps).toEqual({ model: 'main', command: { plan: 'off' } })
  expect(approved.resume?.[0]?.payload).toEqual({ type: 'approve', baseRevision: 2 })
})

function interrupt(
  overrides: Partial<InterruptEvent> & Pick<InterruptEvent, 'id'>,
): InterruptEvent {
  const originalArgs = {
    file_path: `${overrides.id}.txt`,
    content: overrides.id,
  }
  const allowedDecisions: ApprovalAllowedDecision[] = ['approve', 'edit', 'reject']
  return {
    id: overrides.id,
    reason: overrides.reason ?? 'tool_call',
    message: overrides.message ?? `审批 ${overrides.id}`,
    toolCallId: overrides.toolCallId ?? `scoped-tool:${overrides.id}`,
    responseSchema: overrides.responseSchema,
    metadata: overrides.metadata ?? {
      langgraphValue: {
        action_requests: [{ name: 'write_file', args: originalArgs }],
        review_configs: [{
          action_name: 'write_file',
          allowed_decisions: allowedDecisions,
        }],
      },
      deepagents: {
        schema: 'tinkerfin.deepagents.tool-review',
        nativeInterruptId: overrides.id,
        actionIndex: 0,
        toolName: 'write_file',
        allowedDecisions,
        originalArgs,
      },
    },
  }
}

describe('AG-UI runtime reducer', () => {
  it('uses server-owned RAW task identities for live subagent cards and child tools', () => {
    const subRunId = 'subagent-22222222-2222-5222-8222-222222222222'
    const mainSource = { kind: 'root' as const, agentType: 'main' as const, agentName: 'main', graphNamespace: [] }
    const subSource = {
      kind: 'deep_agent_subagent' as const,
      agentType: 'subagent' as const,
      agentName: 'researcher',
      graphNamespace: ['tools:graph-server'],
      graphTaskId: 'graph-server',
      parentGraphNamespace: [],
      parentToolCallId: 'call-task-server',
      subagentInput: '检索 LangGraph',
      subagentInvocationId: subRunId,
    }
    const events: ConversationAgUiEvent[] = [
      { type: 'RUN_STARTED', threadId: THREAD_ID, runId: RUN_ID },
      {
        type: 'TOOL_CALL_START',
        toolCallId: 'call-task-server',
        toolCallName: 'task',
        rawEvent: { streamMode: 'messages', source: mainSource, runId: RUN_ID },
      },
      {
        type: 'TOOL_CALL_ARGS',
        toolCallId: 'call-task-server',
        delta: JSON.stringify({
          description: '检索 LangGraph',
          subagent_type: 'researcher',
        }),
        rawEvent: { streamMode: 'messages', source: mainSource, runId: RUN_ID },
      },
      {
        type: 'RAW',
        source: 'langgraph.tasks',
        rawEvent: { type: 'tasks', phase: 'start', ns: [] },
        event: {
          data: { id: 'graph-server', name: 'tools' },
          provenance: {
            kind: 'root',
            graphNamespace: [],
            agentType: 'main',
            agentName: 'main',
            subagents: [{
              schema: 'tinkerfin.subagent-provenance',
              subagentInvocationId: subRunId,
              graphNamespace: ['tools:graph-server'],
              parentGraphNamespace: [],
              graphTaskId: 'graph-server',
              agentName: 'researcher',
              parentToolCallId: 'call-task-server',
              description: '检索 LangGraph',
              requestRunId: RUN_ID,
            }],
          },
        },
      },
      {
        type: 'TOOL_CALL_START',
        toolCallId: 'call-search-server',
        toolCallName: 'web_search',
        rawEvent: {
          streamMode: 'messages',
          source: subSource,
          runId: RUN_ID,
        },
      },
      {
        type: 'TOOL_CALL_RESULT',
        toolCallId: 'call-search-server',
        messageId: 'message-search-server',
        content: '搜索结果',
        role: 'tool',
        rawEvent: {
          streamMode: 'messages',
          source: subSource,
          runId: RUN_ID,
          toolResultStatus: 'success',
        },
      },
      {
        type: 'TOOL_CALL_RESULT',
        toolCallId: 'call-task-server',
        messageId: 'message-task-server',
        content: '子 Agent 完成',
        role: 'tool',
        rawEvent: {
          streamMode: 'messages',
          source: mainSource,
          runId: RUN_ID,
          relatedSubagentInvocationId: subRunId,
          toolResultStatus: 'success',
        },
      },
    ]
    const initial = buildEmptyConversation({
      threadId: THREAD_ID,
      model: 'main',
      now: '2026-08-18T00:00:00.000Z',
    })
    const current = events.reduce(applyConversationEvent, initial)
    const subagent = current.messages.find(
      (message) => message.role === 'subagent' && message.meta?.subRunId === subRunId,
    )
    const childTool = current.messages.find(
      (message) => message.meta?.toolCallId === 'call-search-server',
    )
    const task = current.messages.find(
      (message) => message.meta?.toolCallId === 'call-task-server',
    )

    expect(subagent?.meta).toMatchObject({
      agentName: 'researcher',
      input: '检索 LangGraph',
      result: '子 Agent 完成',
      status: 'completed',
      runId: subRunId,
      originMainRunId: RUN_ID,
      lastMainRunId: RUN_ID,
      graphTaskId: 'graph-server',
    })
    expect(childTool?.meta).toMatchObject({
      runId: subRunId,
      graphTaskId: 'graph-server',
      sourceAgentName: 'researcher',
      status: 'completed',
    })
    expect(childTool?.id).toBe('call-search-server')
    expect(task?.id).toBe('call-task-server')
    expect(task?.meta?.subRunId).toBe(subRunId)
  })

  it('keeps one subagent card while a resumed descriptor updates its current main run', () => {
    const subRunId = 'subagent-cccccccc-cccc-5ccc-8ccc-cccccccccccc'
    const graphTaskId = 'graph-resume'
    const parentToolCallId = 'call-task-resume'
    const descriptor = (requestRunId: string) => ({
      schema: 'tinkerfin.subagent-provenance' as const,
      subagentInvocationId: subRunId,
      graphNamespace: [`tools:${graphTaskId}`],
      parentGraphNamespace: [],
      graphTaskId,
      agentName: 'researcher',
      parentToolCallId,
      description: '继续研究',
      requestRunId,
    })
    const rawStart = (requestRunId: string): ConversationAgUiEvent => ({
      type: 'RAW',
      source: 'langgraph.tasks',
      rawEvent: { type: 'tasks', phase: 'start', ns: [] },
      event: {
        data: { id: graphTaskId, name: 'tools' },
        provenance: {
          kind: 'root',
          graphNamespace: [],
          agentType: 'main',
          agentName: 'main',
          subagents: [descriptor(requestRunId)],
        },
      },
    })
    let current = buildEmptyConversation({
      threadId: THREAD_ID,
      model: 'main',
      now: '2026-08-18T00:00:00.000Z',
    })
    for (const event of [
      { type: 'RUN_STARTED', threadId: THREAD_ID, runId: 'run-origin' },
      {
        type: 'TOOL_CALL_START',
        toolCallId: parentToolCallId,
        toolCallName: 'task',
      },
      rawStart('run-origin'),
      { type: 'RUN_STARTED', threadId: THREAD_ID, runId: 'run-resume' },
      rawStart('run-resume'),
      {
        type: 'TOOL_CALL_RESULT',
        toolCallId: parentToolCallId,
        messageId: 'task-resume-result',
        content: '完成',
        role: 'tool',
        rawEvent: {
          streamMode: 'messages',
          source: { kind: 'root', agentType: 'main', agentName: 'main', graphNamespace: [] },
          runId: 'run-resume',
          relatedSubagentInvocationId: subRunId,
          toolResultStatus: 'success',
        },
      },
    ] as ConversationAgUiEvent[]) current = applyConversationEvent(current, event)

    const subagents = current.messages.filter((message) => message.role === 'subagent')
    expect(subagents).toHaveLength(1)
    expect(subagents[0].meta).toMatchObject({
      subRunId,
      originMainRunId: 'run-origin',
      lastMainRunId: 'run-resume',
      status: 'completed',
      result: '完成',
    })
  })

  it('isolates parallel server subruns that share one graph task id', () => {
    const mainSource = { kind: 'root' as const, agentType: 'main' as const, agentName: 'main', graphNamespace: [] }
    const graphTaskId = 'shared-graph-task'
    const invocationIds = {
      a: 'subagent-77777777-7777-5777-8777-777777777777',
      b: 'subagent-88888888-8888-5888-8888-888888888888',
    } as const
    const descriptors = (['a', 'b'] as const).map((suffix) => ({
      schema: 'tinkerfin.subagent-provenance' as const,
      subagentInvocationId: invocationIds[suffix],
      graphNamespace: [`tools:${graphTaskId}:${suffix}`],
      parentGraphNamespace: [],
      graphTaskId,
      agentName: 'researcher',
      parentToolCallId: `call-task-${suffix}`,
      description: `研究任务 ${suffix.toUpperCase()}`,
      requestRunId: RUN_ID,
    }))
    const rawStart: ConversationAgUiEvent = {
      type: 'RAW',
      source: 'langgraph.tasks',
      rawEvent: { type: 'tasks', phase: 'start', ns: [] },
      event: {
        data: { id: graphTaskId, name: 'tools' },
        provenance: {
          kind: 'root',
          graphNamespace: [],
          agentType: 'main',
          agentName: 'main',
          subagents: descriptors,
        },
      },
    }
    const initial = [
      { type: 'RUN_STARTED', threadId: THREAD_ID, runId: RUN_ID } as ConversationAgUiEvent,
      {
        type: 'TOOL_CALL_START',
        toolCallId: 'call-task-a',
        toolCallName: 'task',
        rawEvent: { streamMode: 'messages', source: mainSource, runId: RUN_ID },
      } as ConversationAgUiEvent,
      {
        type: 'TOOL_CALL_START',
        toolCallId: 'call-task-b',
        toolCallName: 'task',
        rawEvent: { streamMode: 'messages', source: mainSource, runId: RUN_ID },
      } as ConversationAgUiEvent,
      rawStart,
    ].reduce(
      applyConversationEvent,
      buildEmptyConversation({
        threadId: THREAD_ID,
        model: 'main',
        now: '2026-08-18T00:00:00.000Z',
      }),
    )
    const withText = descriptors.flatMap<ConversationAgUiEvent>((descriptor) => {
      const source = {
        kind: 'deep_agent_subagent' as const,
        agentType: 'subagent' as const,
        agentName: descriptor.agentName,
        graphNamespace: descriptor.graphNamespace,
        graphTaskId,
        parentGraphNamespace: [],
        parentToolCallId: descriptor.parentToolCallId,
        subagentInput: descriptor.description,
        subagentInvocationId: descriptor.subagentInvocationId,
      }
      return [
        {
          type: 'TEXT_MESSAGE_START',
          messageId: `message-${descriptor.subagentInvocationId}`,
          role: 'assistant',
          rawEvent: {
            streamMode: 'messages',
            source,
            runId: RUN_ID,
          },
        },
        {
          type: 'TEXT_MESSAGE_CONTENT',
          messageId: `message-${descriptor.subagentInvocationId}`,
          delta: `结果 ${descriptor.subagentInvocationId}`,
          rawEvent: {
            streamMode: 'messages',
            source,
            runId: RUN_ID,
          },
        },
      ]
    }).reduce(applyConversationEvent, initial)
    const subagents = withText.messages.filter((message) => message.role === 'subagent')

    expect(subagents).toHaveLength(2)
    expect(subagents.map((message) => [message.meta?.subRunId, message.meta?.result])).toEqual([
      [invocationIds.a, `结果 ${invocationIds.a}`],
      [invocationIds.b, `结果 ${invocationIds.b}`],
    ])
  })

  it('fails a discovered subrun and its child tools when the main run errors', () => {
    const subRunId = 'subagent-33333333-3333-5333-8333-333333333333'
    const mainSource = { kind: 'root' as const, agentType: 'main' as const, agentName: 'main', graphNamespace: [] }
    const subSource = {
      kind: 'deep_agent_subagent' as const,
      agentType: 'subagent' as const,
      agentName: 'researcher',
      graphNamespace: ['tools:graph-main-error'],
      graphTaskId: 'graph-main-error',
      parentGraphNamespace: [],
      parentToolCallId: 'call-task-main-error',
      subagentInput: '执行研究',
      subagentInvocationId: subRunId,
    }
    const events: ConversationAgUiEvent[] = [
      { type: 'RUN_STARTED', threadId: THREAD_ID, runId: RUN_ID },
      {
        type: 'TOOL_CALL_START',
        toolCallId: 'call-task-main-error',
        toolCallName: 'task',
        rawEvent: { streamMode: 'messages', source: mainSource, runId: RUN_ID },
      },
      {
        type: 'RAW',
        source: 'langgraph.tasks',
        rawEvent: { type: 'tasks', phase: 'start', ns: [] },
        event: {
          data: { id: 'graph-main-error', name: 'tools' },
          provenance: {
            kind: 'root',
            graphNamespace: [],
            agentType: 'main',
            agentName: 'main',
            subagents: [{
              schema: 'tinkerfin.subagent-provenance',
              subagentInvocationId: subRunId,
              graphNamespace: subSource.graphNamespace,
              parentGraphNamespace: [],
              graphTaskId: subSource.graphTaskId,
              agentName: subSource.agentName,
              parentToolCallId: 'call-task-main-error',
              description: '执行研究',
              requestRunId: RUN_ID,
            }],
          },
        },
      },
      {
        type: 'TOOL_CALL_START',
        toolCallId: 'call-child-main-error',
        toolCallName: 'web_search',
        rawEvent: {
          streamMode: 'messages',
          source: subSource,
          runId: RUN_ID,
        },
      },
      {
        type: 'RUN_ERROR',
        message: '主 run 失败',
        code: 'runtime_error',
        rawEvent: { runId: RUN_ID },
      },
    ]
    const current = events.reduce(
      applyConversationEvent,
      buildEmptyConversation({
        threadId: THREAD_ID,
        model: 'main',
        now: '2026-08-18T00:00:00.000Z',
      }),
    )
    const subagent = current.messages.find(
      (message) => message.role === 'subagent' && message.meta?.subRunId === subRunId,
    )
    const childTool = current.messages.find(
      (message) => message.meta?.toolCallId === 'call-child-main-error',
    )

    expect(current.runStatus).toBe('error')
    expect(current.notice).toBeUndefined()
    expect(subagent?.meta?.status).toBe('failed')
    expect(childTool?.meta?.status).toBe('failed')
  })

  it('treats a standard main RUN_ERROR rawEvent containing only runId as main scope', () => {
    const conversation = {
      ...buildEmptyConversation({
        threadId: THREAD_ID,
        now: '2026-08-18T10:00:00.000Z',
        model: 'main',
      }),
      runStatus: 'streaming' as const,
      activeRunId: RUN_ID,
      messages: [{
        id: 'tool-running',
        role: 'tool' as const,
        content: 'web_search',
        createdAt: '2026-08-18T10:00:01.000Z',
        meta: {
          toolName: 'web_search',
          toolCallId: 'tool-running',
          status: 'running' as const,
          runId: RUN_ID,
        },
      }],
      todos: [{ id: 'todo-running', content: '正在执行', status: 'running' as const }],
    }

    const next = applyConversationEvent(conversation, {
      type: 'RUN_ERROR',
      rawEvent: { runId: RUN_ID },
      code: 'cancelled',
      message: '聊天生成已取消',
    })

    expect(next.runStatus).toBe('idle')
    expect(next.activeRunId).toBeUndefined()
    expect(next.notice).toBeUndefined()
    expect(next.messages).toHaveLength(1)
    expect(next.messages.some((message) => message.id.includes('client-notice'))).toBe(false)
    expect(next.messages[0]?.meta).toMatchObject({
      status: 'cancelled',
      result: '任务已停止',
    })
    expect(next.todos).toEqual([{ id: 'todo-running', content: '正在执行', status: 'cancelled' }])
  })

  it('stores a detached connection notice outside protocol messages', () => {
    const conversation = {
      ...buildEmptyConversation({
        threadId: THREAD_ID,
        now: '2026-08-18T10:00:00.000Z',
        model: 'main',
      }),
      runStatus: 'streaming' as const,
      activeRunId: RUN_ID,
    }

    const detached = markConversationDetached(conversation, '实时连接已断开')

    expect(detached.notice).toMatchObject({
      kind: 'info',
      content: '实时连接已断开',
    })
    expect(detached.activeRunId).toBe(RUN_ID)
    expect(detached.messages).toEqual([])
  })

  it('keeps a submitted approval claimed while its resumed stream is detached', () => {
    const conversation: Conversation = {
      ...buildEmptyConversation({
        threadId: THREAD_ID,
        now: '2026-08-18T10:00:00.000Z',
        model: 'main',
      }),
      runStatus: 'streaming',
      activeRunId: RUN_ID,
      approval: {
        activeIndex: 0,
        submitted: true,
        items: [{
          id: 'approval-detached',
          interruptId: 'interrupt-detached',
          toolCallId: 'tool-detached',
          toolName: 'write_file',
          params: '{}',
          input: '/detached.txt',
          description: '确认写入',
          originalArgs: {},
          allowedDecisions: ['approve', 'reject'],
          decision: 'approved',
        }],
      },
    }

    const detached = markConversationDetached(conversation, '实时连接已断开')

    expect(detached.runStatus).toBe('detached')
    expect(detached.approval?.submitted).toBe(true)
    expect(() => buildResumePayload(detached)).toThrow('approval_stale')
  })

  it('replaces draft identity and title without duplicating Graph input', () => {
    const draft = buildEmptyConversation({
      now: '2026-08-18T10:00:00.000Z',
      model: 'main',
    })

    const next = applyConversationEvent(draft, {
      type: 'RUN_STARTED',
      threadId: 'thread-from-server',
      runId: 'run-from-client',
      parentRunId: 'run-parent',
      title: '服务端生成的标题',
    })

    expect(next.threadId).toBe('thread-from-server')
    expect(next.activeRunId).toBe('run-from-client')
    expect(next.title).toBe('服务端生成的标题')
    expect(next.messages).toEqual([])
  })

  it('keeps the optimistic user message until an authoritative history snapshot arrives', () => {
    const draft = {
      ...buildEmptyConversation({
        now: '2026-08-18T10:00:00.000Z',
        model: 'main',
      }),
      messages: [{
        id: 'request-run-from-client',
        role: 'user' as const,
        content: '分析本季度现金流',
        createdAt: '2026-08-18T10:00:00.000Z',
        meta: { runId: 'run-from-client' },
      }],
    }

    const next = applyConversationEvent(draft, {
      type: 'RUN_STARTED',
      threadId: 'thread-from-server',
      runId: 'run-from-client',
      title: '服务端生成的标题',
    })

    expect(next.messages).toEqual([{
      id: 'request-run-from-client',
      role: 'user',
      content: '分析本季度现金流',
      createdAt: '2026-08-18T10:00:00.000Z',
      meta: { runId: 'run-from-client' },
    }])
  })

  it('uses values as Todo truth while the standard tool result completes the tool card', () => {
    const initial = buildEmptyConversation({
      threadId: 'thread-write-todos-end',
      now: '2026-08-05T00:00:00.000Z',
      model: 'GPT-5.5',
    })

    const afterStart = applyConversationEvent(initial, {
      type: 'TOOL_CALL_START',
      rawEvent: {
        streamMode: 'messages',
        source: { kind: 'root', agentType: 'main', agentName: 'main', graphNamespace: [] },
        langgraphNode: 'model',
      },
      toolCallId: 'call-write-todos-test',
      toolCallName: 'write_todos',
      parentMessageId: 'parent-message',
    })

    const afterArgs = applyConversationEvent(afterStart, {
      type: 'TOOL_CALL_ARGS',
      rawEvent: {
        streamMode: 'messages',
        source: { kind: 'root', agentType: 'main', agentName: 'main', graphNamespace: [] },
        langgraphNode: 'model',
      },
      toolCallId: 'call-write-todos-test',
      delta: '{"todos":[{"content":"读取 url.json","status":"completed"},{"content":"写入 result.txt","status":"in_progress"}]}',
    })

    const afterEnd = applyConversationEvent(afterArgs, {
      type: 'TOOL_CALL_END',
      rawEvent: {
        streamMode: 'messages',
        source: { kind: 'root', agentType: 'main', agentName: 'main', graphNamespace: [] },
      },
      toolCallId: 'call-write-todos-test',
    })

    const message = afterEnd.messages.find(
      (item) => item.role === 'tool' && item.meta?.toolCallId === 'call-write-todos-test',
    )

    expect(message?.meta?.status).toBe('running')
    expect(afterEnd.todos).toEqual([])

    const rawContent = "Updated todo list to [{'content': '读取 url.json', 'status': 'completed'}]"
    const afterResult = applyConversationEvent(afterEnd, {
      type: 'TOOL_CALL_RESULT',
      toolCallId: 'call-write-todos-test',
      messageId: 'tool-message-write-todos-test',
      content: rawContent,
      role: 'tool',
    })
    const afterState = applyConversationEvent(afterResult, {
      type: 'STATE_SNAPSHOT',
      snapshot: {
        tinkerfin_plan: {
          effectiveMode: 'plan',
        },
        todos: [
          { content: '读取 url.json', status: 'completed' },
          { content: '写入 result.txt', status: 'in_progress' },
        ],
      },
    })
    const afterDelta = applyConversationEvent(afterState, {
      type: 'STATE_DELTA',
      delta: [
        { op: 'replace', path: '/todos/1/status', value: 'completed' },
        { op: 'replace', path: '/tinkerfin_plan/effectiveMode', value: 'default' },
      ],
    })
    const completedTool = afterDelta.messages.find(
      (item) => item.role === 'tool' && item.meta?.toolCallId === 'call-write-todos-test',
    )

    expect(completedTool?.meta?.status).toBe('completed')
    expect(completedTool?.meta?.result).toBe(rawContent)
    expect(afterState.mode).toBe('plan')
    expect(afterDelta.mode).toBe('default')
    expect(afterDelta.todos.map((todo) => todo.status)).toEqual(['completed', 'completed'])
  })

  it('pauses every unresolved tool in the interrupted main run without inventing interrupt bindings', () => {
    let current = applyConversationEvent(
      buildEmptyConversation({
        threadId: THREAD_ID,
        now: '2026-08-05T00:00:00.000Z',
        model: 'GPT-5.5',
      }),
      { type: 'RUN_STARTED', threadId: THREAD_ID, runId: RUN_ID },
    )
    const startTool = (toolCallId: string, toolCallName: string, runId: string) => {
      current = applyConversationEvent(current, {
        type: 'TOOL_CALL_START',
        toolCallId,
        toolCallName,
        parentMessageId: 'assistant-mixed-batch',
        rawEvent: {
          streamMode: 'messages',
          source: { kind: 'root', agentType: 'main', agentName: 'main', graphNamespace: [] },
          runId,
        },
      })
    }

    startTool('call-write-todos-paused', 'write_todos', RUN_ID)
    startTool('call-write-file-paused', 'write_file', RUN_ID)
    startTool('call-other-run', 'read_file', 'run-other')

    const interrupted = applyConversationEvent(current, {
      type: 'RUN_FINISHED',
      threadId: THREAD_ID,
      runId: RUN_ID,
      outcome: {
        type: 'interrupt',
        interrupts: [
          interrupt({
            id: 'interrupt-write-file',
            toolCallId: 'call-write-file-paused',
          }),
        ],
      },
    })
    const toolByCallId = new Map(
      interrupted.messages
        .filter((message) => message.role === 'tool' && message.meta?.toolCallId)
        .map((message) => [message.meta?.toolCallId, message]),
    )

    expect(toolByCallId.get('call-write-todos-paused')?.meta?.status).toBe('paused')
    expect(toolByCallId.get('call-write-todos-paused')?.meta?.interruptId).toBeUndefined()
    expect(toolByCallId.get('call-write-file-paused')?.meta?.status).toBe('paused')
    expect(toolByCallId.get('call-write-file-paused')?.meta?.interruptId).toBe(
      'interrupt-write-file',
    )
    expect(toolByCallId.get('call-other-run')?.meta?.status).toBe('running')
  })

  it('marks ToolMessage status error as a failed tool card', () => {
    const initial = buildEmptyConversation({
      threadId: 'thread-tool-error',
      now: '2026-08-05T00:00:00.000Z',
      model: 'GPT-5.5',
    })
    const started = applyConversationEvent(initial, {
      type: 'TOOL_CALL_START',
      toolCallId: 'call-read-error',
      toolCallName: 'read_file',
      rawEvent: {
        streamMode: 'messages',
        source: { kind: 'root', agentType: 'main', agentName: 'main', graphNamespace: [] },
        runId: RUN_ID,
      },
    })

    const failed = applyConversationEvent(started, {
      type: 'TOOL_CALL_RESULT',
      messageId: 'tool-message-error',
      toolCallId: 'call-read-error',
      content: 'file missing',
      role: 'tool',
      rawEvent: {
        streamMode: 'messages',
        source: { kind: 'root', agentType: 'main', agentName: 'main', graphNamespace: [] },
        runId: RUN_ID,
        toolResultStatus: 'error',
      },
    })

    expect(failed.messages.find((message) => message.meta?.toolCallId === 'call-read-error')?.meta?.status).toBe('failed')
  })

  it('ignores the complete reasoning lifecycle for main and subagent runs', () => {
    const initial = buildEmptyConversation({
      threadId: 'thread-reasoning-hidden',
      now: '2026-08-05T00:00:00.000Z',
      model: 'GPT-5.5',
    })
    const mainRawEvent = {
      streamMode: 'messages' as const,
      source: { kind: 'root' as const, agentType: 'main' as const, agentName: 'main', graphNamespace: [] },
      runId: RUN_ID,
    }
    const mainReasoning: ConversationAgUiEvent[] = [
      { type: 'REASONING_START', rawEvent: mainRawEvent, messageId: 'reasoning-main' },
      { type: 'REASONING_MESSAGE_START', rawEvent: mainRawEvent, messageId: 'reasoning-message-main', role: 'reasoning' },
      { type: 'REASONING_MESSAGE_CONTENT', rawEvent: mainRawEvent, messageId: 'reasoning-message-main', delta: '内部推理' },
      { type: 'REASONING_MESSAGE_END', rawEvent: mainRawEvent, messageId: 'reasoning-message-main' },
      { type: 'REASONING_END', rawEvent: mainRawEvent, messageId: 'reasoning-main' },
    ]

    const afterMainReasoning = mainReasoning.reduce(applyConversationEvent, initial)
    expect(afterMainReasoning).toBe(initial)
    expect(afterMainReasoning.messages).toHaveLength(0)

    const subRunId = 'subagent-44444444-4444-5444-8444-444444444444'
    const withSubagent: Conversation = {
      ...initial,
      messages: [{
        id: subRunId,
        role: 'subagent',
        content: '研究任务',
        createdAt: '2026-08-18T00:00:00.000Z',
        meta: {
          agentName: 'researcher',
          input: '研究任务',
          result: '',
          status: 'running',
          subRunId,
          runId: subRunId,
          originMainRunId: RUN_ID,
          lastMainRunId: RUN_ID,
          graphTaskId: 'graph-reasoning',
        },
      }],
    }
    const subRawEvent = {
      streamMode: 'messages' as const,
      source: {
        kind: 'deep_agent_subagent' as const,
        agentType: 'subagent' as const,
        agentName: 'researcher',
        graphNamespace: ['tools:graph-reasoning'],
        graphTaskId: 'graph-reasoning',
        subagentInvocationId: subRunId,
      },
      runId: RUN_ID,
    }
    const subReasoning: ConversationAgUiEvent[] = [
      { type: 'REASONING_START', rawEvent: subRawEvent, messageId: 'reasoning-sub' },
      { type: 'REASONING_MESSAGE_START', rawEvent: subRawEvent, messageId: 'reasoning-message-sub', role: 'reasoning' },
      { type: 'REASONING_MESSAGE_CONTENT', rawEvent: subRawEvent, messageId: 'reasoning-message-sub', delta: '子智能体内部推理' },
      { type: 'REASONING_MESSAGE_END', rawEvent: subRawEvent, messageId: 'reasoning-message-sub' },
      { type: 'REASONING_END', rawEvent: subRawEvent, messageId: 'reasoning-sub' },
    ]

    expect(subReasoning.reduce(applyConversationEvent, withSubagent)).toBe(withSubagent)
  })

  it('replays the messages/tasks/values contract fixture without losing state or hierarchy', () => {
    const initial = buildEmptyConversation({
      threadId: 'thread-real-stream',
      now: '2026-08-05T00:00:00.000Z',
      model: 'GPT-5.5',
    })

    const sampleEvents = nativeContractEvents()
    const finalConversation = sampleEvents.reduce(applyConversationEvent, initial)
    const expectedThreadId = sampleEvents.find(
      (event): event is Extract<ConversationAgUiEvent, { type: 'RUN_STARTED' }> => event.type === 'RUN_STARTED',
    )?.threadId
    const writeTodoMessages = finalConversation.messages.filter(
      (message) => message.role === 'tool' && message.meta?.toolName === 'write_todos',
    )
    const taskMessages = finalConversation.messages.filter(
      (message) => message.role === 'tool' && message.meta?.toolName === 'task',
    )
    const assistantMessages = finalConversation.messages.filter(
      (message) => message.role === 'assistant',
    )
    const subagentMessages = finalConversation.messages.filter(
      (message) => message.role === 'subagent',
    )
    const subagentTools = finalConversation.messages.filter(
      (message) => message.role === 'tool' && message.meta?.sourceAgentName === 'researcher',
    )
    const finalAssistant = assistantMessages.at(-1)
    const delegatedResults = taskMessages.map((message) => message.meta?.result ?? '').join('\n')
    const subagentResults = subagentMessages.map((message) => message.meta?.result ?? '').join('\n')
    const subRunIds = new Set(subagentMessages.map((message) => message.meta?.subRunId))

    expect(finalConversation.threadId).toBe(expectedThreadId)
    expect(finalConversation.runStatus).toBe('idle')
    expect(finalConversation.approval).toBeUndefined()
    expect(finalConversation.todos).toHaveLength(3)
    expect(finalConversation.todos.every((todo) => todo.status === 'completed')).toBe(true)
    expect(writeTodoMessages).toHaveLength(1)
    expect(writeTodoMessages.every((message) => message.meta?.result?.includes('Updated todo list to'))).toBe(true)
    expect(taskMessages.length).toBeGreaterThan(0)
    expect(taskMessages.every((message) => message.meta?.agentName === 'researcher')).toBe(true)
    expect(delegatedResults).toContain('百度')
    expect(delegatedResults).toMatch(/Google|谷歌/)
    expect(subagentMessages).toHaveLength(taskMessages.length)
    expect(subagentMessages.every((message) => message.meta?.agentName === 'researcher')).toBe(true)
    expect(subagentResults).toContain('百度')
    expect(subagentResults).toMatch(/Google|谷歌/)
    expect(subagentMessages.every((message) => message.meta?.reasoning === undefined)).toBe(true)
    expect(subagentTools.length).toBeGreaterThan(0)
    expect(subagentTools.every((message) => subRunIds.has(message.meta?.runId))).toBe(true)
    expect(finalAssistant?.content).toMatch(/Google|谷歌|google\.com/i)
    expect(finalAssistant?.content).toContain('result1.txt')
  })

  it('keeps parallel same-type subagents isolated when task results finish in reverse order', () => {
    let current = buildEmptyConversation({
      threadId: 'thread-parallel-subagents',
      now: '2026-08-05T00:00:00.000Z',
      model: 'GPT-5.5',
    })
    const apply = (event: ConversationAgUiEvent) => {
      current = applyConversationEvent(current, event)
    }
    const mainSource = { kind: 'root' as const, agentType: 'main' as const, agentName: 'main', graphNamespace: [] }
    const subRunIds = {
      a: 'subagent-aaaaaaaa-aaaa-5aaa-8aaa-aaaaaaaaaaaa',
      b: 'subagent-bbbbbbbb-bbbb-5bbb-8bbb-bbbbbbbbbbbb',
    } as const
    const subSource = (suffix: 'a' | 'b') => ({
      kind: 'deep_agent_subagent' as const,
      agentType: 'subagent' as const,
      agentName: 'researcher',
      graphNamespace: [`tools:graph-${suffix}`],
      parentGraphNamespace: [],
      graphTaskId: `graph-${suffix}`,
      parentToolCallId: `task-${suffix}`,
      subagentInput: `研究任务 ${suffix.toUpperCase()}`,
      subagentInvocationId: subRunIds[suffix],
    })

    apply({ type: 'RUN_STARTED', threadId: THREAD_ID, runId: RUN_ID })
    for (const suffix of ['a', 'b']) {
      apply({
        type: 'TOOL_CALL_START',
        rawEvent: { streamMode: 'messages', source: mainSource, runId: RUN_ID },
        toolCallId: `task-${suffix}`,
        toolCallName: 'task',
        parentMessageId: 'task-parent',
      })
      apply({
        type: 'TOOL_CALL_ARGS',
        rawEvent: { streamMode: 'messages', source: mainSource, runId: RUN_ID },
        toolCallId: `task-${suffix}`,
        delta: JSON.stringify({
          description: `研究任务 ${suffix.toUpperCase()}`,
          subagent_type: 'researcher',
        }),
      })
    }

    for (const suffix of ['a', 'b'] as const) {
      const graphTaskId = `graph-${suffix}`
      const subRunId = subRunIds[suffix]
      apply({
        type: 'RAW',
        source: 'langgraph.tasks',
        rawEvent: { type: 'tasks', phase: 'start', ns: [] },
        event: {
          data: { id: graphTaskId, name: 'tools' },
          provenance: {
            kind: 'root',
            graphNamespace: [],
            agentType: 'main',
            agentName: 'main',
            subagents: [{
              schema: 'tinkerfin.subagent-provenance',
              subagentInvocationId: subRunId,
              graphNamespace: [`tools:${graphTaskId}`],
              parentGraphNamespace: [],
              graphTaskId,
              agentName: 'researcher',
              parentToolCallId: `task-${suffix}`,
              description: `研究任务 ${suffix.toUpperCase()}`,
              requestRunId: RUN_ID,
            }],
          },
        },
      })
      apply({
        type: 'TOOL_CALL_START',
        rawEvent: {
          streamMode: 'messages',
          source: subSource(suffix),
          runId: RUN_ID,
        },
        toolCallId: `child-tool-${suffix}`,
        toolCallName: 'web_search',
        parentMessageId: `child-message-${suffix}`,
      })
    }

    const runningSubagents = current.messages.filter((message) => message.role === 'subagent')
    const runningSubagentA = runningSubagents.find((message) => message.meta?.subRunId === subRunIds.a)
    const runningSubagentB = runningSubagents.find((message) => message.meta?.subRunId === subRunIds.b)
    const runningTaskA = current.messages.find((message) => message.meta?.toolCallId === 'task-a')
    const runningTaskB = current.messages.find((message) => message.meta?.toolCallId === 'task-b')

    expect(runningSubagentA?.meta?.input).toBe('研究任务 A')
    expect(runningSubagentA?.meta?.toolCallId).toBe('task-a')
    expect(runningSubagentB?.meta?.input).toBe('研究任务 B')
    expect(runningSubagentB?.meta?.toolCallId).toBe('task-b')
    expect(runningTaskA?.meta?.subRunId).toBe(subRunIds.a)
    expect(runningTaskB?.meta?.subRunId).toBe(subRunIds.b)

    for (const suffix of ['b', 'a'] as const) {
      apply({
        type: 'TOOL_CALL_RESULT',
        rawEvent: {
          streamMode: 'messages',
          source: mainSource,
          runId: RUN_ID,
          relatedSubagentInvocationId: subRunIds[suffix],
        },
        messageId: `task-result-${suffix}`,
        toolCallId: `task-${suffix}`,
        content: `最终结果 ${suffix.toUpperCase()}`,
        role: 'tool',
      })
    }

    const subagents = current.messages.filter((message) => message.role === 'subagent')
    const subagentA = subagents.find((message) => message.meta?.subRunId === subRunIds.a)
    const subagentB = subagents.find((message) => message.meta?.subRunId === subRunIds.b)
    const childToolA = current.messages.find((message) => message.meta?.toolCallId === 'child-tool-a')
    const childToolB = current.messages.find((message) => message.meta?.toolCallId === 'child-tool-b')

    expect(subagents).toHaveLength(2)
    expect(subagentA?.meta?.input).toBe('研究任务 A')
    expect(subagentA?.meta?.result).toBe('最终结果 A')
    expect(subagentB?.meta?.input).toBe('研究任务 B')
    expect(subagentB?.meta?.result).toBe('最终结果 B')
    expect(childToolA?.meta?.runId).toBe(subagentA?.meta?.subRunId)
    expect(childToolB?.meta?.runId).toBe(subagentB?.meta?.subRunId)
  })

  it('keeps interrupt order stable when preparing multi-item resume payloads', () => {
    const interrupted = applyConversationEvent(
      buildEmptyConversation({
        threadId: 'thread-multi-interrupt',
        now: '2026-08-05T00:00:00.000Z',
        model: 'GPT-5.5',
      }),
      {
        type: 'RUN_FINISHED',
        threadId: THREAD_ID,
        runId: RUN_ID,
        outcome: {
          type: 'interrupt',
          interrupts: [
            interrupt({ id: 'interrupt-b', toolCallId: 'tool-b' }),
            interrupt({ id: 'interrupt-a#0', toolCallId: 'tool-a-0' }),
            interrupt({ id: 'interrupt-a#1', toolCallId: 'tool-a-1' }),
          ],
        },
      },
    )

    const approval = interrupted.approval
    expect(approval?.items.map((item) => item.interruptId)).toEqual([
      'interrupt-b',
      'interrupt-a#0',
      'interrupt-a#1',
    ])

    const items = (approval?.items ?? []).map<ApprovalItem>((item) => {
      if (item.interruptId === 'interrupt-b') {
        return { ...item, decision: 'approved' }
      }
      if (item.interruptId === 'interrupt-a#0') {
        return {
          ...item,
          decision: 'approved',
        }
      }
      return { ...item, decision: 'rejected', rejectionReason: 'skip' }
    })

    const payload = buildResumePayload({
      ...interrupted,
      threadId: THREAD_ID,
      approval: approval ? { ...approval, items } : approval,
    })

    expect(payload.forwardedProps).toEqual({ model: 'GPT-5.5', command: { plan: 'off' } })
    expect(payload.resume?.map((entry) => entry.interruptId)).toEqual([
      'interrupt-b',
      'interrupt-a#0',
      'interrupt-a#1',
    ])
    expect(payload.resume).toEqual([
      {
        interruptId: 'interrupt-b',
        status: 'resolved',
        payload: { type: 'approve' },
      },
      {
        interruptId: 'interrupt-a#0',
        status: 'resolved',
        payload: { type: 'approve' },
      },
      {
        interruptId: 'interrupt-a#1',
        status: 'resolved',
        payload: { type: 'reject', message: 'skip' },
      },
    ])
  })

  it.each([
    ['missing approval state', undefined],
    ['empty approval list', { items: [], activeIndex: 0, submitted: false }],
    ['undecided item', {
      items: [{
        id: 'approval-undecided',
        interruptId: 'interrupt-undecided',
        toolName: 'write_file',
        params: '{}',
        input: '{}',
        description: '审批写入',
        originalArgs: {},
        allowedDecisions: ['approve' as const],
      }],
      activeIndex: 0,
      submitted: false,
    }],
    ['decision excluded by the interrupt', {
      items: [{
        id: 'approval-disallowed',
        interruptId: 'interrupt-disallowed',
        toolName: 'write_file',
        params: '{}',
        input: '{}',
        description: '审批写入',
        originalArgs: {},
        allowedDecisions: ['reject' as const],
        decision: 'approved' as const,
      }],
      activeIndex: 0,
      submitted: false,
    }],
    ['empty interrupt identifier', {
      items: [{
        id: 'approval-empty-id',
        interruptId: '',
        toolName: 'write_file',
        params: '{}',
        input: '{}',
        description: '审批写入',
        originalArgs: {},
        allowedDecisions: ['approve' as const],
        decision: 'approved' as const,
      }],
      activeIndex: 0,
      submitted: false,
    }],
  ])('rejects a resume payload with %s', (_name, approval) => {
    const current = buildEmptyConversation({
      threadId: 'thread-invalid-resume',
      now: '2026-08-05T00:00:00.000Z',
      model: 'GPT-5.5',
    })

    expect(() => buildResumePayload({ ...current, approval })).toThrow()
  })

  it('rejects a resume payload when the authoritative interrupt group has changed', () => {
    const current = buildEmptyConversation({
      threadId: 'thread-replaced-resume',
      now: '2026-08-05T00:00:00.000Z',
      model: 'GPT-5.5',
    })
    const approval = {
      items: [{
        id: 'approval-new',
        interruptId: 'interrupt-new',
        toolName: 'write_file',
        params: '{}',
        input: '{}',
        description: '新审批',
        originalArgs: {},
        allowedDecisions: ['approve' as const],
        decision: 'approved' as const,
      }],
      activeIndex: 0,
      submitted: false,
    }

    expect(() => buildResumePayload(
      { ...current, approval },
      ['interrupt-old'],
    )).toThrow('approval_stale')
  })

  it('does not claim a replacement approval group for an old resume submission', () => {
    const current = buildEmptyConversation({
      threadId: 'thread-replaced-submission',
      now: '2026-08-05T00:00:00.000Z',
      model: 'GPT-5.5',
    })
    const authoritative: Conversation = {
      ...current,
      runStatus: 'waiting_approval',
      approval: {
        items: [{
          id: 'approval-new',
          interruptId: 'interrupt-new',
          toolName: 'write_file',
          params: '{}',
          input: '{}',
          description: '新审批',
          originalArgs: {},
          allowedDecisions: ['approve'],
          decision: 'approved',
        }],
        activeIndex: 0,
        submitted: false,
      },
    }

    const result = prepareResumeSubmission(authoritative, ['interrupt-old'])

    expect(result).toBe(authoritative)
    expect(result.runStatus).toBe('waiting_approval')
    expect(result.approval?.submitted).toBe(false)
  })

  it('restores interrupted tool cards to running when approval submission starts', () => {
    const interrupted = applyConversationEvent(
      applyConversationEvent(
        buildEmptyConversation({
          threadId: 'thread-resume-running',
          now: '2026-08-05T00:00:00.000Z',
          model: 'GPT-5.5',
        }),
        {
          type: 'TOOL_CALL_START',
          rawEvent: {
            streamMode: 'messages',
            source: { kind: 'root', agentType: 'main', agentName: 'main', graphNamespace: [] },
            langgraphNode: 'model',
          },
          toolCallId: 'call-write-file-running',
          toolCallName: 'write_file',
          parentMessageId: 'parent-message',
        },
      ),
      {
        type: 'RUN_FINISHED',
        threadId: THREAD_ID,
        runId: RUN_ID,
        outcome: {
          type: 'interrupt',
          interrupts: [
            interrupt({ id: 'interrupt-running', toolCallId: 'call-write-file-running' }),
          ],
        },
      },
    )

    const resumed = prepareResumeSubmission({
      ...interrupted,
      approval: interrupted.approval
        ? {
            ...interrupted.approval,
            items: interrupted.approval.items.map((item) => ({ ...item, decision: 'approved' as const })),
          }
        : interrupted.approval,
    })

    const toolMessage = resumed.messages.find(
      (message) => message.role === 'tool' && message.meta?.toolCallId === 'call-write-file-running',
    )

    expect(resumed.runStatus).toBe('streaming')
    expect(resumed.approval?.submitted).toBe(true)
    expect(toolMessage?.meta?.status).toBe('running')
    expect(toolMessage?.meta?.interruptId).toBeUndefined()

    const confirmedByServer = applyConversationEvent(resumed, {
      type: 'RUN_STARTED',
      threadId: THREAD_ID,
      runId: `${RUN_ID}-resume`,
    })

    expect(confirmedByServer.approval).toBeUndefined()
    expect(confirmedByServer.runStatus).toBe('streaming')
  })

  it('preserves pending approval when a resumed Runtime fails during initialization', () => {
    const started = applyConversationEvent(
      buildEmptyConversation({
        threadId: THREAD_ID,
        now: '2026-08-05T00:00:00.000Z',
        model: 'GPT-5.5',
      }),
      { type: 'RUN_STARTED', threadId: THREAD_ID, runId: RUN_ID },
    )
    const withTool = applyConversationEvent(started, {
      type: 'TOOL_CALL_START',
      toolCallId: 'call-init-failure',
      toolCallName: 'write_file',
    })
    const interrupted = applyConversationEvent(withTool, {
      type: 'RUN_FINISHED',
      threadId: THREAD_ID,
      runId: RUN_ID,
      outcome: {
        type: 'interrupt',
        interrupts: [interrupt({
          id: 'interrupt-init-failure',
          toolCallId: 'call-init-failure',
        })],
      },
    })
    const submitted = prepareResumeSubmission(
      interrupted,
      ['interrupt-init-failure'],
    )
    expect(submitted.approval?.submitted).toBe(true)
    expect(submitted.messages.find(
      (message) => message.meta?.toolCallId === 'call-init-failure',
    )?.meta?.status).toBe('running')
    const initializationStarted = applyConversationEvent(submitted, {
      type: 'RUN_STARTED',
      threadId: THREAD_ID,
      runId: `${RUN_ID}-resume`,
      rawEvent: { runId: `${RUN_ID}-resume`, initializationFailed: true },
    })
    const failed = applyConversationEvent(initializationStarted, {
      type: 'RUN_ERROR',
      message: 'Runtime 初始化失败',
      code: 'runtime_initialization_error',
      rawEvent: { runId: `${RUN_ID}-resume`, initializationFailed: true },
    })

    expect(initializationStarted.runStatus).toBe('waiting_approval')
    expect(initializationStarted.approval).toEqual(interrupted.approval)
    expect(failed.runStatus).toBe('waiting_approval')
    expect(failed.approval).toEqual(interrupted.approval)
    expect(failed.messages.find(
      (message) => message.meta?.toolCallId === 'call-init-failure',
    )?.meta?.status).toBe('paused')
    expect(failed.notice).toMatchObject({ kind: 'error', content: '继续任务失败，请重新提交' })
  })

  it('marks only the related subagent when its parent task result fails', () => {
    const initial = applyConversationEvent(
      buildEmptyConversation({
        threadId: THREAD_ID,
        now: '2026-08-05T00:00:00.000Z',
        model: 'GPT-5.5',
      }),
      { type: 'RUN_STARTED', threadId: THREAD_ID, runId: RUN_ID },
    )
    const task = applyConversationEvent(initial, {
      type: 'TOOL_CALL_START',
      toolCallId: 'call-subagent-error',
      toolCallName: 'task',
      rawEvent: {
        streamMode: 'messages',
        source: { kind: 'root', agentType: 'main', agentName: 'main', graphNamespace: [] },
        runId: RUN_ID,
      },
    })
    const subRunId = 'subagent-55555555-5555-5555-8555-555555555555'
    const running = applyConversationEvent(task, {
      type: 'RAW',
      source: 'langgraph.tasks',
      rawEvent: { type: 'tasks', phase: 'start', ns: [] },
      event: {
        data: { id: 'graph-error', name: 'tools' },
        provenance: {
          kind: 'root',
          graphNamespace: [],
          agentType: 'main',
          agentName: 'main',
          subagents: [{
            schema: 'tinkerfin.subagent-provenance',
            subagentInvocationId: subRunId,
            graphNamespace: ['tools:graph-error'],
            parentGraphNamespace: [],
            graphTaskId: 'graph-error',
            agentName: 'researcher',
            parentToolCallId: 'call-subagent-error',
            description: '失败任务',
            requestRunId: RUN_ID,
          }],
        },
      },
    })

    const failed = applyConversationEvent(running, {
      type: 'TOOL_CALL_RESULT',
      toolCallId: 'call-subagent-error',
      messageId: 'call-subagent-error-result',
      content: '子智能体运行失败',
      role: 'tool',
      rawEvent: {
        streamMode: 'messages',
        source: { kind: 'root', agentType: 'main', agentName: 'main', graphNamespace: [] },
        runId: RUN_ID,
        relatedSubagentInvocationId: subRunId,
        toolResultStatus: 'error',
      },
    })

    const subagent = failed.messages.find(
      (message) => message.role === 'subagent' && message.meta?.subRunId === subRunId,
    )
    expect(subagent?.meta?.status).toBe('failed')
    expect(failed.runStatus).toBe('streaming')
    expect(failed.activeRunId).toBe(RUN_ID)
    expect(failed.messages.some((message) => message.role === 'error')).toBe(false)
  })
})


it('标题通知仅更新目标会话且不被旧事件覆盖', () => {
  const initial = buildEmptyConversation({ threadId: 'title-thread', now: '2026-09-08T00:00:00Z' })
  const event = { type: 'CUSTOM', name: 'studio.conversation.title.updated', value: {
    threadId: 'title-thread', title: '自动标题', titleSource: 'generated', titleGenerationStatus: 'succeeded', titleSeq: 2,
  } }
  const parsed = parseConversationAgUiEvent(event)
  const updated = applyConversationEvent(initial, parsed)
  expect(updated.title).toBe('自动标题')
  expect(updated.messages).toBe(initial.messages)
  expect(updated.runStatus).toBe(initial.runStatus)
  const manual = { ...updated, title: '用户标题', titleSource: 'user' as const, titleGenerationStatus: 'skipped' as const, titleSeq: 3 }
  expect(applyConversationEvent(manual, parsed).title).toBe('用户标题')
  expect(applyConversationEvent(updated, parsed)).toEqual(updated)
  expect(applyConversationEvent(initial, parseConversationAgUiEvent({ ...event, value: { ...event.value, threadId: 'other' } }))).toBe(initial)
  expect(() => parseConversationAgUiEvent({ ...event, value: { ...event.value, title: '中'.repeat(33) } })).toThrow()
})

it('旧运行错误到达时只记录原运行失败，不停止当前新运行', () => {
  const current = buildEmptyConversation({ now: '2026-09-08T00:00:00Z', threadId: THREAD_ID })
  current.activeRunId = 'new-run'
  current.runStatus = 'streaming'
  current.messages = [{ id: 'old-question', role: 'user', content: '旧问题', createdAt: current.updatedAt, meta: { runId: 'old-run' } }]
  const updated = applyConversationEvent(current, { type: 'RUN_ERROR', code: 'runtime_initialization_error', message: 'private diagnostic', rawEvent: { runId: 'old-run' } })
  expect(updated.runStatus).toBe('streaming')
  expect(updated.activeRunId).toBe('new-run')
  expect(updated.runFailures).toMatchObject([{ runId: 'old-run', retryable: true }])
  expect(updated.notice).toBeUndefined()
})
