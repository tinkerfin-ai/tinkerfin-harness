import { act, cleanup, renderHook } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { buildEmptyConversation } from '../../lib/workspace'
import type { WorkspaceState } from '../../types'
import type { ConversationTitleSnapshot } from '../../api/conversation/titles'
import { fetchConversationTitle } from '../../api/conversation/titles'
import { mockResourceNotices } from '../../test/resourceNotices'
import { useConversationTitle } from './useConversationTitle'

vi.mock('../../api/conversation/titles', () => ({ fetchConversationTitle: vi.fn() }))
const fetchTitle = vi.mocked(fetchConversationTitle)
const title = (threadId: string, titleSeq = 1): ConversationTitleSnapshot => ({
  threadId, title: '已生成标题', titleSource: 'generated', titleGenerationStatus: 'succeeded', titleSeq,
})
function useHarness(active = true) {
  const [workspace, setWorkspace] = useState<WorkspaceState>({
    currentThreadId: 'a',
    conversations: ['a', 'b'].map(threadId => ({
      ...buildEmptyConversation({ threadId, now: '2026-09-20T00:00:00Z' }), ...title(threadId),
    })),
  })
  const selected = workspace.conversations.find(item => item.threadId === workspace.currentThreadId)
  useConversationTitle(active ? selected?.threadId : undefined, setWorkspace)
  return { workspace, setWorkspace }
}
let notices: ReturnType<typeof mockResourceNotices>
beforeEach(() => {
  vi.useFakeTimers()
  fetchTitle.mockReset()
  notices = mockResourceNotices()
})
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.useRealTimers() })

it('列表负责标题时不单独查询，启用后仅读取当前会话', async () => {
  fetchTitle.mockImplementation(async threadId => title(threadId))
  const { rerender } = renderHook(({ active }) => useHarness(active), { initialProps: { active: false } })
  await act(async () => { notices.resync(); await vi.advanceTimersByTimeAsync(60_000) })
  expect(fetchTitle).not.toHaveBeenCalled()
  await act(async () => rerender({ active: true }))
  expect(fetchTitle.mock.calls.map(([threadId]) => threadId)).toEqual(['a'])
  await act(async () => notices.changed('studio.conversation.title.changed', 'b'))
  expect(fetchTitle).toHaveBeenCalledOnce()
  fetchTitle.mockResolvedValue({ ...title('a', 2), title: '手动标题', titleSource: 'user', titleGenerationStatus: 'skipped' })
  await act(async () => notices.changed('studio.conversation.title.changed', 'a'))
  expect(fetchTitle).toHaveBeenCalledTimes(2)
})

it('相同和过期标题不提交更新，较新标题只更新元信息', async () => {
  fetchTitle.mockResolvedValue(title('a'))
  const { result } = await act(async () => renderHook(useHarness))
  const initial = result.current.workspace
  fetchTitle.mockResolvedValue({ ...title('a', 0), title: '过期标题' })
  await act(async () => notices.resync())
  expect(result.current.workspace).toBe(initial)
  fetchTitle.mockResolvedValue({ ...title('a', 3), title: '手动标题', titleSource: 'user', titleGenerationStatus: 'skipped' })
  await act(async () => notices.changed('studio.conversation.title.changed', 'a'))
  expect(result.current.workspace.conversations[0]?.title).toBe('手动标题')
  expect(result.current.workspace.conversations[0]?.messages).toBe(initial.conversations[0]?.messages)
  expect(result.current.workspace.currentThreadId).toBe('a')
  fetchTitle.mockResolvedValue({ ...title('a', 2), title: '迟到标题' })
  await act(async () => notices.resync())
  expect(result.current.workspace.conversations[0]?.title).toBe('手动标题')
})

it('切换当前会话取消原查询，迟到标题不会写入缓存', async () => {
  let reply!: (value: ConversationTitleSnapshot) => void
  fetchTitle.mockImplementationOnce(() => new Promise(resolve => { reply = resolve }))
    .mockResolvedValue({ ...title('b', 2), title: '会话乙标题' })
  const { result } = renderHook(useHarness)
  const signal = fetchTitle.mock.calls[0]![1]
  await act(async () => result.current.setWorkspace(state => ({ ...state, currentThreadId: 'b' })))
  expect(signal.aborted).toBe(true)
  await act(async () => reply({ ...title('a', 9), title: '失效查询' }))
  expect(fetchTitle.mock.calls.map(([threadId]) => threadId)).toEqual(['a', 'b'])
  expect(result.current.workspace.conversations.map(item => item.title)).toEqual(['已生成标题', '会话乙标题'])
})

it.each(['listed', 'deleted', 'unmounted'] as const)('不再负责当前会话时释放查询和通知：%s', async reason => {
  let reply!: (value: ConversationTitleSnapshot) => void
  fetchTitle.mockImplementationOnce(() => new Promise(resolve => { reply = resolve }))
  const { result, rerender, unmount } = renderHook(({ active }) => useHarness(active), { initialProps: { active: true } })
  const signal = fetchTitle.mock.calls[0]![1]
  act(() => {
    if (reason === 'listed') rerender({ active: false })
    else if (reason === 'deleted') result.current.setWorkspace(state => ({ ...state, conversations: [] }))
    else unmount()
  })
  expect(signal.aborted).toBe(true)
  const state = result.current.workspace
  await act(async () => {
    reply({ ...title('a', 9), title: '失效查询' })
    notices.resync()
    await vi.advanceTimersByTimeAsync(60_000)
  })
  expect(result.current.workspace).toBe(state)
  expect(fetchTitle).toHaveBeenCalledOnce()
  expect(vi.getTimerCount()).toBe(0)
})

it('隐藏页面取消读取并拒绝迟到标题，恢复可见后读取最新值', async () => {
  const visibility = vi.spyOn(document, 'hidden', 'get').mockReturnValue(false)
  let reply!: (value: ConversationTitleSnapshot) => void
  fetchTitle.mockImplementationOnce(() => new Promise(resolve => { reply = resolve }))
    .mockResolvedValue({ ...title('a', 2), title: '最新标题' })
  const { result } = renderHook(useHarness)
  const signal = fetchTitle.mock.calls[0]![1]
  act(() => { visibility.mockReturnValue(true); document.dispatchEvent(new Event('visibilitychange')) })
  expect(signal.aborted).toBe(true)
  await act(async () => { reply({ ...title('a', 9), title: '失效查询' }); await vi.advanceTimersByTimeAsync(30_000) })
  expect(result.current.workspace.conversations[0]?.title).toBe('已生成标题')
  expect(fetchTitle).toHaveBeenCalledOnce()
  await act(async () => { visibility.mockReturnValue(false); document.dispatchEvent(new Event('visibilitychange')) })
  expect(result.current.workspace.conversations[0]?.title).toBe('最新标题')
  expect(fetchTitle).toHaveBeenCalledTimes(2)
})

it('隐藏时挂载不查询，恢复可见后读取；超时后通过校准恢复', async () => {
  const visibility = vi.spyOn(document, 'hidden', 'get').mockReturnValue(true)
  fetchTitle.mockRejectedValueOnce(new DOMException('请求超时', 'TimeoutError'))
    .mockResolvedValue({ ...title('a', 2), title: '最新标题' })
  const { result } = renderHook(useHarness)
  await act(async () => vi.advanceTimersByTimeAsync(30_000))
  expect(fetchTitle).not.toHaveBeenCalled()
  await act(async () => { visibility.mockReturnValue(false); document.dispatchEvent(new Event('visibilitychange')) })
  expect(fetchTitle).toHaveBeenCalledOnce()
  await act(async () => vi.advanceTimersByTimeAsync(30_000))
  expect(fetchTitle).toHaveBeenCalledTimes(2)
  expect(result.current.workspace.conversations[0]?.title).toBe('最新标题')
})
