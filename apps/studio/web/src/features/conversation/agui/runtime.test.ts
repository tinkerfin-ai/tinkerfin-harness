import { describe, expect, it } from 'vitest'
import { rootToolId } from '../../../test/aguiFixtures'

import type { ConversationAgUiEvent, InterruptEvent } from '../../../api/conversation/types'
import { buildEmptyConversation } from '../../../lib/workspace'
import type { ApprovalAllowedDecision, ApprovalItem, Conversation } from '../../../types'
import {
  applyConversationEvent,
  buildResumePayload,
  prepareResumeSubmission,
} from './runtime'

const THREAD_ID = 'thread-order-check'
const RUN_ID = 'run-order-check'

it('准备失败后保留已提交审批和暂停工具，等待历史确认而非重复授权', () => {
  const waiting = applyConversationEvent(
    applyConversationEvent(applyConversationEvent(buildEmptyConversation({projectId: 'project-1',  threadId: THREAD_ID, now: '2026-10-04T00:00:00Z' }),
      { type: 'RUN_STARTED', threadId: THREAD_ID, runId: RUN_ID }), {
      type: 'TOOL_CALL_START', toolCallId: 'pending-write', toolCallName: 'write_file',
    }),
    { type: 'RUN_FINISHED', threadId: THREAD_ID, runId: RUN_ID, outcome: {
      type: 'interrupt', interrupts: [interrupt({ id: 'approval-write', toolCallId: 'pending-write' })],
    } },
  )
  const submitted = prepareResumeSubmission(waiting)
  const started = applyConversationEvent(submitted, { type: 'RUN_STARTED', threadId: THREAD_ID, runId: 'resume-prepare' })
  const failed = applyConversationEvent(started, {
    type: 'RUN_ERROR', code: 'runtime_initialization_error', message: '准备失败', rawEvent: { runId: 'resume-prepare' },
  })
  expect(started.approval?.submitted).toBe(true)
  expect(failed.approval).toEqual(started.approval)
  expect(failed.messages.find(message => message.meta?.toolCallId === 'pending-write')?.meta).toMatchObject({
    status: 'paused', interruptId: 'approval-write',
  })
})

function nativeContractEvents(): ConversationAgUiEvent[] {
  const subRunId = 'subagent-11111111-1111-5111-8111-111111111111'
  const mainSource = { kind: 'root' as const, agentType: 'main' as const, agentName: 'main', graphNamespace: [] }
  const provenance = {
    schema: 'tinkerfin.subagent-provenance' as const,
    subagentInvocationId: subRunId,
    parentGraphNamespace: [],
    agentName: 'researcher',
    parentToolCallId: rootToolId('call-task'),
    description: '研究百度与 Google',
    requestRunId: RUN_ID,
  }
  const subSource = {
    kind: 'deep_agent_subagent' as const,
    agentType: 'subagent' as const,
    agentName: 'researcher',
    graphNamespace: ['tools:graph-research'],
    parentGraphNamespace: [],
    graphTaskId: 'graph-research',
    parentToolCallId: rootToolId('call-task'),
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
      toolCallId: rootToolId('call-task'),
      toolCallName: 'task',
      rawEvent: { streamMode: 'messages', source: mainSource, runId: RUN_ID },
    },
    {
      type: 'TOOL_CALL_ARGS',
      toolCallId: rootToolId('call-task'),
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
      toolCallId: rootToolId('call-task'),
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

  it('uses values as Todo truth while the standard tool result completes the tool card', () => {
    const initial = buildEmptyConversation({projectId: 'project-1',
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

  it('replays the messages/tasks/values contract fixture without losing state or hierarchy', () => {
    const initial = buildEmptyConversation({projectId: 'project-1',
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
    let current = buildEmptyConversation({projectId: 'project-1',
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
      parentToolCallId: rootToolId(`task-${suffix}`),
      subagentInput: `研究任务 ${suffix.toUpperCase()}`,
      subagentInvocationId: subRunIds[suffix],
    })

    apply({ type: 'RUN_STARTED', threadId: THREAD_ID, runId: RUN_ID })
    for (const suffix of ['a', 'b']) {
      apply({
        type: 'TOOL_CALL_START',
        rawEvent: { streamMode: 'messages', source: mainSource, runId: RUN_ID },
        toolCallId: rootToolId(`task-${suffix}`),
        toolCallName: 'task',
        parentMessageId: 'task-parent',
      })
      apply({
        type: 'TOOL_CALL_ARGS',
        rawEvent: { streamMode: 'messages', source: mainSource, runId: RUN_ID },
        toolCallId: rootToolId(`task-${suffix}`),
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
              parentGraphNamespace: [],
              agentName: 'researcher',
              parentToolCallId: rootToolId(`task-${suffix}`),
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
    const runningTaskA = current.messages.find((message) => message.meta?.toolCallId === rootToolId('task-a'))
    const runningTaskB = current.messages.find((message) => message.meta?.toolCallId === rootToolId('task-b'))

    expect(runningSubagentA?.meta?.input).toBe('研究任务 A')
    expect(runningSubagentA?.meta?.toolCallId).toBe(rootToolId('task-a'))
    expect(runningSubagentB?.meta?.input).toBe('研究任务 B')
    expect(runningSubagentB?.meta?.toolCallId).toBe(rootToolId('task-b'))
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
        toolCallId: rootToolId(`task-${suffix}`),
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
      buildEmptyConversation({projectId: 'project-1',
        threadId: 'thread-multi-interrupt',
        accessMode: 'write_approval',
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

    expect(payload.forwardedProps).toEqual({ projectId: 'project-1', skillIds: [], accessMode: 'write_approval', model: 'GPT-5.5', command: { plan: 'off' } })
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

  it('rejects a resume payload when the authoritative interrupt group has changed', () => {
    const current = buildEmptyConversation({projectId: 'project-1',
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
    const current = buildEmptyConversation({projectId: 'project-1',
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
})

it('旧运行错误到达时只记录原运行失败，不停止当前新运行', () => {
  const current = buildEmptyConversation({projectId: 'project-1',  now: '2026-09-08T00:00:00Z', threadId: THREAD_ID })
  current.activeRunId = 'new-run'
  current.runStatus = 'streaming'
  current.messages = [{ id: 'old-question', role: 'user', content: '旧问题', createdAt: current.updatedAt, meta: { runId: 'old-run' } }]
  const updated = applyConversationEvent(current, { type: 'RUN_ERROR', code: 'runtime_initialization_error', message: 'private diagnostic', rawEvent: { runId: 'old-run' } })
  expect(updated.runStatus).toBe('streaming')
  expect(updated.activeRunId).toBe('new-run')
  expect(updated.runFailures).toMatchObject([{ runId: 'old-run', retryable: true }])
  expect(updated.notice).toBeUndefined()
})
