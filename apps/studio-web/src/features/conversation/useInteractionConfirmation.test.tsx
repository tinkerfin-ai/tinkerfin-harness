import { act, renderHook } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { fetchConversationHistoryDetail, type ConversationHistoryDetail } from '../../api/conversation/history'
import { buildEmptyConversation } from '../../lib/workspace'
import type { Conversation } from '../../types'
import { mockResourceNotices } from '../../test/resourceNotices'
import { emptyTraceGraph } from '../../test/traceFixtures'
import { applyInteractionConfirmation, markInteractionRequestRejected } from './interactionConfirmation'
import { useInteractionConfirmation } from './useInteractionConfirmation'

vi.mock('../../api/conversation/history', async original => ({ ...await original<typeof import('../../api/conversation/history')>(), fetchConversationHistoryDetail: vi.fn() }))
const initial = (): Conversation => ({ ...buildEmptyConversation({ projectId: 'project', threadId: 'thread', now: '2026-10-10T00:00:00Z' }),
  runStatus: 'streaming', activeRunId: 'resume', lastSeq: 42,
  messages: [{ id: 'live', role: 'assistant', content: '已经开始执行', createdAt: '2026-10-10T00:00:00Z', meta: { runId: 'resume' } }],
  planInteraction: { kind: 'review', interruptId: 'review', revision: 1, submitted: true, submissionRunId: 'resume', action: 'approve', allowedActions: ['approve', 'reject'],
    draft: { revision: 1, contentSchema: { mediaType: 'text/markdown', fingerprint: 'a'.repeat(64) }, content: { description: '加字计划', markdown: '# 计划' } } },
})
const detail = (state: 'confirming' | 'resolved' | 'available' | 'cancelled' = 'resolved'): ConversationHistoryDetail => ({
  id: 1, projectId: 'project', archived: false, threadId: 'thread', title: '计划', pinned: false, accessMode: 'full',
  titleSource: 'default', titleGenerationStatus: 'idle', titleSeq: 0,
  asOfSeq: 1, generation: 'generation', observedAt: '2026-10-10T00:00:00Z', headRunId: 'previous', availableHeads: ['previous'],
  messages: [], reasoning: [], runFailures: [], messageCount: 0, toolCallCount: 0,
  graph: emptyTraceGraph(1),
  state: { root: {}, subgraphs: {} }, status: { execution: 'waiting', headRunId: 'previous' }, completeness: { missingPrefix: false, missingTail: false, payloadOmitted: false },
  taskTrace: null, createdAt: '2026-10-10T00:00:00Z', updatedAt: '2026-10-10T00:00:00Z',
  interactionAvailability: [{ interruptId: 'review', submissionRunId: state === 'available' ? null : 'resume', state }],
  submissionResult: state === 'available' ? { submissionRunId: 'resume', interruptIds: ['review'], state: 'not_saved' } : null,
  planResults: state === 'resolved' ? [{ interruptId: 'review', submissionRunId: 'resume', outcome: 'approved', answers: null, reason: null }] : [],
  interactions: [{ id: 'plan', traceSeq: 1, sourceId: 'review', graphNamespace: [], runId: 'previous', kind: 'tinkerfin:plan_review', toolCallIds: [], status: 'pending', payloadOmitted: false, openedAt: '2026-10-10T00:00:00Z',
    agui: [{ id: 'review', reason: 'tinkerfin:plan_review', responseSchema: {}, metadata: {} }] }],
})
let notices: ReturnType<typeof mockResourceNotices>
beforeEach(() => { vi.useFakeTimers(); notices = mockResourceNotices(); vi.mocked(fetchConversationHistoryDetail).mockReset() })
afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks() })

it('保存确认在运行结束前移除卡片，正文、游标和运行状态保持实时值', () => {
  const current = initial()
  const confirmed = applyInteractionConfirmation(current, detail(), 'resume')
  expect(confirmed.planInteraction).toBeUndefined()
  expect(confirmed.runStatus).toBe('streaming')
  expect(confirmed.activeRunId).toBe('resume')
  expect(confirmed.lastSeq).toBe(42)
  expect(confirmed.messages.at(-1)).toBe(current.messages[0])
  expect(confirmed.messages[0].meta?.planResult?.outcome).toBe('approved')
  expect(applyInteractionConfirmation(confirmed, detail(), 'resume')).toBe(confirmed)
})

it('缺失或其他提交的确认不能清除卡片，明确未保存才恢复原输入', () => {
  const current = initial()
  const missing = { ...detail(), interactionAvailability: [] }
  expect(applyInteractionConfirmation(current, missing, 'resume')).toBe(current)
  expect(applyInteractionConfirmation(current, detail(), 'old-resume')).toBe(current)
  expect(applyInteractionConfirmation(current, detail('confirming'), 'resume')).toBe(current)
  const restored = applyInteractionConfirmation(current, detail('available'), 'resume')
  expect(restored.planInteraction).toMatchObject({ submitted: false, action: 'approve', draft: current.planInteraction!.kind === 'review' && current.planInteraction!.draft })
  expect(restored.messages).toBe(current.messages)
})

it('通知与正在进行的基线读取交错时，追加读取并及时收束', async () => {
  let release!: (value: ConversationHistoryDetail) => void
  vi.mocked(fetchConversationHistoryDetail).mockImplementationOnce(() => new Promise(resolve => { release = resolve })).mockResolvedValue(detail())
  const { result } = renderHook(() => {
    const [conversation, setConversation] = useState(initial)
    useInteractionConfirmation(conversation, (_id, updater) => setConversation(updater))
    return conversation
  })
  act(() => notices.changed('studio.conversation.interactions.changed', 'thread'))
  await act(async () => release(detail('confirming')))
  expect(fetchConversationHistoryDetail).toHaveBeenCalledTimes(2)
  expect(result.current.planInteraction).toBeUndefined()
  expect(result.current.messages.at(-1)?.content).toBe('已经开始执行')
})

it('读取失败保留决定，重新检查只读状态，卸载后取消旧请求', async () => {
  vi.mocked(fetchConversationHistoryDetail).mockRejectedValueOnce(new Error('offline')).mockResolvedValue(detail())
  const update = vi.fn()
  const { result, unmount } = renderHook(() => useInteractionConfirmation(initial(), update))
  await act(async () => {})
  expect(result.current.failed).toBe(true)
  expect(update).not.toHaveBeenCalled()
  await act(async () => result.current.retry())
  expect(update).toHaveBeenCalledOnce()
  expect(result.current.failed).toBe(false)
  const signal = vi.mocked(fetchConversationHistoryDetail).mock.calls.at(-1)![1].signal
  unmount()
  expect(signal?.aborted).toBe(true)
  act(() => notices.changed('studio.conversation.interactions.changed', 'thread'))
  expect(fetchConversationHistoryDetail).toHaveBeenCalledTimes(2)
})

it('审批组全部确认后才收束，部分确认不开放重复提交', () => {
  const current = { ...initial(), planInteraction: undefined, approval: {
    submitted: true, submissionRunId: 'resume', activeIndex: 0,
    items: ['first', 'second'].map(id => ({ id, interruptId: id, toolName: 'write_file', params: '{}', input: '', description: '', originalArgs: {}, allowedDecisions: ['approve' as const] })),
  } }
  const partial = { ...detail(), interactionAvailability: [
    { interruptId: 'first', submissionRunId: 'resume', state: 'resolved' as const },
    { interruptId: 'second', submissionRunId: 'resume', state: 'confirming' as const },
  ] }
  expect(applyInteractionConfirmation(current, partial, 'resume')).toBe(current)
  const completed = applyInteractionConfirmation(current, { ...partial, interactionAvailability: partial.interactionAvailability.map(item => ({ ...item, state: 'resolved' })) }, 'resume')
  expect(completed.approval).toBeUndefined()
  expect(completed.messages).toBe(current.messages)
})

it('切换会话使在途确认失效，迟到结果不能写回新会话', async () => {
  let release!: (value: ConversationHistoryDetail) => void
  vi.mocked(fetchConversationHistoryDetail).mockImplementationOnce(() => new Promise(resolve => { release = resolve })).mockResolvedValue({ ...detail(), threadId: 'other' })
  const update = vi.fn()
  const { rerender } = renderHook(({ conversation }) => useInteractionConfirmation(conversation, update), { initialProps: { conversation: initial() } })
  const signal = vi.mocked(fetchConversationHistoryDetail).mock.calls[0][1].signal
  await act(async () => rerender({ conversation: { ...initial(), threadId: 'other' } }))
  expect(signal?.aborted).toBe(true)
  await act(async () => release(detail()))
  expect(update).toHaveBeenCalledTimes(1)
  expect(update.mock.calls[0][0]).toBe('other')
})

it('明确未保存的结果独立于当前空闲权限，保留输入后解锁', () => {
  const current = initial()
  const saved = { ...detail('available'),
    interactionAvailability: [{ interruptId: 'review', state: 'available' as const, submissionRunId: null }],
    submissionResult: { submissionRunId: 'resume', interruptIds: ['review'], state: 'not_saved' as const },
  }
  const restored = applyInteractionConfirmation(current, saved, 'resume')
  expect(restored.planInteraction).toMatchObject({ submitted: false, submissionRunId: undefined, action: 'approve', error: '提交未保存，请重试' })
  expect(restored.messages).toBe(current.messages)
  expect(restored.lastSeq).toBe(42)
})

it('原提交未保存后采用另一提交的当前认领，迟到原提交不能覆盖新归属', () => {
  const current = initial()
  const saved = { ...detail('confirming'),
    interactionAvailability: [{ interruptId: 'review', state: 'confirming' as const, submissionRunId: 'other-resume' }],
    submissionResult: { submissionRunId: 'resume', interruptIds: ['review'], state: 'not_saved' as const },
  }
  const transferred = applyInteractionConfirmation(current, saved, 'resume')
  expect(transferred.planInteraction).toMatchObject({ submitted: true, submissionRunId: 'other-resume', action: 'approve' })
  expect(transferred.messages).toBe(current.messages)
  expect(applyInteractionConfirmation(transferred, detail(), 'resume')).toBe(transferred)
})

it.each([
  { threadId: 'other', submissionRunId: 'resume', interruptIds: ['review'] },
  { threadId: 'thread', submissionRunId: 'other-resume', interruptIds: ['review'] },
  { threadId: 'thread', submissionRunId: 'resume', interruptIds: ['other-review'] },
  { threadId: 'thread', submissionRunId: 'resume', interruptIds: ['review', 'extra'] },
])('未保存证明必须同时匹配会话、提交和完整交互组：%j', ({ threadId, submissionRunId, interruptIds }) => {
  const current = initial()
  const proof = { ...detail('available'), threadId, submissionResult: { submissionRunId, interruptIds, state: 'not_saved' as const } }
  expect(applyInteractionConfirmation(current, proof, 'resume')).toBe(current)
  expect(markInteractionRequestRejected(current, threadId, submissionRunId, interruptIds)).toBe(current)
})

it('缺失未保存证明或当前权限时保持未知，不能以空闲状态推断原提交失败', () => {
  const current = initial()
  expect(applyInteractionConfirmation(current, { ...detail('available'), submissionResult: null }, 'resume')).toBe(current)
  expect(applyInteractionConfirmation(current, { ...detail('available'), interactionAvailability: [] }, 'resume')).toBe(current)
})

it('工具组失败证明必须覆盖整组，完整证明后保留原决定并跟随新认领', () => {
  const current: Conversation = { ...initial(), planInteraction: undefined, approval: {
    submitted: true, submissionRunId: 'resume', activeIndex: 0,
    items: ['first', 'second'].map(id => ({ id, interruptId: id, toolName: 'write_file', params: '{}', input: '', description: '', originalArgs: {}, allowedDecisions: ['reject'], decision: 'rejected', rejectionReason: `保留${id}` })),
  } }
  const claimed = { ...detail('confirming'), interactionAvailability: current.approval!.items.map(item => ({
    interruptId: item.interruptId, state: 'confirming' as const, submissionRunId: 'other-resume',
  })), submissionResult: { submissionRunId: 'resume', interruptIds: ['first'], state: 'not_saved' as const } }
  expect(applyInteractionConfirmation(current, claimed, 'resume')).toBe(current)
  claimed.submissionResult.interruptIds.push('second')
  const transferred = applyInteractionConfirmation(current, claimed, 'resume')
  expect(transferred.approval).toMatchObject({ submitted: true, submissionRunId: 'other-resume' })
  expect(transferred.approval?.items).toBe(current.approval!.items)
})

it.each(['available', 'confirming'] as const)('首次请求明确拒绝后废弃旧读取，回读当前权限前保持只读：%s', async state => {
  let releaseOld!: (value: ConversationHistoryDetail) => void
  let releaseCurrent!: (value: ConversationHistoryDetail) => void
  const currentAuthority: ConversationHistoryDetail = { ...detail(state), submissionResult: null,
    interactionAvailability: [{ interruptId: 'review', state, submissionRunId: state === 'available' ? null : 'other-resume' }],
  }
  vi.mocked(fetchConversationHistoryDetail)
    .mockImplementationOnce(() => new Promise(resolve => { releaseOld = resolve }))
    .mockImplementationOnce(() => new Promise(resolve => { releaseCurrent = resolve }))
    .mockResolvedValue(currentAuthority)
  const { result } = renderHook(() => {
    const [conversation, setConversation] = useState<Conversation>(() => ({ ...initial(), messages: [] }))
    useInteractionConfirmation(conversation, (_id, updater) => setConversation(updater))
    return { conversation, reject: () => setConversation(current => markInteractionRequestRejected(current, 'thread', 'resume', ['review'])) }
  })
  const oldSignal = vi.mocked(fetchConversationHistoryDetail).mock.calls[0][1].signal
  act(() => result.current.reject())
  expect(oldSignal?.aborted).toBe(true)
  expect(result.current.conversation.planInteraction).toMatchObject({ submitted: true, submissionRunId: 'resume', requestRejected: true })
  await act(async () => releaseOld({ ...detail('available'), submissionResult: null }))
  expect(result.current.conversation.planInteraction?.submitted).toBe(true)
  expect(fetchConversationHistoryDetail).toHaveBeenCalledTimes(2)
  await act(async () => releaseCurrent(currentAuthority))
  expect(result.current.conversation.planInteraction).toMatchObject({
    submitted: state !== 'available', submissionRunId: state === 'available' ? undefined : 'other-resume',
    requestRejected: undefined, action: 'approve',
  })
  if (state === 'available') expect(result.current.conversation.planInteraction?.error).toBe('提交未保存，请重试')
  else expect(vi.mocked(fetchConversationHistoryDetail).mock.calls.at(-1)![1].submissionRunId).toBe('other-resume')
})
