import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { useWorkspaceHistory } from './useWorkspaceHistory'
import { useWorkspaceState } from './useWorkspaceState'
import { fetchConversationHistoryList, fetchConversationHistoryGroupConfig, type ConversationHistoryListResponse } from '../../api/conversation/history'

vi.mock(import('../../api/conversation/history'), async importOriginal => ({
  ...await importOriginal(), fetchConversationHistoryList: vi.fn(), fetchConversationHistoryGroupConfig: vi.fn(),
}))
const item = { id: 1, projectId: 'project', archived: false, threadId: 'first', title: '活跃会话', titleSource: 'user', titleGenerationStatus: 'skipped', titleSeq: 1,
  accessMode: 'full', status: 'idle', lastRunId: 'run', lastModel: 'main', messageCount: 0, toolCallCount: 0, hasPendingInterrupt: false,
  pendingInteractionKind: null, pinned: false, createdAt: '2030-01-01', updatedAt: '2030-01-01',
} satisfies ConversationHistoryListResponse['items'][number]
const prepare = async () => {}
const follow = () => {}
const toast = () => {}
afterEach(() => { cleanup(); vi.resetAllMocks(); vi.useRealTimers() })

it('切换归档范围取消旧分页，晚到结果不能覆盖新范围或解除新分页等待', async () => {
  vi.useFakeTimers()
  window.history.replaceState({}, '', '/')
  vi.mocked(fetchConversationHistoryGroupConfig).mockResolvedValue({ dayRanges: [7, 30] })
  let completeOldPage!: (value: ConversationHistoryListResponse) => void
  let completeArchived!: (value: ConversationHistoryListResponse) => void
  let completeArchivedPage!: (value: ConversationHistoryListResponse) => void
  vi.mocked(fetchConversationHistoryList).mockImplementation(params => {
    if (params.archived) return new Promise(resolve => { if (params.cursor) completeArchivedPage = resolve; else completeArchived = resolve })
    if (params.cursor) return new Promise(resolve => { completeOldPage = resolve })
    return Promise.resolve({ items: [item], nextCursor: 'next' })
  })
  const { result, rerender } = renderHook(({ archived }) => {
    const state = useWorkspaceState()
    return useWorkspaceHistory({ ...state, projectId: 'project', archived, searchScope: 'project', preferDraft: true,
      defaultModelId: 'main', modelCatalogStatus: 'ready', followDetachedConversation: follow, prepareTaskTraceOwner: prepare, onToast: toast })
  }, { initialProps: { archived: false } })
  await act(async () => vi.advanceTimersByTimeAsync(0))
  act(() => result.current.loadMoreHistory())
  const oldSignal = vi.mocked(fetchConversationHistoryList).mock.calls.at(-1)![0].signal
  rerender({ archived: true })
  await act(async () => vi.advanceTimersByTimeAsync(0))
  expect(oldSignal?.aborted).toBe(true)
  expect(completeArchived).toBeTypeOf('function')
  expect(result.current.historyConversations).toEqual([])
  const archivedItem = { ...item, archived: true, threadId: 'archived', title: '归档会话' }
  await act(async () => completeArchived({ items: [archivedItem], nextCursor: 'next' }))
  act(() => result.current.loadMoreHistory())
  await act(async () => completeOldPage({ items: [{ ...item, threadId: 'late-page' }], nextCursor: null }))
  expect(result.current.historyConversations.map(value => value.threadId)).toEqual(['archived'])
  expect(result.current.isHistoryLoadingMore).toBe(true)
  await act(async () => completeArchivedPage({ items: [{ ...archivedItem, threadId: 'archived-page' }], nextCursor: null }))
  expect(result.current.historyConversations.map(value => value.threadId)).toEqual(['archived', 'archived-page'])
  expect(result.current.isHistoryLoadingMore).toBe(false)
})
