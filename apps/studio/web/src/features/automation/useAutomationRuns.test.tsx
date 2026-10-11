import { act, cleanup, fireEvent, render, renderHook, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { saveAuthSession, createAuthSession, clearAuthSession, updateAuthSession } from '../../auth/session'
import * as notices from '../../api/notifications'
import type { ResourceNotice } from '../../api/notifications'
import { executeTask, fetchActiveRunPage, fetchRunStatus } from './api'
import { useAutomationRuns } from './useAutomationRuns'
import { AutomationTaskList } from './AutomationTaskList'
import { runFixture, taskFixture } from '../../test/automationFixtures'
import type { AutomationRunRecord } from './model'

vi.mock('./api', () => ({ executeTask: vi.fn(), fetchActiveRunPage: vi.fn(), fetchRunStatus: vi.fn() }))
const listeners = new Set<(notice: ResourceNotice) => void>()
const emit = (notice: ResourceNotice) => listeners.forEach(listener => listener(notice))
const change = (id = 'run', project = 'project-1', user = 1) => emit({ kind: 'change', change: {
  scope: { namespace: 'studio_automation', owner_id: `${user}:${project}` }, topic: 'automation.execution.changed', key: id, details: {},
} })
const settle = () => act(() => vi.advanceTimersByTimeAsync(0))
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => { resolve = done })
  return { promise, resolve }
}
beforeEach(() => {
  vi.useFakeTimers(); vi.resetAllMocks(); listeners.clear()
  vi.spyOn(notices, 'getResourceConnectionState').mockReturnValue('ready')
  vi.spyOn(notices, 'subscribeResourceChanges').mockImplementation(listener => { listeners.add(listener); return () => { listeners.delete(listener) } })
  vi.mocked(fetchActiveRunPage).mockResolvedValue({ items: [], nextCursor: null })
})
afterEach(() => { cleanup(); clearAuthSession(); vi.restoreAllMocks(); vi.useRealTimers() })

it('执行按钮立即 loading，入队响应保留 loading，实时成功只提示一次并可打开会话', async () => {
  const reply = deferred<AutomationRunRecord>(), toast = vi.fn(), open = vi.fn(), task = taskFixture()
  vi.mocked(executeTask).mockReturnValue(reply.promise)
  const queued = runFixture({ status: 'queued', updatedAt: '2030-01-01T00:00:00Z', finishedAt: null })
  vi.mocked(fetchRunStatus).mockResolvedValue(queued)
  function Harness() {
    const runs = useAutomationRuns(1, ['project-1'], toast, open)
    return <AutomationTaskList tasks={[task]} query="" status="all" busy={new Set()} executions={runs.states} onEdit={vi.fn()}
      onExecute={() => { void runs.execute(task) }} onToggle={vi.fn()} onPause={async () => new Set()} onDelete={async () => new Set()} onClearFilter={vi.fn()} />
  }
  render(<Harness />); await settle()
  const button = screen.getByRole('button', { name: `执行 ${task.name}` })
  fireEvent.click(button)
  expect(button).toBeDisabled(); expect(button).toHaveAttribute('aria-busy', 'true')
  await act(async () => reply.resolve(queued)); await settle()
  expect(button).toBeDisabled(); expect(screen.getByRole('status')).toHaveTextContent('排队中')
  const done = runFixture({ updatedAt: '2030-01-01T00:00:01Z', conversation: { state: 'available', projectId: 'project-1', threadId: 'conversation' } })
  vi.mocked(fetchRunStatus).mockResolvedValue(done)
  act(() => change()); await settle()
  expect(button).toBeEnabled(); expect(screen.getByRole('status')).toHaveTextContent('运行完成')
  expect(toast.mock.calls.filter(call => call[0] === 'success')).toHaveLength(1)
  act(() => change()); await settle()
  expect(toast.mock.calls.filter(call => call[0] === 'success')).toHaveLength(1)
  toast.mock.calls.find(call => call[0] === 'success')![2].onSelect()
  expect(open).toHaveBeenCalledWith(done)
})

it('在线极快完成早于入队响应时，迟到响应不能恢复 loading 或重复成功提示', async () => {
  const reply = deferred<AutomationRunRecord>(), toast = vi.fn()
  vi.mocked(executeTask).mockReturnValue(reply.promise)
  vi.mocked(fetchRunStatus).mockResolvedValue(runFixture({ updatedAt: '2030-01-01T00:00:02Z' }))
  const { result } = renderHook(() => useAutomationRuns(1, ['project-1'], toast, vi.fn()))
  await settle()
  let submitted!: Promise<void>
  act(() => { submitted = result.current.execute(taskFixture()) })
  act(() => change()); await settle()
  await act(async () => { reply.resolve(runFixture({ status: 'queued', updatedAt: '2030-01-01T00:00:00Z' })); await submitted })
  await settle()
  expect(result.current.states.get('news')).toBe('succeeded')
  expect(toast.mock.calls.filter(call => call[0] === 'success')).toHaveLength(1)
})

it('所有授权项目实时执行都通知，初始和重连基线不补弹离线完成', async () => {
  const active = runFixture({ status: 'running', projectId: 'second' }), toast = vi.fn()
  vi.mocked(fetchActiveRunPage).mockResolvedValueOnce({ items: [active], nextCursor: null })
  const { result, rerender } = renderHook(({ projects }) => useAutomationRuns(1, projects, toast, vi.fn()), { initialProps: { projects: ['project-1', 'second'] } })
  await settle(); expect(result.current.states.get('news')).toBe('running'); expect(toast).not.toHaveBeenCalled()
  act(() => emit({ kind: 'connection', state: 'disconnected' }))
  vi.mocked(fetchRunStatus).mockResolvedValue(runFixture({ projectId: 'second' }))
  act(() => emit({ kind: 'resync' })); await settle()
  expect(result.current.states.get('news')).toBe('succeeded'); expect(toast).not.toHaveBeenCalled()
  rerender({ projects: ['second', 'project-1'] })
  vi.mocked(fetchRunStatus).mockResolvedValue(runFixture({ id: 'scheduled', taskId: 'scheduled-task', projectId: 'second', status: 'failed' }))
  act(() => change('scheduled', 'second')); await settle()
  expect(toast).toHaveBeenCalledWith('error', expect.stringContaining('运行失败'), undefined)
  const count = vi.mocked(fetchRunStatus).mock.calls.length
  act(() => change('foreign', 'project-1', 2)); await settle()
  expect(fetchRunStatus).toHaveBeenCalledTimes(count)
})

it('未知提交重试保留原命令与请求 ID，任务编辑不改写它，卸载取消在途请求', async () => {
  const task = taskFixture(), toast = vi.fn()
  vi.mocked(executeTask).mockRejectedValueOnce(new Error('network'))
  const { result, unmount } = renderHook(() => useAutomationRuns(1, ['project-1'], toast, vi.fn()))
  await settle(); await act(async () => result.current.execute(task))
  expect(result.current.states.get(task.id)).toBe('uncertain')
  const second = deferred<AutomationRunRecord>()
  vi.mocked(executeTask).mockReturnValue(second.promise)
  let retry!: Promise<void>
  act(() => { retry = result.current.execute({ ...task, revision: 2, prompt: 'changed' }) })
  const calls = vi.mocked(executeTask).mock.calls
  expect(calls[1][0]).toEqual(calls[0][0]); expect(calls[1][1]).toBe(calls[0][1])
  unmount(); expect(calls[1][2].aborted).toBe(true)
  await act(async () => { second.resolve(runFixture()); await retry })
  expect(toast.mock.calls.filter(call => call[0] === 'success')).toHaveLength(0)
})


it('更新同一登录会话的头像保留未知提交身份和在线通知', async () => {
  const user = { user_id: 1, username: 'owner', avatar_url: null, roles: [], disabled: false }
  const expires_at = '2099-01-01T00:00:00Z'
  saveAuthSession(createAuthSession({ access_token: 'unchanged-token', token_type: 'Bearer', expires_at, user }))
  vi.mocked(executeTask).mockRejectedValueOnce(new Error('network')).mockResolvedValue(runFixture())
  const toast = vi.fn()
  const { result } = renderHook(() => useAutomationRuns(1, ['project-1'], toast, vi.fn()))
  await settle(); await act(async () => result.current.execute(taskFixture()))
  act(() => { updateAuthSession({ expires_at, user: { ...user, avatar_url: 'data:image/png;base64,AA' } }) })
  expect(result.current.states.get('news')).toBe('uncertain')
  await act(async () => result.current.execute(taskFixture()))
  expect(vi.mocked(executeTask).mock.calls[0][1]).toBe(vi.mocked(executeTask).mock.calls[1][1])
  expect(toast.mock.calls.filter(call => call[0] === 'success')).toHaveLength(1)
})

it('终态事件状态查询失败后，重试仍读取原执行并只通知一次', async () => {
  const toast = vi.fn()
  vi.mocked(fetchRunStatus).mockRejectedValueOnce(new Error('network')).mockResolvedValue(runFixture())
  const { result } = renderHook(() => useAutomationRuns(1, ['project-1'], toast, vi.fn()))
  await settle(); act(() => change()); await settle()
  expect(result.current.syncError).toBe(true)
  expect(toast).not.toHaveBeenCalled()
  act(() => result.current.refresh()); await settle()
  expect(result.current.syncError).toBe(false)
  expect(fetchRunStatus).toHaveBeenCalledTimes(2)
  expect(result.current.states.get('news')).toBe('succeeded')
  expect(toast.mock.calls.filter(call => call[0] === 'success')).toHaveLength(1)
})


it('活跃列表读取失败也保留读取期间的在线终态，基线重试后继续确认', async () => {
  vi.mocked(fetchActiveRunPage).mockRejectedValueOnce(new Error('offline'))
    .mockRejectedValueOnce(new Error('baseline unavailable'))
    .mockResolvedValue({ items: [], nextCursor: null })
  vi.mocked(fetchRunStatus).mockResolvedValue(runFixture())
  const toast = vi.fn()
  const { result } = renderHook(() => useAutomationRuns(1, ['project-1'], toast, vi.fn()))
  await settle(); act(() => change()); await settle()
  expect(result.current.syncError).toBe(true)
  act(() => result.current.refresh()); await settle()
  expect(result.current.states.get('news')).toBe('succeeded')
  expect(result.current.syncError).toBe(false)
  expect(toast.mock.calls.filter(call => call[0] === 'success')).toHaveLength(1)
})


it('同一毫秒内的运行状态不会被较早排队响应覆盖', async () => {
  const response = deferred<AutomationRunRecord>()
  vi.mocked(executeTask).mockReturnValue(response.promise)
  vi.mocked(fetchRunStatus).mockResolvedValue(runFixture({ status: 'running', updatedAt: '2030-01-01T00:00:00.000200Z' }))
  const { result } = renderHook(() => useAutomationRuns(1, ['project-1'], vi.fn(), vi.fn()))
  await settle()
  let submission!: Promise<void>
  act(() => { submission = result.current.execute(taskFixture()) })
  act(() => change()); await settle()
  await act(async () => { response.resolve(runFixture({ status: 'queued', updatedAt: '2030-01-01T00:00:00.000100Z' })); await submission })
  expect(result.current.states.get('news')).toBe('running')
})
