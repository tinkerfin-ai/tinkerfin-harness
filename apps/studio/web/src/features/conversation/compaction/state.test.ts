import { describe, expect, it } from 'vitest'
import { buildEmptyConversation } from '../../../lib/workspace'
import { traceGraphNode } from '../../../test/traceFixtures'
import { applyConversationEvent } from '../agui/runtime'
import { buildConversationDisplayEntries } from '../todoTrace/displayEntries'
import { beginCompaction, compactionsFromTrace } from './state'

const base = () => ({ ...buildEmptyConversation({ threadId: 't', now: '2026-09-21T00:00:00Z' }),
  messages: [{ id: 'answer', role: 'assistant' as const, content: '原回复', createdAt: '2026-09-21T00:00:00Z', meta: { runId: 'chat' } }],
  todos: [{ id: 'todo', content: '待办', status: 'running' as const }],
})

const saving = { type: 'RAW' as const, source: 'langgraph.custom', event: { data: { operation: 'context_compaction', phase: 'saving', runId: 'compact' } } }
const result = { run_id: 'compact', status: 'compacted', summary: '真实摘要', compacted_messages: 3 }

describe('上下文压缩操作', () => {
  it('实时保存结果独立于聊天和 Todo，重复快照只更新同一操作', () => {
    const original = base()
    let current = beginCompaction(original, 'compact')
    current = applyConversationEvent(current, saving)
    expect(current.compactions?.[0]?.status).toBe('saving')
    for (let count = 0; count < 2; count++) current = applyConversationEvent(current, { type: 'STATE_SNAPSHOT', snapshot: { context_compaction: result } })
    current = applyConversationEvent(current, { type: 'RUN_FINISHED', threadId: 't', runId: 'compact' })
    expect(current.messages).toEqual(original.messages)
    expect(current.todos).toEqual(original.todos)
    expect(current.compactions).toHaveLength(1)
    expect(current.compactions?.[0]).toMatchObject({ status: 'compacted', summary: '真实摘要', compactedMessages: 3 })
  })

  it.each([false, true])('取消时不改写待办或聊天；保存中=%s', (duringSave) => {
    const original = base()
    let current = beginCompaction(original, 'compact')
    if (duringSave) current = applyConversationEvent(current, saving)
    current = applyConversationEvent(current, { type: 'RUN_ERROR', code: 'cancelled', message: 'cancelled' })
    expect(current.messages).toEqual(original.messages)
    expect(current.todos).toEqual(original.todos)
    expect(current.compactions?.[0]?.status).toBe(duringSave ? 'unconfirmed' : 'cancelled')
  })

  it('历史使用 Trace 顺序定位卡片，保存阶段和实际摘要在重新加载后可恢复', () => {
    const original = base()
    const chat = traceGraphNode({ id: 'chat', runId: 'chat', startedSeq: 1 })
    const operation = traceGraphNode({ id: 'compact', runId: 'compact', name: 'context_compaction', kind: 'custom', contextKind: 'compaction', compactionOrigin: 'manual', startedSeq: 10, status: 'running', completedAt: null })
    operation.result = { status: 'saving', summary: '真实摘要' }
    const pending = compactionsFromTrace([chat, operation], original.messages)
    expect(pending[0]).toMatchObject({ status: 'saving', afterMessageId: 'answer' })
    const unconfirmed = compactionsFromTrace([chat, { ...operation, status: 'failed' }], original.messages)
    expect(unconfirmed[0]?.status).toBe('unconfirmed')
    const restored = compactionsFromTrace([chat, { ...operation, result, status: 'succeeded' }], original.messages)
    expect(restored[0]).toMatchObject({ status: 'compacted', summary: '真实摘要', compactedMessages: 3 })
    const entries = buildConversationDisplayEntries({ ...original, compactions: restored, messages: [...original.messages, { id: 'next', role: 'user', content: '继续', createdAt: '2026-09-22T00:00:00Z' }] })
    expect(entries.map(entry => entry.type)).toEqual(['message', 'compaction', 'message'])
  })
})
