import { describe, expect, it } from 'vitest'

import type { Message } from '../../../types'
import { buildEmptyConversation } from '../../../lib/workspace'
import { buildConversationDisplayEntries } from './displayEntries'

const tool = (
  id: string,
  toolName: string,
  options: Partial<NonNullable<Message['meta']>> = {},
): Message => ({
  id,
  role: 'tool',
  content: toolName,
  createdAt: '2026-08-30T12:00:00.000Z',
  meta: {
    toolName,
    toolCallId: id,
    runId: 'run-1',
    status: 'completed',
    ...options,
  },
})

describe('buildConversationDisplayEntries', () => {
  it('extracts the selected Todos row without splitting the ordinary batch', () => {
    const selected = tool('tool-selected', 'write_todos', { batchId: 'batch-1' })
    const failed = tool('tool-failed', 'write_todos', {
      batchId: 'batch-1',
      status: 'failed',
    })
    const ordinary = tool('tool-read', 'read_file', { batchId: 'batch-1' })
    const conversation = {
      ...buildEmptyConversation({ now: '2026-08-30T12:00:00.000Z', model: 'main' }),
      messages: [ordinary, selected, failed],
      taskTrace: {
        phase: 'ready' as const,
        snapshot: {
          status: 'ready' as const,
          todoGroups: [{
            id: 'todo-group:run-1',
            userMessageId: 'request-run-1',
            userMessagePreview: '执行任务',
            groupToolCallId: selected.id,
            createdAt: '2026-08-30T12:00:01.000Z',
            status: 'running' as const,
            todos: [],
          }],
        },
      },
    }

    const entries = buildConversationDisplayEntries(conversation)

    expect(entries.map((entry) => entry.type)).toEqual(['tools', 'todo-group'])
    expect(entries[0]?.type === 'tools' && entries[0].messages.map((item) => item.id))
      .toEqual(['tool-read', 'tool-failed'])
    expect(entries[1]?.type === 'todo-group' && entries[1].group.id)
      .toBe('todo-group:run-1')
  })

  it('keeps one unconfirmed row and hides subagent or redundant successful calls', () => {
    const conversation = {
      ...buildEmptyConversation({ now: '2026-08-30T12:00:00.000Z', model: 'main' }),
      messages: [
        tool('first-success', 'write_todos'),
        tool('second-success', 'write_todos'),
        tool('subagent-write', 'write_todos', { sourceAgentName: 'worker' }),
      ],
    }

    const entries = buildConversationDisplayEntries(conversation)

    expect(entries).toHaveLength(1)
    expect(entries[0]?.type === 'tools' && entries[0].messages[0]?.id)
      .toBe('first-success')
  })
})

it('失败属于原提问轮次，部分回答保留且不影响后续成功轮次', () => {
  const conversation = buildEmptyConversation({ now: '2026-09-08T00:00:00Z' })
  conversation.messages = [
    { id: 'q1', role: 'user', content: '问题1', createdAt: conversation.updatedAt, meta: { runId: 'r1' } },
    { id: 'a1', role: 'assistant', content: '部分回答', createdAt: conversation.updatedAt, meta: { runId: 'r1' } },
    { id: 'q2', role: 'user', content: '问题2', createdAt: conversation.updatedAt, meta: { runId: 'r2' } },
    { id: 'a2', role: 'assistant', content: '完整回答', createdAt: conversation.updatedAt, meta: { runId: 'r2' } },
  ]
  conversation.runFailures = [{ runId: 'r1', errorCode: 'model_error', failedAt: conversation.updatedAt, retryable: false }]
  const entries = buildConversationDisplayEntries(conversation)
  expect(entries.map(entry => entry.type === 'run-failure' ? 'failure' : entry.type === 'message' ? entry.message.id : entry.type)).toEqual(['q1', 'a1', 'failure', 'q2', 'a2'])
  expect(conversation.messages[0]?.meta?.status).toBeUndefined()
})


it('同一提问跨恢复运行只展示一张清单，保留失败工具并区分下一次提问', () => {
  const conversation = buildEmptyConversation({ now: '2026-09-15T07:55:00Z' })
  conversation.messages = [
    { id: 'question', role: 'user', content: '生成报告', createdAt: conversation.updatedAt, meta: { runId: 'run-1' } },
    tool('selected', 'write_todos', { runId: 'resume-1' }),
    tool('updated', 'write_todos', { runId: 'resume-2' }),
    tool('failed', 'write_todos', { runId: 'resume-2', status: 'failed' }),
    { id: 'next-question', role: 'user', content: '新任务', createdAt: conversation.updatedAt, meta: { runId: 'run-2' } },
    tool('unconfirmed', 'write_todos', { runId: 'run-2' }),
  ]
  conversation.taskTrace = { phase: 'ready', snapshot: { status: 'ready', todoGroups: [{
    id: 'todo-group:run-1', userMessageId: 'question', userMessagePreview: '生成报告',
    groupToolCallId: 'selected', createdAt: conversation.updatedAt, status: 'completed', todos: [],
  }] } }
  const entries = buildConversationDisplayEntries(conversation)
  expect(entries.filter(entry => entry.type === 'todo-group')).toHaveLength(1)
  expect(entries.flatMap(entry => entry.type === 'tools' ? entry.messages.map(message => message.id) : []))
    .toEqual(['failed', 'unconfirmed'])
})
