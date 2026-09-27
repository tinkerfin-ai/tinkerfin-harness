import { act, renderHook } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { buildEmptyConversation } from '../../lib/workspace'
import type { WorkspaceState } from '../../types'
import type { ConversationTitleSnapshot } from '../../api/conversation/titles'
import { fetchConversationTitle } from '../../api/conversation/titles'
import { useConversationTitles } from './useConversationTitles'
import { mockResourceNotices } from '../../test/resourceNotices'

vi.mock('../../api/conversation/titles', () => ({ fetchConversationTitle: vi.fn() }))
const fetchTitle = vi.mocked(fetchConversationTitle)
const title = (threadId: string, completed = false): ConversationTitleSnapshot => ({ threadId, title: completed ? '总结标题' : '临时标题', titleSource: completed ? 'generated' : 'default', titleGenerationStatus: completed ? 'succeeded' : 'running', titleSeq: completed ? 2 : 1 })
function useHarness(threadIds = ['a', 'b']) {
  const [workspace, setWorkspace] = useState<WorkspaceState>({ currentThreadId: 'a', conversations: threadIds.map(threadId => ({ ...buildEmptyConversation({ threadId, now: '2026-09-20T00:00:00Z' }), ...title(threadId), runStatus: 'streaming' })) })
  useConversationTitles(workspace, setWorkspace)
  return { workspace, setWorkspace }
}
let notices: ReturnType<typeof mockResourceNotices>
beforeEach(() => { vi.useFakeTimers(); fetchTitle.mockReset(); notices = mockResourceNotices() })
afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks() })

it('主回复结束和切换会话后仍接收标题变化，完成后停止校准且不改正文', async () => {
  fetchTitle.mockImplementation(async threadId => title(threadId))
  const { result } = await act(async () => renderHook(useHarness))
  await act(async () => result.current.setWorkspace(state => ({ ...state, currentThreadId: 'b', conversations: state.conversations.map(item => ({ ...item, runStatus: 'idle' })) })))
  fetchTitle.mockImplementation(async threadId => title(threadId, true))
  await act(async () => notices.resync())
  expect(result.current.workspace.currentThreadId).toBe('b')
  expect(result.current.workspace.conversations.map(item => item.title)).toEqual(['总结标题', '总结标题'])
  expect(result.current.workspace.conversations.every(item => item.messages.length === 0)).toBe(true)
  expect(fetchTitle).toHaveBeenCalledTimes(4)
  await act(async () => vi.advanceTimersByTimeAsync(60_000))
  expect(fetchTitle).toHaveBeenCalledTimes(4)
})

it('每个会话只保留一个查询，手动命名、删除和卸载拒绝迟到结果', async () => {
  const replies = new Map<string, (value: ConversationTitleSnapshot) => void>()
  const signals = new Map<string, AbortSignal>()
  fetchTitle.mockImplementation((threadId, signal) => {
    signals.set(threadId, signal)
    return new Promise(resolve => replies.set(threadId, resolve))
  })
  const { result, unmount } = renderHook(useHarness)
  await act(async () => vi.advanceTimersByTimeAsync(5000))
  expect(fetchTitle).toHaveBeenCalledTimes(2)
  await act(async () => result.current.setWorkspace(state => ({ ...state, conversations: state.conversations.filter(item => item.threadId === 'a').map(item => ({ ...item, title: '手动标题', titleSource: 'user', titleGenerationStatus: 'skipped', titleSeq: 3 })) })))
  expect(signals.get('b')?.aborted).toBe(true)
  await act(async () => { for (const [id, reply] of replies) reply(title(id, true)) })
  expect(result.current.workspace.conversations.map(item => item.title)).toEqual(['手动标题'])
  unmount()
  expect(vi.getTimerCount()).toBe(0)
})

it('相同标题和过期快照不提交工作区更新', async () => {
  let reply!: (value: ConversationTitleSnapshot) => void
  fetchTitle.mockImplementationOnce(() => new Promise(resolve => { reply = resolve }))
  const { result, unmount } = renderHook(() => useHarness(['a']))
  const initial = result.current.workspace
  await act(async () => reply(title('a')))
  expect(result.current.workspace).toBe(initial)
  fetchTitle.mockResolvedValue({ ...title('a', true), titleSeq: 0 })
  await act(async () => notices.resync())
  expect(result.current.workspace).toBe(initial)
  expect(fetchTitle).toHaveBeenCalledTimes(2)
  expect(vi.getTimerCount()).toBe(1)
  unmount()
  expect(vi.getTimerCount()).toBe(0)
})

it('隐藏页面关闭读取并拒绝迟到标题，重新可见立即查询', async () => {
  const visibility = vi.spyOn(document, 'hidden', 'get').mockReturnValue(false)
  let reply!: (value: ConversationTitleSnapshot) => void
  let signal!: AbortSignal
  fetchTitle.mockImplementationOnce((_threadId, currentSignal) => {
    signal = currentSignal
    return new Promise(resolve => { reply = resolve })
  }).mockResolvedValue(title('a'))
  const { result, unmount } = await act(async () => renderHook(() => useHarness(['a'])))
  visibility.mockReturnValue(true)
  act(() => document.dispatchEvent(new Event('visibilitychange')))
  expect(signal.aborted).toBe(true)
  await act(async () => { reply(title('a', true)); await vi.advanceTimersByTimeAsync(30_000) })
  expect(result.current.workspace.conversations[0]?.title).toBe('临时标题')
  expect(fetchTitle).toHaveBeenCalledTimes(1)
  expect(vi.getTimerCount()).toBe(0)

  visibility.mockReturnValue(false)
  await act(async () => document.dispatchEvent(new Event('visibilitychange')))
  expect(fetchTitle).toHaveBeenCalledTimes(2)
  expect(vi.getTimerCount()).toBe(1)
  visibility.mockReturnValue(true)
  act(() => document.dispatchEvent(new Event('visibilitychange')))
  expect(vi.getTimerCount()).toBe(0)
  fetchTitle.mockResolvedValue(title('a', true))
  visibility.mockReturnValue(false)
  await act(async () => document.dispatchEvent(new Event('visibilitychange')))
  expect(result.current.workspace.conversations[0]?.title).toBe('总结标题')
  expect(fetchTitle).toHaveBeenCalledTimes(3)
  await act(async () => vi.advanceTimersByTimeAsync(60_000))
  expect(fetchTitle).toHaveBeenCalledTimes(3)
  unmount()
})

it('隐藏状态挂载不读取，显示后补查所有仍需标题的会话', async () => {
  const visibility = vi.spyOn(document, 'hidden', 'get').mockReturnValue(true)
  fetchTitle.mockImplementation(async threadId => title(threadId, true))
  const { result, unmount } = renderHook(() => useHarness())
  await act(async () => vi.advanceTimersByTimeAsync(30_000))
  expect(fetchTitle).not.toHaveBeenCalled()
  visibility.mockReturnValue(false)
  await act(async () => document.dispatchEvent(new Event('visibilitychange')))
  expect(result.current.workspace.conversations.every(item => item.title === '总结标题')).toBe(true)
  expect(fetchTitle).toHaveBeenCalledTimes(2)
  await act(async () => vi.advanceTimersByTimeAsync(60_000))
  expect(fetchTitle).toHaveBeenCalledTimes(2)
  unmount()
})

it('通知失败后靠三十秒校准恢复，终态不再空查但仍接受手动标题通知', async () => {
  fetchTitle.mockRejectedValue(new TypeError('network'))
  const { result, unmount } = await act(async () => renderHook(() => useHarness(['a'])))
  await act(async () => vi.advanceTimersByTimeAsync(29_999))
  expect(fetchTitle).toHaveBeenCalledTimes(1)
  fetchTitle.mockResolvedValue(title('a', true))
  await act(async () => vi.advanceTimersByTimeAsync(1))
  expect(fetchTitle).toHaveBeenCalledTimes(2)
  expect(result.current.workspace.conversations[0]?.title).toBe('总结标题')
  await act(async () => vi.advanceTimersByTimeAsync(60_000))
  expect(fetchTitle).toHaveBeenCalledTimes(2)
  fetchTitle.mockResolvedValue({ ...title('a', true), title: '手动标题', titleSource: 'user', titleSeq: 3 })
  await act(async () => notices.changed('studio.conversation.title.changed', 'a'))
  expect(result.current.workspace.conversations[0]?.title).toBe('手动标题')
  unmount()
  expect(vi.getTimerCount()).toBe(0)
})
