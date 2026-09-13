import { applyConversationEvent, prepareResumeSubmission } from '../agui'
import type { JsonValue } from '../../../types'
import type { ConversationAgUiEvent } from '../../../api/conversation/types'
import { toolReviewInterrupts, planInterrupt } from '../../../test/aguiFixtures'
import { describe, expect, it } from 'vitest'
import mediaFixture from '../agui/contracts/message-attachments.fixture.json'

import type { ConversationHistoryDetail, ConversationTraceUpdate } from '../../../api/conversation/history'
import { applyConversationTraceUpdate, restoreConversationFromTrace } from './runtime'

const detail = (): ConversationHistoryDetail => ({ accessMode: 'write_approval',
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
  interactions: [],
  status: { execution: 'succeeded', headRunId: 'run-1' },
  completeness: { missingPrefix: false, missingTail: false, payloadOmitted: false },
  taskTrace: { status: 'ready', todoGroups: [] },
  createdAt: '2026-08-28T00:00:00',
  updatedAt: '2026-08-28T00:00:03',
})

describe('Trace conversation projection', () => {
  it('从持久记录恢复用户、助手和工具图片，不把附件引用显示为文字', () => {
    const snapshot = detail()
    snapshot.messages = snapshot.messages.map(message => ({
      ...message,
      content: [{ type: 'text', text: '查看图片' }, ...mediaFixture.toolContent],
    }))
    const conversation = restoreConversationFromTrace(snapshot, { model: 'main', includeTaskTrace: true })
    for (const role of ['user', 'assistant', 'tool']) {
      const message = conversation.messages.find(item => item.role === role)
      expect(message?.attachments).toEqual([mediaFixture.attachment])
      expect(role === 'tool' ? message?.meta?.result : message?.content).toBe('查看图片')
    }
  })
  it('子智能体仅返回图片时也能从历史恢复到对应卡片', () => {
    const snapshot = detail()
    snapshot.graph.nodes = [{
      ...snapshot.graph.nodes[0]!, id: 'child', kind: 'subagent', name: 'researcher',
      graphNamespace: ['tools:child'], sourceId: 'call-child', result: null,
    }]
    snapshot.messages = [{
      ...snapshot.messages[1]!, graphNamespace: ['tools:child'], content: mediaFixture.toolContent,
    }]
    const conversation = restoreConversationFromTrace(snapshot, { model: 'main', includeTaskTrace: true })
    expect(conversation.messages).toHaveLength(1)
    expect(conversation.messages[0].role).toBe('subagent')
    expect(conversation.messages[0].attachments).toEqual([mediaFixture.attachment])
    expect(conversation.messages[0].meta?.result).toBe('')
  })
  it('orders HTTP snapshots within one millisecond and allows observed owner recovery', () => {
    const first = detail()
    first.status = { execution: 'running', headRunId: first.headRunId }
    const lost: ConversationHistoryDetail = {
      ...first, observedAt: '2026-09-05T00:00:00.000002Z',
      status: { execution: 'unknown', headRunId: first.headRunId },
      completeness: { ...first.completeness, missingTail: true },
    }
    const current = restoreConversationFromTrace(lost, { model: 'main', includeTaskTrace: true })
    const stale = { ...first, observedAt: '2026-09-05T00:00:00.000001Z' }
    expect(restoreConversationFromTrace(stale, {
      previous: current, model: 'main', includeTaskTrace: true,
    })).toBe(current)
    expect(() => restoreConversationFromTrace({ ...first, observedAt: lost.observedAt }, {
      previous: current, model: 'main', includeTaskTrace: true,
    })).toThrow('stream_event_invalid')
    const recovered = restoreConversationFromTrace({
      ...first, observedAt: '2026-09-05T00:00:00.000003Z',
    }, { previous: current, model: 'main', includeTaskTrace: true })
    expect(recovered.runStatus).toBe('detached')
    expect(recovered.trace?.completeness.missingTail).toBe(false)
  })

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

  it('applies ownership changes at the same sequence without replaying content', () => {
    const source = detail()
    source.status = { execution: 'running', headRunId: source.headRunId }
    const initial = restoreConversationFromTrace(source, {
      model: 'main', includeTaskTrace: true, lastDeliveredSeq: 73,
    })
    const update: ConversationTraceUpdate = {
      asOfSeq: source.asOfSeq,
      generation: source.generation,
      observedAt: '2026-09-05T00:00:00.000001Z',
      events: [],
      facts: [],
      messages: { upserts: [], removes: [] },
      reasoning: { upserts: [], removes: [] },
      interactions: { upserts: [], removes: [] },
      graph: {
        asOfSeq: source.asOfSeq,
        nextCursor: null,
        turnUpserts: [],
        turnRemoves: [],
        nodeUpserts: [],
        nodeRemoves: [],
        orderedNodeIds: source.graph.orderedNodeIds,
        matchedNodeIds: source.graph.matchedNodeIds,
        completeness: source.graph.completeness,
      },
      state: source.state,
      status: { execution: 'unknown', headRunId: source.headRunId },
      completeness: { ...source.completeness, missingTail: true },
      messageCount: source.messageCount,
      toolCallCount: source.toolCallCount,
      projections: {}, runFailures: [],
    }

    const updated = applyConversationTraceUpdate(initial, update, null, true)
    expect(updated.runStatus).toBe('error')
    expect(updated.activeRunId).toBeUndefined()
    expect(updated.trace?.completeness.missingTail).toBe(true)
    expect(updated.trace?.asOfSeq).toBe(source.asOfSeq)
    expect(updated.trace?.messages).toEqual(source.messages)
    expect(updated.trace?.graph).toEqual(source.graph)
    expect(updated.lastSeq).toBe(73)
    const repeated = applyConversationTraceUpdate(updated, update, null, true)
    expect(repeated.messages).toEqual(updated.messages)
    expect(repeated.trace).toEqual(updated.trace)
    expect(applyConversationTraceUpdate(updated, {
      ...update, asOfSeq: source.asOfSeq - 1, status: source.status,
    }, null, true)).toBe(updated)
    expect(() => applyConversationTraceUpdate(updated, {
      ...update,
      messages: { upserts: [{ ...source.messages[1]!, content: '不应替换' }], removes: [] },
    }, null, true)).toThrow('stream_event_invalid')
  })

  it('hydrates messages, tools, reasoning, todos and status without AG-UI replay', () => {
    const restored = restoreConversationFromTrace(detail(), { model: 'fallback', includeTaskTrace: true })

    expect(restored.runStatus).toBe('idle')
    expect(restored.mode).toBe('plan')
    expect(restored.todos).toEqual([
      { id: 'trace-todo-0', content: '验证结果', status: 'completed' },
    ])
    expect(restored.messages.map((message) => message.role)).toEqual([
      'user',
      'assistant',
      'tool',
    ])
    expect(restored.messages[1]?.meta?.reasoning).toBe('已授权推理')
    expect(restored.messages[2]?.meta).toMatchObject({
      toolName: 'write_file',
      params: '{\n  "file_path": "/result.txt"\n}',
      result: 'written',
      status: 'completed',
    })
    expect(restored.lastSeq).toBeUndefined()
    expect(restored.trace?.asOfSeq).toBe(8)
    expect(restored.trace).not.toHaveProperty('taskTrace')
    expect(restored.taskTrace).toEqual({
      phase: 'ready',
      snapshot: { status: 'ready', todoGroups: [] },
    })
  })

  it('preserves a caller-owned Messaging cursor across Trace projection', () => {
    const restored = restoreConversationFromTrace(detail(), {
      model: 'fallback',
      includeTaskTrace: true,
      lastDeliveredSeq: 73,
    })

    expect(restored.lastSeq).toBe(73)
  })

  it('keeps subgraph messages out of the main conversation timeline', () => {
    const source = detail()
    source.messages = [
      ...source.messages,
      {
        ...source.messages[0]!,
        id: 'message:subgraph-user',
        sourceId: 'subgraph-user',
        graphNamespace: ['tools:planner'],
        traceSeq: 5,
        content: '内部输入',
      },
      {
        ...source.messages[1]!,
        id: 'message:subgraph-assistant',
        sourceId: 'subgraph-assistant',
        graphNamespace: ['tools:planner'],
        traceSeq: 6,
        content: '内部输出',
      },
    ]

    const restored = restoreConversationFromTrace(source, { model: 'fallback', includeTaskTrace: true })

    expect(restored.messages.filter((message) => (
      message.role === 'user' || message.role === 'assistant'
    )).map((message) => message.id)).toEqual([
      'public-user-1',
      'public-assistant-1',
    ])
  })

  it('restores active SubAgent partial output without leaking it into main messages', () => {
    const source = detail()
    source.status = { execution: 'running', headRunId: 'run-1' }
    source.messages = [
      ...source.messages,
      {
        ...source.messages[1]!,
        id: 'message:subagent-partial-a',
        sourceId: 'subagent-partial-a',
        graphNamespace: ['tools:parent-task'],
        traceSeq: 6,
        content: '第一段子任务输出',
        status: 'streaming',
        completedAt: null,
      },
      {
        ...source.messages[1]!,
        id: 'message:subagent-partial-b',
        sourceId: 'subagent-partial-b',
        graphNamespace: ['tools:parent-task'],
        traceSeq: 7,
        content: '第二段子任务输出',
        status: 'streaming',
        completedAt: null,
      },
    ]
    source.graph.nodes = [{
      ...source.graph.nodes[0]!,
      id: 'subagent-running',
      kind: 'subagent',
      name: 'researcher',
      graphNamespace: ['tools:parent-task'],
      sourceId: 'call-task',
      status: 'running',
      request: { description: '检查当前行为', subagent_type: 'researcher' },
      result: undefined,
      resultOmitted: false,
      completedAt: null,
    }]

    const restored = restoreConversationFromTrace(source, { model: 'fallback', includeTaskTrace: true })
    const subagent = restored.messages.find((message) => message.role === 'subagent')

    expect(subagent?.meta?.result).toBe('第一段子任务输出\n\n第二段子任务输出')
    expect(restored.messages.filter((message) => (
      message.role === 'user' || message.role === 'assistant'
    )).map((message) => message.id)).toEqual([
      'public-user-1',
      'public-assistant-1',
    ])
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

  it('applies id-based semantic deltas and replaces state atomically', () => {
    const initial = restoreConversationFromTrace(detail(), { model: 'fallback', includeTaskTrace: true })

    const updated = applyConversationTraceUpdate(initial, {
      asOfSeq: 10,
      generation: 'generation-test',
      observedAt: '2026-09-05T00:00:00.000001Z',
      events: [],
      facts: [],
      messages: {
        upserts: [{
          ...detail().messages[1]!,
          content: '更新后的回答',
        }],
        removes: [],
      },
      reasoning: { upserts: [], removes: [] },
      graph: {
        asOfSeq: 10,
        nextCursor: null,
        turnUpserts: [],
        turnRemoves: ['turn-1'],
        nodeUpserts: [],
        nodeRemoves: ['tool-node'],
        orderedNodeIds: [],
        matchedNodeIds: [],
        completeness: {
          callTrackingMissing: false,
          relationshipEvidenceMissing: false,
          detailsOmitted: false,
        },
      },
      interactions: { upserts: [], removes: [] },
      state: { root: { todos: [] }, subgraphs: {} },
      status: { execution: 'failed', headRunId: 'run-1' },
      completeness: { missingPrefix: false, missingTail: true, payloadOmitted: false },
      messageCount: 2,
      toolCallCount: 1,
      projections: {}, runFailures: [],
    }, null, true)

    expect(updated.trace?.asOfSeq).toBe(10)
    expect(updated.messages.find((message) => message.role === 'assistant')?.content)
      .toBe('更新后的回答')
    expect(updated.messages.some((message) => message.role === 'tool')).toBe(false)
    expect(updated.todos).toEqual([])
    expect(updated.runStatus).toBe('error')
    expect(updated.taskTrace).toBe(initial.taskTrace)
  })

  it('restores public multi-action approval IDs without decoding captured arguments', () => {
    const source = detail()
    source.status = { execution: 'waiting', headRunId: 'run-1' }
    source.interactions = [{
      agui: toolReviewInterrupts('native-interrupt', [{ toolCallId: 'call-a', args: { file_path: '/a.txt' }, description: '写入 A 文件' }, { toolCallId: 'call-b', args: { file_path: '/b.txt' } }]),
      id: 'interaction-scoped',
      traceSeq: 5,
      sourceId: 'native-interrupt',
      graphNamespace: [],
      runId: 'run-1',
      kind: 'tool_approval',
      toolCallIds: ['call-a', 'call-b'],
      status: 'pending',
      payloadOmitted: false,
      payload: {
        action_requests: [
          {
            name: 'write_file',
            description: '写入 A 文件',
            arguments: {
              disposition: 'inline',
              safeSizeBytes: 20,
              value: { '': { file_path: '/a.txt' } },
            },
          },
          {
            name: 'write_file',
            arguments: {
              disposition: 'inline',
              safeSizeBytes: 20,
              value: { '/file_path': '/b.txt' },
            },
          },
        ],
        review_configs: [
          { action_name: 'write_file', allowed_decisions: ['approve', 'reject'] },
          { action_name: 'write_file', allowed_decisions: ['approve', 'reject'] },
        ],
      },
      openedAt: '2026-08-28T00:00:04Z',
      resolvedAt: null,
    }]
    source.graph.nodes = [
      { ...source.graph.nodes[0]!, id: 'tool-b', status: 'waiting', sourceId: 'call-b' },
      { ...source.graph.nodes[0]!, id: 'tool-a', status: 'waiting', sourceId: 'call-a' },
    ]

    const restored = restoreConversationFromTrace(source, { model: 'fallback', includeTaskTrace: true })

    expect(restored.approval?.items.map((item) => item.interruptId)).toEqual([
      'native-interrupt#0',
      'native-interrupt#1',
    ])
    expect(restored.approval?.items.map((item) => item.originalArgs.file_path)).toEqual([
      '/a.txt',
      '/b.txt',
    ])
    expect(restored.approval?.items.map((item) => item.toolCallId)).toEqual([
      'call-a',
      'call-b',
    ])
    expect(restored.approval?.items.map((item) => item.description)).toEqual([
      '写入 A 文件',
      'write_file',
    ])
    expect(restored.pendingInteractionKind).toBe('tool_approval')
  })

  it('preserves public arguments for each same-name HITL action', () => {
    const source = detail()
    source.status = { execution: 'waiting', headRunId: 'run-1' }
    source.interactions = [{
      agui: toolReviewInterrupts('native-interrupt', [{ toolCallId: 'call-a', args: { content: 'A', file_path: '/multi-hitl-a.txt' } }, { toolCallId: 'call-b', args: { content: 'B', file_path: '/multi-hitl-b.txt' } }]),
      id: 'interaction-full-content',
      traceSeq: 5,
      sourceId: 'native-interrupt',
      graphNamespace: [],
      runId: 'run-1',
      kind: 'tool_approval',
      toolCallIds: ['call-a', 'call-b'],
      status: 'pending',
      payloadOmitted: false,
      payload: {
        action_requests: [
          {
            name: 'write_file',
            arguments: {
              disposition: 'inline',
              safeSizeBytes: 47,
              value: { content: 'A', file_path: '/multi-hitl-a.txt' },
            },
          },
          {
            name: 'write_file',
            arguments: {
              disposition: 'inline',
              safeSizeBytes: 47,
              value: { content: 'B', file_path: '/multi-hitl-b.txt' },
            },
          },
        ],
        review_configs: [
          { action_name: 'write_file', allowed_decisions: ['approve', 'reject'] },
          { action_name: 'write_file', allowed_decisions: ['approve', 'reject'] },
        ],
      },
      openedAt: '2026-08-28T00:00:04Z',
      resolvedAt: null,
    }]
    source.graph.nodes = [
      {
        ...source.graph.nodes[0]!,
        id: 'tool-b',
        status: 'waiting',
        sourceId: 'call-b',
        request: { content: 'B', file_path: '/multi-hitl-b.txt' },
      },
      {
        ...source.graph.nodes[0]!,
        id: 'tool-a',
        status: 'waiting',
        sourceId: 'call-a',
        request: { content: 'A', file_path: '/multi-hitl-a.txt' },
      },
    ]

    const restored = restoreConversationFromTrace(source, { model: 'fallback', includeTaskTrace: true })

    expect(restored.approval?.items.map((item) => ({
      toolCallId: item.toolCallId,
      originalArgs: item.originalArgs,
      params: item.params,
    }))).toEqual([
      {
        toolCallId: 'call-a',
        originalArgs: { content: 'A', file_path: '/multi-hitl-a.txt' },
        params: '{\n  "content": "A",\n  "file_path": "/multi-hitl-a.txt"\n}',
      },
      {
        toolCallId: 'call-b',
        originalArgs: { content: 'B', file_path: '/multi-hitl-b.txt' },
        params: '{\n  "content": "B",\n  "file_path": "/multi-hitl-b.txt"\n}',
      },
    ])
  })

  it('merges pending Tool approvals while a SubAgent Tool is still running', () => {
    const source = detail()
    source.status = { execution: 'waiting', headRunId: 'run-1' }
    const interaction = (
      id: string,
      traceSeq: number,
      sourceId: string,
      graphNamespace: string[],
      toolCallId: string,
      filePath: string,
    ): ConversationHistoryDetail['interactions'][number] => ({
      agui: toolReviewInterrupts(sourceId, [{ toolCallId, args: { file_path: filePath } }]),
      id,
      traceSeq,
      sourceId,
      graphNamespace,
      runId: 'run-1',
      kind: 'tool_approval',
      toolCallIds: [toolCallId],
      status: 'pending',
      payloadOmitted: false,
      payload: {
        action_requests: [{
          name: 'write_file',
          arguments: {
            disposition: 'inline',
            safeSizeBytes: 20,
            value: { '/file_path': filePath },
          },
        }],
        review_configs: [{
          action_name: 'write_file',
          allowed_decisions: ['approve', 'reject'],
        }],
      },
      openedAt: '2026-08-28T00:00:04Z',
      resolvedAt: null,
    })
    source.interactions = [
      interaction(
        'interaction-b',
        7,
        'interrupt-b',
        ['tools:child'],
        'call-b',
        '/b.txt',
      ),
      interaction('interaction-a', 5, 'interrupt-a', [], 'call-a', '/a.txt'),
    ]
    source.graph.nodes = [
      {
        ...source.graph.nodes[0]!,
        id: 'tool-b',
        startedSeq: 6,
        graphNamespace: ['tools:child'],
        status: 'running',
        sourceId: 'call-b',
      },
      {
        ...source.graph.nodes[0]!,
        id: 'tool-a',
        startedSeq: 4,
        graphNamespace: [],
        status: 'waiting',
        sourceId: 'call-a',
      },
    ]

    const restored = restoreConversationFromTrace(source, { model: 'fallback', includeTaskTrace: true })

    expect(restored.approval?.items.map((item) => ({
      interruptId: item.interruptId,
      toolCallId: item.toolCallId,
      filePath: item.originalArgs.file_path,
    }))).toEqual([
      { interruptId: 'interrupt-a', toolCallId: 'call-a', filePath: '/a.txt' },
      { interruptId: 'interrupt-b', toolCallId: 'call-b', filePath: '/b.txt' },
    ])
    expect(restored.pendingInteractionKind).toBe('tool_approval')
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

  it('hydrates Plan clarification from the public interrupt', () => {
    const source = detail()
    source.status = { execution: 'succeeded', headRunId: 'run-1' }
    source.interactions = [{
      agui: [planInterrupt('plan-interrupt', {
        schema: 'tinkerfin.runtime-interrupt',
        kind: 'tinkerfin:plan_clarification',
        message: '回答问题',
        responseSchema: { type: 'object' },
        metadata: {
          origin: 'plan',
          clarification: {
            form: {
              title: '确认范围',
              description: '补充执行范围',
              questions: [{
                id: 'scope',
                answerType: 'text',
                prompt: '请输入范围',
                required: true,
              }],
            },
          },
        },
      })],
      id: 'interaction-plan',
      traceSeq: 5,
      sourceId: 'plan-interrupt',
      graphNamespace: [],
      runId: 'run-1',
      kind: 'tinkerfin:plan_clarification',
      toolCallIds: [],
      status: 'pending',
      payloadOmitted: false,
      payload: {
        schema: 'tinkerfin.runtime-interrupt',
        kind: 'tinkerfin:plan_clarification',
        message: '回答问题',
        responseSchema: { type: 'object' },
        metadata: {
          origin: 'plan',
          clarification: {
            form: {
              title: '确认范围',
              description: '补充执行范围',
              questions: [{
                id: 'scope',
                answerType: 'text',
                prompt: '请输入范围',
                required: true,
              }],
            },
          },
        },
      },
      openedAt: '2026-08-28T00:00:04Z',
      resolvedAt: null,
    }]

    const restored = restoreConversationFromTrace(source, { model: 'fallback', includeTaskTrace: true })

    expect(restored.planInteraction).toMatchObject({
      kind: 'questions',
      interruptId: 'plan-interrupt',
      title: '确认范围',
    })
    expect(restored.pendingInteractionKind).toBe('plan_clarification')
    expect(restored.runStatus).toBe('waiting_approval')
  })
})


it('较旧Trace快照中的较新标题独立合并，不回退正文或运行状态', () => {
  const original = detail()
  const current = restoreConversationFromTrace({ ...original, observedAt: '2026-09-05T00:00:00.000002Z' }, { model: 'main', includeTaskTrace: true })
  const result = restoreConversationFromTrace({ ...original, observedAt: '2026-09-05T00:00:00.000001Z', title: '用户新标题', titleSource: 'user', titleGenerationStatus: 'skipped', titleSeq: 3 }, { previous: current, model: 'main', includeTaskTrace: true })
  expect(result.title).toBe('用户新标题')
  expect(result.messages).toBe(current.messages)
  expect(result.trace).toBe(current.trace)
  expect(result.runStatus).toBe(current.runStatus)
})

describe('运行失败历史反馈', () => {
  it('未知运行结果不合成失败消息', () => {
    const source = detail()
    source.status.execution = 'unknown'
    const restored = restoreConversationFromTrace(source, { model: 'main', includeTaskTrace: false })
    expect(restored.messages.some(message => message.role === 'error')).toBe(false)
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

  it('省略审批内容不从保留的原生payload猜测可提交动作', () => {
    const snapshot = detail()
    snapshot.status.execution = 'waiting'
    snapshot.interactions = [{
      id: 'omitted', traceSeq: 5, sourceId: 'native-review', graphNamespace: [], runId: 'run-1',
      kind: 'tool_approval', toolCallIds: ['call-write'], status: 'pending', payloadOmitted: true,
      openedAt: '2026-08-28T00:00:04Z', agui: null,
      payload: { action_requests: [{ name: 'write_file', args: {} }], review_configs: [] },
    }]
    const conversation = restoreConversationFromTrace(snapshot, { model: 'main', includeTaskTrace: true })
    expect(conversation.pendingInteractionKind).toBe('input_required')
    expect(conversation.approval).toBeUndefined()
  })

  it.each<{ request: JsonValue; requestOmitted: boolean }>([
    { request: null, requestOmitted: true },
    { request: { '/description': '分析门店数据' }, requestOmitted: false },
    { request: { '/subagent_type': 'researcher' }, requestOmitted: false },
    { request: { description: '分析门店数据' }, requestOmitted: false },
  ])('子智能体恢复后补全真实来源，已知来源矛盾必须拒绝 %#', (capture) => {
    const snapshot = detail()
    snapshot.graph.nodes = [{
      ...snapshot.graph.nodes[0]!, id: 'native-child', kind: 'subagent', name: 'researcher',
      graphNamespace: ['tools:opaque:task'], sourceId: 'native-parent-call', ...capture,
      agui: { kind: 'subagent', parentToolCallId: 'public-parent-call', subagentInvocationId: 'subagent-11111111-1111-5111-8111-111111111111' },
      status: 'waiting', completedAt: null,
    }]
    const restored = restoreConversationFromTrace(snapshot, { model: 'main', includeTaskTrace: true })
    const event: ConversationAgUiEvent = {
      type: 'RAW', source: 'langgraph.tasks', rawEvent: { type: 'tasks', phase: 'start', ns: [] },
      event: { data: { id: 'opaque:task', name: 'tools' }, provenance: {
        kind: 'root', graphNamespace: [], agentType: 'main', agentName: 'main', subagents: [{
          schema: 'tinkerfin.subagent-provenance',
          subagentInvocationId: 'subagent-11111111-1111-5111-8111-111111111111', parentToolCallId: 'public-parent-call',
          graphNamespace: ['tools:opaque:task'], parentGraphNamespace: [], graphTaskId: 'opaque:task',
          agentName: 'researcher', description: '分析门店数据', requestRunId: 'run-resume',
        }],
      } },
    }
    let conversation = applyConversationEvent(restored, event)
    conversation = applyConversationEvent(conversation, event)
    const children = conversation.messages.filter(message => message.role === 'subagent')
    expect(children).toHaveLength(1)
    expect(children[0]).toMatchObject({ id: 'native-child', meta: {
      subRunId: 'subagent-11111111-1111-5111-8111-111111111111', graphTaskId: 'opaque:task', input: '分析门店数据', originMainRunId: 'run-1', lastMainRunId: 'run-resume',
    } })
    const conflicts = conversation.messages.map(message => message.role === 'subagent'
      ? { ...message, meta: { ...message.meta, graphTaskId: 'different-task' } } : message)
    expect(() => applyConversationEvent({ ...conversation, messages: conflicts }, event)).toThrow('身份冲突')
    const conflictingInput = conversation.messages.map(message => message.role === 'subagent'
      ? { ...message, meta: { ...message.meta, input: '另一个任务' } } : message)
    expect(() => applyConversationEvent({ ...conversation, messages: conflictingInput }, event)).toThrow('身份冲突')
    const source = {
      kind: 'deep_agent_subagent' as const, agentType: 'subagent' as const, agentName: 'researcher',
      graphNamespace: ['tools:opaque:task'], subagentInvocationId: 'subagent-11111111-1111-5111-8111-111111111111',
    }
    conversation = applyConversationEvent(conversation, {
      type: 'TOOL_CALL_START', toolCallId: 'public-child-read', toolCallName: 'read_file',
      rawEvent: { source, runId: 'run-resume' },
    })
    conversation = applyConversationEvent(conversation, {
      type: 'TOOL_CALL_RESULT', toolCallId: 'public-child-read', messageId: 'child-result', role: 'tool', content: '门店分析完成',
      rawEvent: { source, runId: 'run-resume' },
    })
    expect(conversation.messages.find(message => message.meta?.toolCallId === 'public-child-read')).toMatchObject({
      role: 'tool', meta: { runId: 'subagent-11111111-1111-5111-8111-111111111111', status: 'completed', result: '门店分析完成' },
    })
  })
})


it.each(['runtime_error', 'cancelled'])('恢复的父委派工具与子智能体共同结算 %s', (code) => {
  const snapshot = detail()
  snapshot.status.execution = 'running'
  snapshot.graph.nodes = [
    { ...snapshot.graph.nodes[0]!, id: 'parent-task', kind: 'tool', name: 'task', status: 'running', completedAt: null,
      agui: { kind: 'tool', toolCallId: 'public-delegate' } },
    { ...snapshot.graph.nodes[0]!, id: 'child-task', kind: 'subagent', name: 'researcher', status: 'running', completedAt: null,
      graphNamespace: ['tools:child-task'], sourceId: 'delegate',
      agui: { kind: 'subagent', parentToolCallId: 'public-delegate', subagentInvocationId: 'subagent-11111111-1111-5111-8111-111111111111' } },
  ]
  let conversation = restoreConversationFromTrace(snapshot, { model: 'main', includeTaskTrace: true })
  conversation = applyConversationEvent(conversation, {
    type: 'RUN_ERROR', code, message: '执行已结束', rawEvent: { runId: snapshot.headRunId },
  })
  const executions = conversation.messages.filter(message => message.role === 'tool' || message.role === 'subagent')
  expect(executions).toHaveLength(2)
  expect(executions.every(message => message.meta?.status === (code === 'cancelled' ? 'cancelled' : 'failed'))).toBe(true)
})
