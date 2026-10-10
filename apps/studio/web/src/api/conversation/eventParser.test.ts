import { describe, expect, it } from 'vitest'

import { parseConversationAgUiEvent } from './eventParser'

describe('AG-UI 事件边界解析', () => {

  it('接受当前服务端使用的来源、标题和中断扩展', () => {
    const event = {
      type: 'RUN_STARTED',
      threadId: 'thread-1',
      runId: 'run-1',
      parentRunId: 'run-parent',
      title: '权威标题',
      titleSource: 'default',
      titleGenerationStatus: 'idle',
      titleSeq: 0,
      rawEvent: {
        streamMode: 'tasks',
        runId: 'run-1',
        source: {
          kind: 'deep_agent_subagent',
          agentType: 'subagent',
          agentName: 'researcher',
          graphNamespace: ['tools:task-1'],
          graphTaskId: 'task-1',
          parentGraphNamespace: [],
          parentToolCallId: 'tool-1',
          subagentInput: '研究问题',
          subagentInvocationId: 'invocation-1',
        },
      },
    }

    expect(parseConversationAgUiEvent(event)).toBe(event)
    expect(parseConversationAgUiEvent({
      type: 'RUN_FINISHED',
      threadId: 'thread-1',
      runId: 'run-1',
      outcome: {
        type: 'interrupt',
        interrupts: [{
          id: 'interrupt-1',
          reason: 'tool_review',
          toolCallId: 'tool-1',
          responseSchema: {},
          metadata: { source: 'deepagents' },
        }],
      },
    }).type).toBe('RUN_FINISHED')
  })

  it('接受重复引用的 JSON 子值并拒绝真实循环', () => {
    const shared = { file_path: '/reports/result.txt' }
    const event = {
      type: 'RUN_FINISHED',
      threadId: 'thread-shared-json',
      runId: 'run-shared-json',
      outcome: {
        type: 'interrupt',
        interrupts: [{
          id: 'interrupt-shared-json',
          reason: 'tool_call',
          metadata: { action: shared, projection: shared },
        }],
      },
    }
    const cycle: Record<string, unknown> = {}
    cycle.self = cycle

    expect(parseConversationAgUiEvent(event)).toBe(event)
    expect(() => parseConversationAgUiEvent({
      type: 'CUSTOM',
      name: 'cycle',
      value: cycle,
    })).toThrow('事件流包含无效的 AG-UI 事件')
  })

  it('rejects JSON payloads deeper than the supported stream boundary', () => {
    let nested: unknown = 'leaf'
    for (let depth = 0; depth < 65; depth += 1) nested = { nested }

    expect(() => parseConversationAgUiEvent({
      type: 'STATE_SNAPSHOT',
      snapshot: nested,
    })).toThrow('事件流包含无效的 AG-UI 事件')
  })
})
