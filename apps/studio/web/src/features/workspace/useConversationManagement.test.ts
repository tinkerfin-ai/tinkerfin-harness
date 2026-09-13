import { act, renderHook, waitFor } from '@testing-library/react'
import { useRef, useState } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { ConversationHistoryListItem } from '../../api/conversation/history'
import { buildEmptyConversation } from '../../lib/workspace'
import type { WorkspaceState } from '../../types'
import { useConversationManagement } from './useConversationManagement'

const historyMocks = vi.hoisted(() => ({
  patch: vi.fn(),
  remove: vi.fn(),
}))

vi.mock('../../api/conversation/history', () => ({
  patchConversation: historyMocks.patch,
  deleteConversation: historyMocks.remove,
}))

function deferred<T>() {
  let resolve: (value: T) => void = () => undefined
  let reject: (reason?: unknown) => void = () => undefined
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

const summary = (pinned: boolean): ConversationHistoryListItem => ({ accessMode: 'write_approval',
  titleSource: 'default',
  titleGenerationStatus: 'idle',
  titleSeq: 0,
  id: 1,
  threadId: 'thread-pin',
  title: '置顶会话',
  status: 'idle',
  lastRunId: 'run-pin',
  lastModel: 'GPT-5.5',
  messageCount: 0,
  toolCallCount: 0,
  hasPendingInterrupt: false,
  pendingInteractionKind: null,
  pinned,
  createdAt: '2026-08-25T00:00:00.000Z',
  updatedAt: '2026-08-25T00:00:00.000Z',
})

function useHarness() {
  const conversation = buildEmptyConversation({
    threadId: 'thread-pin',
    now: '2026-08-25T00:00:00.000Z',
    model: 'GPT-5.5',
  })
  const [workspace, setWorkspace] = useState<WorkspaceState>({
    conversations: [conversation],
    currentThreadId: conversation.threadId,
  })
  const onToast = useRef(vi.fn()).current
  const management = useConversationManagement({
    workspace,
    conversation: workspace.conversations[0] ?? conversation,
    setWorkspace,
    setDraft: vi.fn(),
    setDraftConversation: vi.fn(),
    setDraftModel: vi.fn(),
    followDetachedConversation: vi.fn(async () => undefined),
    abandonPlanInteraction: vi.fn(),
    cancelRun: vi.fn(async () => false),
    isActiveThread: vi.fn(() => false),
    onToast,
    onConversationBoundary: vi.fn(),
  })
  return { management, workspace, onToast }
}

describe('useConversationManagement pin ownership', () => {
  beforeEach(() => vi.clearAllMocks())

  it('重命名失败保留对话框和输入值，提示一次后允许重试', async () => {
    historyMocks.patch.mockRejectedValueOnce(new Error('修改失败，请重试')).mockResolvedValueOnce({ ...summary(false), title: '修改后的标题', titleSource: 'user', titleGenerationStatus: 'skipped', titleSeq: 1 })
    const {result, rerender} = renderHook(useHarness)
    act(() => result.current.management.renameConversation('thread-pin'))
    await act(() => result.current.management.confirmDialog('修改后的标题'))
    expect(result.current.management.dialog?.kind).toBe('rename')
    expect(result.current.management.dialogPending).toBe(false)
    expect(result.current.onToast).toHaveBeenCalledExactlyOnceWith('error', '修改失败，请重试')
    rerender()
    expect(result.current.onToast).toHaveBeenCalledOnce()
    await act(() => result.current.management.confirmDialog('修改后的标题'))
    expect(result.current.management.dialog).toBeNull()
    expect(result.current.workspace.conversations[0]?.title).toBe('修改后的标题')
  })
  it('同名保存固定标题且32个Unicode字符可提交', async () => {
    historyMocks.patch.mockResolvedValue({ ...summary(false), title: '新会话', titleSource: 'user', titleGenerationStatus: 'skipped', titleSeq: 1 })
    const { result } = renderHook(useHarness)
    act(() => result.current.management.renameConversation('thread-pin'))
    await act(() => result.current.management.confirmDialog('新会话'))
    expect(historyMocks.patch).toHaveBeenCalledWith('thread-pin', { title: '新会话' })
    act(() => result.current.management.renameConversation('thread-pin'))
    await act(() => result.current.management.confirmDialog('😀'.repeat(32)))
    expect(historyMocks.patch).toHaveBeenLastCalledWith('thread-pin', { title: '😀'.repeat(32) })
  })

  it('超长标题保留输入且不发送请求', async () => {
    const { result } = renderHook(useHarness)
    act(() => result.current.management.renameConversation('thread-pin'))
    await act(() => result.current.management.confirmDialog('中'.repeat(33)))
    expect(historyMocks.patch).not.toHaveBeenCalled()
    expect(result.current.management.dialog?.kind).toBe('rename')
    expect(result.current.onToast).toHaveBeenCalledWith('error', '会话名称最多32个字符')
  })

  it('卸载后不通知尚未结束的删除请求', async () => {
    const response = deferred<void>()
    historyMocks.remove.mockReturnValueOnce(response.promise)
    const {result, unmount} = renderHook(useHarness)
    const onToast = result.current.onToast
    act(() => result.current.management.deleteConversation('thread-pin'))
    let pending!: Promise<void>
    act(() => { pending = result.current.management.confirmDialog() })
    unmount()
    await act(async () => { response.reject(new Error('删除失败')); await pending })
    expect(onToast).not.toHaveBeenCalled()
  })
  it('deduplicates a pending mutation and applies the authoritative response', async () => {
    const response = deferred<ConversationHistoryListItem>()
    historyMocks.patch.mockReturnValueOnce(response.promise)
    const { result } = renderHook(useHarness)

    act(() => {
      result.current.management.pinConversation('thread-pin')
      result.current.management.pinConversation('thread-pin')
    })

    expect(historyMocks.patch).toHaveBeenCalledOnce()
    expect(result.current.workspace.conversations[0]?.pinned).toBe(true)
    expect(result.current.management.pinPendingThreadIds.has('thread-pin')).toBe(true)

    await act(async () => {
      response.resolve(summary(false))
      await response.promise
    })

    await waitFor(() => {
      expect(result.current.workspace.conversations[0]?.pinned).toBe(false)
      expect(result.current.management.pinPendingThreadIds.has('thread-pin')).toBe(false)
    })
    expect(result.current.onToast).not.toHaveBeenCalled()
  })

  it('rolls back only the optimistic value owned by the failed request', async () => {
    const response = deferred<ConversationHistoryListItem>()
    historyMocks.patch.mockReturnValueOnce(response.promise)
    const { result } = renderHook(useHarness)
    act(() => result.current.management.pinConversation('thread-pin'))
    expect(result.current.workspace.conversations[0]?.pinned).toBe(true)

    await act(async () => {
      response.reject(new Error('patch failed'))
      await response.promise.catch(() => undefined)
    })

    await waitFor(() => expect(result.current.workspace.conversations[0]?.pinned).toBe(false))
    expect(result.current.onToast).toHaveBeenCalledOnce()
    expect(result.current.onToast).toHaveBeenCalledWith('error', '置顶状态更新失败，请重试')
  })
})
