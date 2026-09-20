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
function useHarness() {
  const [workspace, setWorkspace] = useState<WorkspaceState>({ currentThreadId: 'a', conversations: ['a', 'b'].map(threadId => ({ ...buildEmptyConversation({ threadId, now: '2026-09-20T00:00:00Z' }), ...title(threadId), runStatus: 'streaming' })) })
  useConversationTitles(workspace, setWorkspace)
  return { workspace, setWorkspace }
}
beforeEach(() => { vi.useFakeTimers(); fetchTitle.mockReset() })
afterEach(() => vi.useRealTimers())

it('主回复结束和切到B后仍查询A，完成后停止且不改正文', async () => {
  fetchTitle.mockImplementation(async threadId => title(threadId))
  const { result } = renderHook(useHarness)
  await act(async () => {})
  await act(async () => result.current.setWorkspace(state => ({ ...state, currentThreadId: 'b', conversations: state.conversations.map(item => ({ ...item, runStatus: 'idle' })) })))
  fetchTitle.mockImplementation(async threadId => title(threadId, true))
  await act(async () => vi.advanceTimersByTimeAsync(1000))
  expect(result.current.workspace.currentThreadId).toBe('b')
  expect(result.current.workspace.conversations.map(item => item.title)).toEqual(['总结标题', '总结标题'])
  expect(result.current.workspace.conversations.every(item => item.messages.length === 0)).toBe(true)
  expect(fetchTitle).toHaveBeenCalledTimes(4)
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

it('查询错误按间隔恢复，较旧标题不能覆盖较新状态', async () => {
  fetchTitle.mockRejectedValueOnce(new TypeError('network')).mockImplementation(async threadId => ({ ...title(threadId, true), title: '过期标题', titleSeq: 0 }))
  const { result, unmount } = renderHook(useHarness)
  await act(async () => {})
  await act(async () => vi.advanceTimersByTimeAsync(1000))
  expect(result.current.workspace.conversations.every(item => item.title === '临时标题')).toBe(true)
  fetchTitle.mockImplementation(async threadId => title(threadId, true))
  await act(async () => vi.advanceTimersByTimeAsync(1000))
  expect(result.current.workspace.conversations.every(item => item.title === '总结标题')).toBe(true)
  unmount()
  expect(vi.getTimerCount()).toBe(0)
})
