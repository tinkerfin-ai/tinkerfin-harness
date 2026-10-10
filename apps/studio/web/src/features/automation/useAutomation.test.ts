import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { mockResourceNotices } from '../../test/resourceNotices'
import { fetchRunCalendar, fetchRunPage, fetchTaskPage, type Page } from './api'
import { presentRun, weekDates, type AutomationRun } from './model'
import { useAutomation } from './useAutomation'

vi.mock('./api', () => ({ fetchRunCalendar: vi.fn(), fetchRunPage: vi.fn(), fetchTaskPage: vi.fn() }))
const dates = weekDates('2030-01-01')
const options = { projectId: 'first', page: 'history' as const, query: '', status: 'all', dates, view: 'week' as const }
const run = (id: string, date = dates[0], status: AutomationRun['status'] = 'succeeded') => presentRun({
  id, taskId: null, name: id, queuedAt: `${date}T12:00:00+08:00`, startedAt: null, finishedAt: null, status, trigger: 'manual', error: null,
})
const calendar = () => dates.map(date => ({ date, items: [run(date, date)], nextCursor: date === dates[0] ? 'next' : null }))
const settle = () => act(() => vi.advanceTimersByTimeAsync(0))
let notices: ReturnType<typeof mockResourceNotices>
beforeEach(() => {
  vi.useFakeTimers(); vi.resetAllMocks()
  notices = mockResourceNotices()
  vi.mocked(fetchRunCalendar).mockImplementation(async () => calendar())
  vi.mocked(fetchRunPage).mockResolvedValue({ items: [run('older')], nextCursor: null })
  vi.mocked(fetchTaskPage).mockResolvedValue({ items: [], nextCursor: null })
})
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.useRealTimers() })

it('周历只读一次，空闲与任务通知不重读，执行通知刷新且每日分页完整', async () => {
  const { result } = renderHook(() => useAutomation(options))
  await settle()
  expect(fetchRunCalendar).toHaveBeenCalledOnce()
  expect(fetchRunPage).not.toHaveBeenCalled()
  expect(result.current.runs).toHaveLength(7)
  act(() => notices.changed('automation.task.changed', 'task'))
  await act(() => vi.advanceTimersByTimeAsync(90_000))
  expect(fetchRunCalendar).toHaveBeenCalledOnce()
  act(() => { result.current.loadMore(dates[0]); result.current.loadMore(dates[0]) })
  await settle()
  expect(fetchRunPage).toHaveBeenCalledOnce()
  expect(fetchRunPage).toHaveBeenCalledWith(expect.objectContaining({ from: `${dates[0]}T00:00:00+08:00`, cursor: 'next' }), expect.any(AbortSignal))
  expect(fetchRunCalendar).toHaveBeenCalledOnce()
  expect(result.current.runs).toHaveLength(8)
  act(() => notices.changed('automation.execution.changed', 'run')); await settle()
  expect(fetchRunCalendar).toHaveBeenCalledTimes(2)
  expect(fetchRunPage).toHaveBeenCalledTimes(2)
  expect(result.current.runs).toHaveLength(8)
})

it('任务页忽略执行通知，连续搜索只读取最后输入，切换项目取消并丢弃迟到分页', async () => {
  const { result, rerender } = renderHook(props => useAutomation(props), { initialProps: { ...options, page: 'tasks' as 'tasks' | 'history' } })
  await settle()
  act(() => notices.changed('automation.execution.changed', 'run')); await settle()
  expect(fetchTaskPage).toHaveBeenCalledOnce()
  rerender({ ...options, page: 'tasks', query: 'r' })
  rerender({ ...options, page: 'tasks', query: 'report' })
  await act(() => vi.advanceTimersByTimeAsync(250))
  expect(fetchTaskPage).toHaveBeenCalledTimes(2)
  expect(fetchTaskPage).toHaveBeenLastCalledWith(expect.objectContaining({ query: 'report' }), expect.any(AbortSignal))
  rerender(options); await settle()
  let resolve!: (page: Page<AutomationRun>) => void
  vi.mocked(fetchRunPage).mockReturnValueOnce(new Promise(reply => { resolve = reply }))
  act(() => result.current.loadMore(dates[0]))
  const signal = vi.mocked(fetchRunPage).mock.calls[0][1]
  rerender({ ...options, projectId: 'second' }); await settle()
  expect(signal.aborted).toBe(true)
  await act(async () => resolve({ items: [run('private')], nextCursor: null }))
  expect(result.current.runs.some(item => item.id === 'private')).toBe(false)
})

it('只校准进行中的执行，完成或请求失败后停止，支持主动重试', async () => {
  vi.mocked(fetchRunCalendar).mockResolvedValueOnce([{ date: dates[0], items: [run('active', dates[0], 'running')], nextCursor: null }])
  const { result } = renderHook(() => useAutomation(options))
  await act(() => vi.advanceTimersByTimeAsync(120_000))
  expect(fetchRunCalendar).toHaveBeenCalledTimes(2)
  vi.mocked(fetchRunCalendar).mockRejectedValueOnce(new Error('offline'))
  act(() => result.current.reload()); await settle()
  expect(result.current.error).toBe(true)
  await act(() => vi.advanceTimersByTimeAsync(90_000))
  expect(fetchRunCalendar).toHaveBeenCalledTimes(3)
  act(() => result.current.reload()); await settle()
  expect(result.current.error).toBe(false)
})


it('追加页出现进行中运行后启动确认，刷新后完成则停止', async () => {
  vi.mocked(fetchRunPage).mockResolvedValueOnce({ items: [run('older-active', dates[0], 'running')], nextCursor: null })
  const { result } = renderHook(() => useAutomation(options))
  await settle()
  act(() => result.current.loadMore(dates[0])); await settle()
  expect(fetchRunCalendar).toHaveBeenCalledOnce()
  expect(fetchRunPage).toHaveBeenCalledOnce()
  expect(result.current.runs.some(item => item.status === 'running')).toBe(true)
  await act(() => vi.advanceTimersByTimeAsync(90_000))
  expect(fetchRunCalendar).toHaveBeenCalledTimes(2)
  expect(fetchRunPage).toHaveBeenCalledTimes(2)
  expect(result.current.runs.some(item => item.status === 'running')).toBe(false)
})
