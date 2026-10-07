import { act, cleanup, renderHook } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, expect, it, vi } from 'vitest'
import { patchConversation, type ConversationHistoryListItem } from '../../api/conversation/history'
import { buildEmptyConversation } from '../../lib/workspace'
import { useProjectConversationActions } from './useProjectConversationActions'

vi.mock('../../api/conversation/history', () => ({ patchConversation: vi.fn() }))
const first = buildEmptyConversation({ projectId: 'project', threadId: 'first', now: '2030-01-01', model: 'main' })
const second = { ...first, threadId: 'second' }
const summary: ConversationHistoryListItem = {
  id: 1, threadId: 'first', projectId: 'project', archived: true, title: '第一条', titleSource: 'user', titleGenerationStatus: 'skipped', titleSeq: 1,
  accessMode: 'full', status: 'idle', lastRunId: 'run', lastModel: 'main', messageCount: 0, toolCallCount: 0,
  hasPendingInterrupt: false, pendingInteractionKind: null, pinned: false, createdAt: '2030-01-01', updatedAt: '2030-01-01',
}
afterEach(() => { cleanup(); vi.resetAllMocks() })

it.each([false, true])('归档结果不关闭其他会话的移动窗口或解除其等待：已提交=%s', async submitMove => {
  let finishArchive!: (value: ConversationHistoryListItem) => void
  let finishMove!: (value: ConversationHistoryListItem) => void
  vi.mocked(patchConversation).mockImplementationOnce(() => new Promise(resolve => { finishArchive = resolve }))
    .mockImplementationOnce(() => new Promise(resolve => { finishMove = resolve }))
  const { result } = renderHook(() => {
    const [workspace, setWorkspace] = useState({ conversations: [first, second], currentThreadId: '' })
    return useProjectConversationActions(workspace, setWorkspace, vi.fn())
  })
  act(() => result.current.archive('first'))
  act(() => result.current.move('second', document.createElement('button')))
  if (submitMove) act(() => result.current.confirmMove('destination'))
  await act(async () => finishArchive(summary))
  expect(result.current.moving?.conversation.threadId).toBe('second')
  expect(result.current.pending).toBe(submitMove)
  if (submitMove) {
    act(() => result.current.close())
    expect(result.current.moving?.conversation.threadId).toBe('second')
    await act(async () => finishMove({ ...summary, threadId: 'second', projectId: 'destination', archived: false }))
    expect(result.current.moving).toBeNull()
  }
})

it('草稿无法安全移动时保留输入并且不修改会话归属', async () => {
  const { result } = renderHook(() => {
    const [workspace, setWorkspace] = useState({ conversations: [first], currentThreadId: first.threadId })
    return useProjectConversationActions(workspace, setWorkspace, vi.fn(), () => { throw new Error('请重新选择附件') })
  })
  act(() => result.current.move('first', document.createElement('button')))
  await act(async () => result.current.confirmMove('destination'))
  expect(patchConversation).not.toHaveBeenCalled()
  expect(result.current.moving?.conversation.threadId).toBe('first')
  expect(result.current.pending).toBe(false)
  expect(result.current.error).toBe('请重新选择附件')
})
