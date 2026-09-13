import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fetchCounts, fetchRunPage, fetchTaskPage } from './api'
import { useAutomation } from './useAutomation'
import { taskFixture, runFixture } from '../../test/automationFixtures'
import { weekDates } from './model'

vi.mock('./api', () => ({ fetchCounts: vi.fn(), fetchRunPage: vi.fn(), fetchTaskPage: vi.fn() }))
const dates = weekDates('2026-09-10')
const base = { page: 'tasks' as const, query: '', status: 'all', dates, view: 'week' as const }
beforeEach(() => {
  vi.useFakeTimers()
  vi.mocked(fetchCounts).mockResolvedValue({ enabled: 1, paused: 0 })
  vi.mocked(fetchTaskPage).mockReset().mockResolvedValue({ items: [taskFixture()], nextCursor: null })
  vi.mocked(fetchRunPage).mockReset().mockResolvedValue({ items: [], nextCursor: null })
})
afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks() })
const settle = () => act(async () => { await vi.advanceTimersByTimeAsync(0) })

describe('自动化服务端数据', () => {
  it('旧查询被取消，延迟返回不能覆盖新的筛选结果', async () => {
    let release: (value: Awaited<ReturnType<typeof fetchTaskPage>>) => void = () => {}
    vi.mocked(fetchTaskPage).mockImplementationOnce(() => new Promise(resolve => { release = resolve }))
    const { result, rerender } = renderHook(({ query }) => useAutomation({ ...base, query }), { initialProps: { query: 'old' } })
    const oldSignal = vi.mocked(fetchTaskPage).mock.calls[0][1]
    vi.mocked(fetchTaskPage).mockResolvedValue({ items: [taskFixture({ id: 'new' })], nextCursor: null })
    rerender({ query: 'new' }); await settle()
    expect(oldSignal.aborted).toBe(true)
    await act(async () => release({ items: [taskFixture({ id: 'old' })], nextCursor: null }))
    expect(result.current.tasks.map(task => task.id)).toEqual(['new'])
  })
  it('加载更多使用后端游标，刷新保留已加载页', async () => {
    vi.mocked(fetchTaskPage).mockImplementation(async params => params.cursor ? { items: [taskFixture({ id: 'second' })], nextCursor: null } : { items: [taskFixture({ id: 'first' })], nextCursor: 'next' })
    const { result } = renderHook(() => useAutomation(base)); await settle()
    act(() => result.current.loadMore('tasks')); await settle()
    expect(result.current.tasks.map(task => task.id)).toEqual(['first', 'second'])
    act(() => result.current.reload()); await settle()
    expect(result.current.tasks.map(task => task.id)).toEqual(['first', 'second'])
  })
  it('运行状态按虚拟时钟刷新，隐藏页面和卸载停止请求', async () => {
    let hidden = false
    vi.spyOn(document, 'hidden', 'get').mockImplementation(() => hidden)
    vi.mocked(fetchRunPage).mockResolvedValue({ items: [runFixture({ status: 'running' })], nextCursor: null })
    const { unmount } = renderHook(() => useAutomation({ ...base, page: 'history', view: 'list' })); await settle()
    expect(fetchRunPage).toHaveBeenCalledTimes(1)
    await act(async () => { await vi.advanceTimersByTimeAsync(2000) })
    expect(fetchRunPage).toHaveBeenCalledTimes(2)
    act(() => { hidden = true; document.dispatchEvent(new Event('visibilitychange')) })
    await act(async () => { await vi.advanceTimersByTimeAsync(10000) })
    expect(fetchRunPage).toHaveBeenCalledTimes(2)
    act(() => { hidden = false; document.dispatchEvent(new Event('visibilitychange')) }); await settle()
    const last = vi.mocked(fetchRunPage).mock.calls.at(-1)![1]
    unmount(); expect(last.aborted).toBe(true)
  })
})
