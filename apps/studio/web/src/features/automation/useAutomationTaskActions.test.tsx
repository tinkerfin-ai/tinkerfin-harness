import { act, renderHook } from '@testing-library/react'
import { beforeEach, expect, it, vi } from 'vitest'
import { taskFixture } from '../../test/automationFixtures'
import { batchTasks, commandTask } from './api'
import { useAutomationTaskActions } from './useAutomationTaskActions'

vi.mock('./api', () => ({ batchTasks: vi.fn(), commandTask: vi.fn() }))
beforeEach(() => { vi.mocked(batchTasks).mockReset(); vi.mocked(commandTask).mockReset() })

it('未确认的单条操作复用身份，确认成功后的新操作获得新身份', async () => {
  const task = taskFixture()
  vi.mocked(commandTask).mockRejectedValueOnce(new Error('network')).mockResolvedValue(task)
  const reload = vi.fn()
  const { result } = renderHook(() => useAutomationTaskActions('project-1', [task], reload, vi.fn()))
  await act(async () => result.current.execute(task.id, 'run'))
  await act(async () => result.current.execute(task.id, 'run'))
  const calls = vi.mocked(commandTask).mock.calls
  expect(calls[0][2]).toBe(calls[1][2])
  expect(reload).toHaveBeenCalledOnce()
  await act(async () => result.current.execute(task.id, 'run'))
  expect(calls[2][2]).not.toBe(calls[1][2])
})

it('同一任务的单条和批量操作互斥，卸载取消请求并拒绝延迟反馈', async () => {
  const task = taskFixture()
  let complete!: (value: Awaited<ReturnType<typeof commandTask>>) => void
  const request = new Promise<Awaited<ReturnType<typeof commandTask>>>(resolve => { complete = resolve })
  vi.mocked(commandTask).mockReturnValue(request)
  const reload = vi.fn(), toast = vi.fn()
  const { result, unmount } = renderHook(() => useAutomationTaskActions('project-1', [task], reload, toast))
  let first!: Promise<void>
  act(() => { first = result.current.execute(task.id, 'run') })
  await act(async () => {
    await result.current.execute(task.id, 'pause')
    expect(await result.current.pause(new Set([task.id]))).toEqual(new Set([task.id]))
  })
  expect(commandTask).toHaveBeenCalledOnce()
  expect(batchTasks).not.toHaveBeenCalled()
  const signal = vi.mocked(commandTask).mock.calls[0][3]
  unmount()
  expect(signal.aborted).toBe(true)
  await act(async () => { complete(task); await first })
  expect(reload).not.toHaveBeenCalled()
  expect(toast).not.toHaveBeenCalled()
})

it.each(['cancel', 'unmount'] as const)('删除确认在 %s 时完成选择结果且不发送删除请求', async ending => {
  const task = taskFixture()
  const { result, unmount } = renderHook(() => useAutomationTaskActions('project-1', [task], vi.fn(), vi.fn()))
  let selection!: Promise<ReadonlySet<string>>
  act(() => { selection = result.current.requestDelete(new Set([task.id]), document.body) })
  expect(result.current.deletion?.tasks).toEqual([task])
  if (ending === 'cancel') act(() => result.current.cancelDelete())
  else unmount()
  expect(await selection).toEqual(new Set([task.id]))
  expect(batchTasks).not.toHaveBeenCalled()
})

it('批量删除只保留失败项，其重试继续使用原操作身份', async () => {
  const first = taskFixture(), second = taskFixture({ id: 'second' })
  vi.mocked(batchTasks).mockResolvedValueOnce([{ taskId: first.id, succeeded: true, error: null }, { taskId: second.id, succeeded: false, error: 'failed' }])
    .mockResolvedValueOnce([{ taskId: second.id, succeeded: true, error: null }])
  const { result } = renderHook(() => useAutomationTaskActions('project-1', [first, second], vi.fn(), vi.fn()))
  let selection!: Promise<ReadonlySet<string>>
  act(() => { selection = result.current.requestDelete(new Set([first.id, second.id]), document.body) })
  await act(async () => result.current.confirmDelete())
  expect(await selection).toEqual(new Set([second.id]))
  expect(result.current.deletion).toBeNull()
  act(() => { selection = result.current.requestDelete(new Set([second.id]), document.body) })
  await act(async () => result.current.confirmDelete())
  expect(await selection).toEqual(new Set())
  const calls = vi.mocked(batchTasks).mock.calls
  expect(calls[0][3][second.id]).toBe(calls[1][3][second.id])
})
