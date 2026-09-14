import { describe, expect, it } from 'vitest'

import { parseConversationAgUiEvent } from '../../../api/conversation/eventParser'
import { parseTaskTraceSnapshot } from '../../../api/conversation/taskTrace'
import type { JsonObject } from '../../../types'
import { LiveTodoTraceProjector } from './liveProjection'

const consumeConfirmedGroup = ({
  projector,
  runId,
  userMessageId,
  preview,
  toolCallId,
  todoId,
  createdAt,
}: {
  projector: LiveTodoTraceProjector
  runId: string
  userMessageId: string
  preview: string
  toolCallId: string
  todoId: string
  createdAt: string
}) => {
  projector.startRun({
    runId,
    inputKind: 'ordinary',
    turn: { runId, userMessageId, userMessagePreview: preview },
  })
  const events: Array<[unknown, string, JsonObject]> = [
    [{
      type: 'TOOL_CALL_START',
      rawEvent: {
        source: { kind: 'root', agentType: 'main', agentName: 'main', graphNamespace: [] },
        runId,
      },
      toolCallId,
      toolCallName: 'write_todos',
      parentMessageId: `assistant:${runId}`,
    }, createdAt, {}],
    [{
      type: 'TOOL_CALL_RESULT',
      rawEvent: {
        source: { kind: 'root', agentType: 'main', agentName: 'main', graphNamespace: [] },
        runId,
        toolResultStatus: 'success',
      },
      messageId: `tool-message:${runId}`,
      toolCallId,
      content: 'ok',
      role: 'tool',
    }, createdAt, {}],
    [{
      type: 'STATE_SNAPSHOT',
      rawEvent: {
        source: { kind: 'root', agentType: 'main', agentName: 'main', graphNamespace: [] },
        runId,
      },
      snapshot: { todos: [{ id: todoId, content: preview, status: 'in_progress' }] },
    }, createdAt, {
      todos: [{ id: todoId, content: preview, status: 'in_progress' }],
    }],
  ]
  for (const [raw, receivedAt, rootState] of events) {
    projector.consume(parseConversationAgUiEvent(raw), { receivedAt, rootState })
  }
}

describe('todo group contracts', () => {
  it('成功结束保留未确认完成，恢复后只接受模型明确完成的清单', () => {
    let projector = new LiveTodoTraceProjector()
    consumeConfirmedGroup({
      projector,
      runId: 'run-1',
      userMessageId: 'message-1',
      preview: '实现任务轨迹',
      toolCallId: 'tool-1',
      todoId: 'todo-1',
      createdAt: '2026-08-30T12:00:03.000Z',
    })

    expect(projector.snapshot).toEqual({
      status: 'ready',
      todoGroups: [{
        id: 'todo-group:run-1',
        userMessageId: 'message-1',
        userMessagePreview: '实现任务轨迹',
        groupToolCallId: 'tool-1',
        createdAt: '2026-08-30T12:00:03.000Z',
        status: 'running',
        todos: [{ id: 'todo-1', content: '实现任务轨迹', status: 'running' }],
      }],
    })
    const consume = (raw: unknown, rootState: JsonObject = {}) => projector.consume(
      parseConversationAgUiEvent(raw),
      { receivedAt: '2026-08-30T12:00:05.000Z', rootState },
    )
    consume({ type: 'RUN_FINISHED', threadId: 'thread-1', runId: 'run-1' })
    const ended = parseTaskTraceSnapshot(projector.snapshot)
    expect(ended.todoGroups[0]).toMatchObject({
      status: 'incomplete', todos: [{ status: 'incomplete' }],
    })
    if (ended.status !== 'ready') throw new Error('已结束任务轨迹不可用')
    projector.close()
    projector = new LiveTodoTraceProjector()
    projector.hydrate(ended, { headRunId: 'run-1', isRunning: false })
    projector.startRun({ runId: 'resume-1', inputKind: 'resume', parentRunId: 'run-1' })
    const rootState = { todos: [{ id: 'todo-1', content: '实现任务轨迹', status: 'completed' }] }
    consume({ type: 'STATE_SNAPSHOT', snapshot: rootState }, rootState)
    consume({ type: 'RUN_FINISHED', threadId: 'thread-1', runId: 'resume-1' }, rootState)
    expect(projector.snapshot.todoGroups).toMatchObject([{
      id: 'todo-group:run-1', status: 'completed', todos: [{ status: 'completed' }],
    }])
    projector.close()
  })

  it('rejects partial, versioned, and contradictory wire values', () => {
    expect(() => parseTaskTraceSnapshot({
      status: 'ready',
      todoGroups: [],
      errorCode: 'trace_incomplete',
    })).toThrow('可用任务轨迹不能包含错误码')
    expect(() => parseTaskTraceSnapshot({
      status: 'unavailable',
      todoGroups: [{ id: 'partial' }],
      errorCode: 'trace_incomplete',
    })).toThrow('任务轨迹分组格式无效')
    expect(() => parseTaskTraceSnapshot({
      status: 'ready',
      todoGroups: [],
      version: 1,
    })).toThrow('任务轨迹响应格式无效')
  })

  it('hydrates a newer head Turn without rebinding it to the previous Group', () => {
    const projector = new LiveTodoTraceProjector()
    projector.hydrate({
      status: 'ready',
      todoGroups: [{
        id: 'todo-group:run-1',
        userMessageId: 'message-1',
        userMessagePreview: '第一轮',
        groupToolCallId: 'tool-1',
        createdAt: '2026-08-30T12:00:00.000Z',
        status: 'completed',
        todos: [{ id: 'todo-1', content: '第一轮任务', status: 'completed' }],
      }],
    }, {
      headRunId: 'run-2',
      latestTurn: {
        runId: 'run-2',
        userMessageId: 'message-2',
        userMessagePreview: '第二轮',
      },
    })
    projector.startRun({ runId: 'run-3', inputKind: 'resume' })
    const events: Array<[unknown, string, JsonObject]> = [
      [{
        type: 'TOOL_CALL_START',
        rawEvent: {
          source: { kind: 'root', agentType: 'main', agentName: 'main', graphNamespace: [] },
          runId: 'run-3',
        },
        toolCallId: 'tool-2',
        toolCallName: 'write_todos',
        parentMessageId: 'assistant-2',
      }, '2026-08-30T12:01:00.000Z', {}],
      [{
        type: 'TOOL_CALL_RESULT',
        rawEvent: {
          source: { kind: 'root', agentType: 'main', agentName: 'main', graphNamespace: [] },
          runId: 'run-3',
          toolResultStatus: 'success',
        },
        messageId: 'tool-message-2',
        toolCallId: 'tool-2',
        content: 'ok',
        role: 'tool',
      }, '2026-08-30T12:01:01.000Z', {}],
      [{
        type: 'STATE_SNAPSHOT',
        rawEvent: {
          source: { kind: 'root', agentType: 'main', agentName: 'main', graphNamespace: [] },
          runId: 'run-3',
        },
        snapshot: { todos: [{ id: 'todo-2', content: '第二轮任务', status: 'in_progress' }] },
      }, '2026-08-30T12:01:02.000Z', {
        todos: [{ id: 'todo-2', content: '第二轮任务', status: 'in_progress' }],
      }],
    ]
    for (const [raw, receivedAt, rootState] of events) {
      projector.consume(parseConversationAgUiEvent(raw), { receivedAt, rootState })
    }

    expect(projector.snapshot).toMatchObject({
      status: 'ready',
      todoGroups: [
        {
          id: 'todo-group:run-2',
          userMessageId: 'message-2',
          groupToolCallId: 'tool-2',
          todos: [{ content: '第二轮任务' }],
        },
        {
          id: 'todo-group:run-1',
          userMessageId: 'message-1',
          groupToolCallId: 'tool-1',
          todos: [{ content: '第一轮任务' }],
        },
      ],
    })
    projector.close()
  })

  it('orders same-millisecond Groups by newest creation order', () => {
    const projector = new LiveTodoTraceProjector()
    const createdAt = '2026-08-30T12:00:00.000Z'
    consumeConfirmedGroup({
      projector,
      runId: 'run-1',
      userMessageId: 'message-1',
      preview: '第一轮任务',
      toolCallId: 'tool-1',
      todoId: 'todo-1',
      createdAt,
    })
    projector.consume(parseConversationAgUiEvent({
      type: 'RUN_FINISHED',
      threadId: 'thread:same-time',
      runId: 'run-1',
    }), { receivedAt: createdAt, rootState: {} })
    consumeConfirmedGroup({
      projector,
      runId: 'run-2',
      userMessageId: 'message-2',
      preview: '第二轮任务',
      toolCallId: 'tool-2',
      todoId: 'todo-2',
      createdAt,
    })

    expect(projector.snapshot.status).toBe('ready')
    if (projector.snapshot.status === 'ready') {
      expect(projector.snapshot.todoGroups.map((group) => group.id).slice(0, 2))
        .toEqual(['todo-group:run-2', 'todo-group:run-1'])
    }
    projector.close()
  })

  it('preserves authoritative same-millisecond order after hydrated updates', () => {
    const projector = new LiveTodoTraceProjector()
    const createdAt = '2026-08-30T12:00:00.000Z'
    projector.hydrate({
      status: 'ready',
      todoGroups: [
        {
          id: 'todo-group:run-2',
          userMessageId: 'message-2',
          userMessagePreview: '第二轮',
          groupToolCallId: 'tool-2',
          createdAt,
          status: 'running',
          todos: [{ id: 'todo-2', content: '第二轮任务', status: 'running' }],
        },
        {
          id: 'todo-group:run-1',
          userMessageId: 'message-1',
          userMessagePreview: '第一轮',
          groupToolCallId: 'tool-1',
          createdAt,
          status: 'completed',
          todos: [{ id: 'todo-1', content: '第一轮任务', status: 'completed' }],
        },
      ],
    }, {
      headRunId: 'run-2',
      latestTurn: {
        runId: 'run-2',
        userMessageId: 'message-2',
        userMessagePreview: '第二轮',
      },
    })
    projector.startRun({ runId: 'run-3', inputKind: 'resume' })
    projector.consume(parseConversationAgUiEvent({
      type: 'STATE_SNAPSHOT',
      rawEvent: {
        source: { kind: 'root', agentType: 'main', agentName: 'main', graphNamespace: [] },
        runId: 'run-3',
      },
      snapshot: { todos: [{ id: 'todo-2', content: '第二轮任务', status: 'completed' }] },
    }), {
      receivedAt: '2026-08-30T12:01:00.000Z',
      rootState: {
        todos: [{ id: 'todo-2', content: '第二轮任务', status: 'completed' }],
      },
    })

    expect(projector.snapshot.status).toBe('ready')
    if (projector.snapshot.status === 'ready') {
      expect(projector.snapshot.todoGroups.map((group) => group.id))
        .toEqual(['todo-group:run-2', 'todo-group:run-1'])
    }
    projector.close()
  })
})
