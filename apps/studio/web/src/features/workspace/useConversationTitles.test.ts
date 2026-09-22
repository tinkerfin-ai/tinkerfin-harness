import { act, renderHook } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { buildEmptyConversation } from '../../lib/workspace'
import type { WorkspaceState } from '../../types'
import type { ConversationTitleSnapshot } from '../../api/conversation/titles'
import { fetchConversationTitle } from '../../api/conversation/titles'
import { useConversationTitles } from './useConversationTitles'

vi.mock('../../api/conversation/titles', () => ({ fetchConversationTitle: vi.fn() }))
const fetchTitle = vi.mocked(fetchConversationTitle)
const title = (threadId: string, completed = false): ConversationTitleSnapshot => ({ threadId, title: completed ? '总结标题' : '临时标题', titleSource: completed ? 'generated' : 'default', titleGenerationStatus: completed ? 'succeeded' : 'running', titleSeq: completed ? 2 : 1 })
function useHarness(threadIds = ['a', 'b']) {
  const [workspace, setWorkspace] = useState<WorkspaceState>({ currentThreadId: 'a', conversations: threadIds.map(threadId => ({ ...buildEmptyConversation({ threadId, now: '2026-09-20T00:00:00Z' }), ...title(threadId), runStatus: 'streaming' })) })
  useConversationTitles(workspace, setWorkspace)
  return { workspace, setWorkspace }
}
beforeEach(() => { vi.useFakeTimers(); fetchTitle.mockReset() })
afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks() })

it('主回复结束和切到B后仍查询A，完成后停止且不改正文', async () => {
  fetchTitle.mockImplementation(async threadId => title(threadId))
  const { result } = await act(async () => renderHook(useHarness))
  await act(async () => result.current.setWorkspace(state => ({ ...state, currentThreadId: 'b', conversations: state.conversations.map(item => ({ ...item, runStatus: 'idle' })) })))
  fetchTitle.mockImplementation(async threadId => title(threadId, true))
  await act(async () => vi.advanceTimersByTimeAsync(1000))
  expect(result.current.workspace.currentThreadId).toBe('b')
  expect(result.current.workspace.conversations.map(item => item.title)).toEqual(['总结标题', '总结标题'])
  expect(result.current.workspace.conversations.every(item => item.messages.length === 0)).toBe(true)
  expect(fetchTitle).toHaveBeenCalledTimes(4)
  expect(vi.getTimerCount()).toBe(0)
  await act(async () => vi.advanceTimersByTimeAsync(5000))
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
  expect([...signals.values()].every(signal => signal.aborted)).toBe(true)
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
  await act(async () => vi.advanceTimersByTimeAsync(1000))
  expect(result.current.workspace).toBe(initial)
  expect(fetchTitle).toHaveBeenCalledTimes(2)
  expect(vi.getTimerCount()).toBe(1)
  unmount()
  expect(vi.getTimerCount()).toBe(0)
})

it('隐藏页面关闭读取并拒绝迟到标题，重新可见立即查询', async () => {
  const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible')
  let reply!: (value: ConversationTitleSnapshot) => void
  let signal!: AbortSignal
  fetchTitle.mockImplementationOnce((_threadId, currentSignal) => {
    signal = currentSignal
    return new Promise(resolve => { reply = resolve })
  }).mockResolvedValue(title('a'))
  const { result, unmount } = await act(async () => renderHook(() => useHarness(['a'])))
  visibility.mockReturnValue('hidden')
  act(() => document.dispatchEvent(new Event('visibilitychange')))
  expect(signal.aborted).toBe(true)
  await act(async () => { reply(title('a', true)); await vi.advanceTimersByTimeAsync(30_000) })
  expect(result.current.workspace.conversations[0]?.title).toBe('临时标题')
  expect(fetchTitle).toHaveBeenCalledTimes(1)
  expect(vi.getTimerCount()).toBe(0)

  visibility.mockReturnValue('visible')
  await act(async () => document.dispatchEvent(new Event('visibilitychange')))
  expect(fetchTitle).toHaveBeenCalledTimes(2)
  expect(vi.getTimerCount()).toBe(1)
  visibility.mockReturnValue('hidden')
  act(() => document.dispatchEvent(new Event('visibilitychange')))
  expect(vi.getTimerCount()).toBe(0)
  fetchTitle.mockResolvedValue(title('a', true))
  visibility.mockReturnValue('visible')
  await act(async () => document.dispatchEvent(new Event('visibilitychange')))
  expect(result.current.workspace.conversations[0]?.title).toBe('总结标题')
  expect(fetchTitle).toHaveBeenCalledTimes(3)
  expect(vi.getTimerCount()).toBe(0)
  unmount()
})

it('隐藏状态挂载不读取，显示后补查所有仍需标题的会话', async () => {
  const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden')
  fetchTitle.mockImplementation(async threadId => title(threadId, true))
  const { result, unmount } = renderHook(() => useHarness())
  await act(async () => vi.advanceTimersByTimeAsync(30_000))
  expect(fetchTitle).not.toHaveBeenCalled()
  visibility.mockReturnValue('visible')
  await act(async () => document.dispatchEvent(new Event('visibilitychange')))
  expect(result.current.workspace.conversations.every(item => item.title === '总结标题')).toBe(true)
  expect(fetchTitle).toHaveBeenCalledTimes(2)
  expect(vi.getTimerCount()).toBe(0)
  unmount()
})

it('连续错误按两倍间隔退避至三十秒，成功后恢复每秒读取并在终态停止', async () => {
  fetchTitle.mockRejectedValue(new TypeError('network'))
  const { result, unmount } = await act(async () => renderHook(() => useHarness(['a'])))
  let calls = 1
  for (const delay of [2000, 4000, 8000, 16_000, 30_000, 30_000]) {
    await act(async () => vi.advanceTimersByTimeAsync(delay - 1))
    expect(fetchTitle).toHaveBeenCalledTimes(calls)
    await act(async () => vi.advanceTimersByTimeAsync(1))
    expect(fetchTitle).toHaveBeenCalledTimes(++calls)
  }
  fetchTitle.mockResolvedValue(title('a'))
  await act(async () => vi.advanceTimersByTimeAsync(30_000))
  expect(fetchTitle).toHaveBeenCalledTimes(++calls)
  fetchTitle.mockResolvedValue(title('a', true))
  await act(async () => vi.advanceTimersByTimeAsync(999))
  expect(fetchTitle).toHaveBeenCalledTimes(calls)
  await act(async () => vi.advanceTimersByTimeAsync(1))
  expect(fetchTitle).toHaveBeenCalledTimes(calls + 1)
  expect(result.current.workspace.conversations[0]?.title).toBe('总结标题')
  expect(vi.getTimerCount()).toBe(0)
  unmount()
})
