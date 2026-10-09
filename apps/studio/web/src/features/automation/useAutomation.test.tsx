import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fetchRunPage, fetchTaskPage } from './api'
import { useAutomation } from './useAutomation'
import { taskFixture, runFixture } from '../../test/automationFixtures'
import { weekDates } from './model'
import { mockResourceNotices } from '../../test/resourceNotices'

vi.mock('./api', () => ({ fetchRunPage: vi.fn(), fetchTaskPage: vi.fn() }))
const dates = weekDates('2026-09-10')
const base = {projectId: 'project-1',  page: 'tasks' as const, query: '', status: 'all', dates, view: 'week' as const }
let notices: ReturnType<typeof mockResourceNotices>
beforeEach(() => {
  notices = mockResourceNotices()
  vi.useFakeTimers()
  vi.mocked(fetchTaskPage).mockReset().mockResolvedValue({ items: [taskFixture()], nextCursor: null })
  vi.mocked(fetchRunPage).mockReset().mockResolvedValue({ items: [], nextCursor: null })
})
afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks() })
const settle = () => act(async () => { await vi.advanceTimersByTimeAsync(0) })

describe('自动化服务端数据', () => {
  it('旧查询被取消，延迟返回不能覆盖新的筛选结果', async () => {
    let release: (value: Awaited<ReturnType<typeof fetchTaskPage>>) => void = () => {}
    const onLoadError = vi.fn()
    vi.mocked(fetchTaskPage).mockImplementationOnce(() => new Promise(resolve => { release = resolve }))
    const { result, rerender } = renderHook(({ query }) => useAutomation({ ...base, query, onLoadError }), { initialProps: { query: 'old' } })
    const oldSignal = vi.mocked(fetchTaskPage).mock.calls[0][1]
    vi.mocked(fetchTaskPage).mockResolvedValue({ items: [taskFixture({ id: 'new' })], nextCursor: null })
    rerender({ query: 'new' }); await settle()
    expect(oldSignal.aborted).toBe(true)
    await act(async () => release({ items: [taskFixture({ id: 'old' })], nextCursor: null }))
    expect(result.current.tasks.map(task => task.id)).toEqual(['new'])
    expect(onLoadError).not.toHaveBeenCalled()
  })
  it('读取失败通知一次，显式重试成功后清除失败状态', async () => {
    const onLoadError = vi.fn()
    vi.mocked(fetchTaskPage).mockRejectedValueOnce(new Error('unavailable'))
    const { result } = renderHook(() => useAutomation({ ...base, onLoadError })); await settle()
    expect(result.current.error).toBe(true)
    expect(onLoadError).toHaveBeenCalledOnce()
    act(() => result.current.reload()); await settle()
    expect(result.current.error).toBe(false)
    expect(onLoadError).toHaveBeenCalledOnce()
  })
  it('加载更多使用后端游标，刷新保留已加载页', async () => {
    vi.mocked(fetchTaskPage).mockImplementation(async params => params.cursor ? { items: [taskFixture({ id: 'second' })], nextCursor: null } : { items: [taskFixture({ id: 'first' })], nextCursor: 'next' })
    const { result } = renderHook(() => useAutomation(base)); await settle()
    act(() => result.current.loadMore('tasks')); await settle()
    expect(result.current.tasks.map(task => task.id)).toEqual(['first', 'second'])
    act(() => result.current.reload()); await settle()
    expect(result.current.tasks.map(task => task.id)).toEqual(['first', 'second'])
  })
  it('运行变化触发读取，空闲只做三十秒校准，隐藏和卸载停止请求', async () => {
    let hidden = false
    vi.spyOn(document, 'hidden', 'get').mockImplementation(() => hidden)
    vi.mocked(fetchRunPage).mockResolvedValue({ items: [runFixture({ status: 'running' })], nextCursor: null })
    const { unmount } = renderHook(() => useAutomation({ ...base, page: 'history', view: 'list' })); await settle()
    expect(fetchRunPage).toHaveBeenCalledTimes(1)
    await act(async () => { await vi.advanceTimersByTimeAsync(2000) })
    expect(fetchRunPage).toHaveBeenCalledTimes(1)
    await act(async () => notices.changed('automation.execution.changed', 'run'))
    expect(fetchRunPage).toHaveBeenCalledTimes(2)
    act(() => { hidden = true; document.dispatchEvent(new Event('visibilitychange')) })
    await act(async () => { await vi.advanceTimersByTimeAsync(10000) })
    expect(fetchRunPage).toHaveBeenCalledTimes(2)
    act(() => { hidden = false; document.dispatchEvent(new Event('visibilitychange')) }); await settle()
    const last = vi.mocked(fetchRunPage).mock.calls.at(-1)![1]
    unmount(); expect(last.aborted).toBe(true)
  })
})
