import { act, cleanup, fireEvent, render, renderHook, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { saveAuthSession, createAuthSession, clearAuthSession, updateAuthSession } from '../../auth/session'
import * as notices from '../../api/notifications'
import type { ResourceNotice } from '../../api/notifications'
import { executeTask, fetchActiveRunPage, fetchRunStatus, fetchSubmission } from './api'
import { useAutomationRuns } from './useAutomationRuns'
import { AutomationTaskList } from './AutomationTaskList'
import { runFixture, taskFixture } from '../../test/automationFixtures'
import type { AutomationRunRecord } from './model'

vi.mock('./api', () => ({ executeTask: vi.fn(), fetchActiveRunPage: vi.fn(), fetchRunStatus: vi.fn(), fetchSubmission: vi.fn() }))
const listeners = new Set<(notice: ResourceNotice) => void>()
const emit = (notice: ResourceNotice) => listeners.forEach(listener => listener(notice))
const change = (id = 'run', project = 'project-1', user = 1) => emit({ kind: 'change', change: {
  scope: { namespace: 'studio_automation', owner_id: `${user}:${project}` }, topic: 'automation.execution.changed', key: id, details: {},
} })
const published = (id = 'run', project = 'project-1', user = 1) => emit({ kind: 'change', change: {
  scope: { namespace: `ns_${user}`, owner_id: null }, topic: 'studio.conversation.changed', key: 'conversation',
  details: { automation_execution_id: id, automation_project_id: project },
} })
const settle = () => act(() => vi.advanceTimersByTimeAsync(0))
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}
beforeEach(() => {
  vi.useFakeTimers(); vi.resetAllMocks(); listeners.clear()
  vi.spyOn(notices, 'getResourceConnectionState').mockReturnValue('ready')
  vi.spyOn(notices, 'subscribeResourceChanges').mockImplementation(listener => { listeners.add(listener); return () => { listeners.delete(listener) } })
  vi.mocked(fetchActiveRunPage).mockResolvedValue({ items: [], nextCursor: null })
  vi.mocked(fetchSubmission).mockResolvedValue(null)
})
afterEach(() => { cleanup(); clearAuthSession(); vi.restoreAllMocks(); vi.useRealTimers() })

it('终态立即解除执行按钮 loading，会话开放后只提示一次并可打开会话', async () => {
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
  vi.mocked(fetchRunStatus).mockResolvedValue({ ...done, conversation: { state: 'pending' } })
  act(() => change()); await settle()
  expect(button).toBeEnabled(); expect(screen.getByRole('status')).toHaveTextContent('运行完成')
  expect(toast.mock.calls.filter(call => call[0] === 'success')).toHaveLength(0)
  vi.mocked(fetchRunStatus).mockResolvedValue(done)
  act(() => published()); await settle()
  expect(toast.mock.calls.filter(call => call[0] === 'success')).toHaveLength(1)
  act(() => { published(); change() }); await settle()
  expect(toast.mock.calls.filter(call => call[0] === 'success')).toHaveLength(1)
  toast.mock.calls.find(call => call[0] === 'success')![2].onSelect()
  expect(open).toHaveBeenCalledWith(done)
})

it('在线失败先结算，漏掉会话开放通知时校准仍兑现一次完成提示并停止轮询', async () => {
  const toast = vi.fn(), open = vi.fn()
  const failure = runFixture({ status: 'failed', conversation: { state: 'pending' } })
  vi.mocked(fetchRunStatus).mockResolvedValue(failure)
  const { result } = renderHook(() => useAutomationRuns(1, ['project-1'], toast, open))
  await settle(); act(() => change()); await settle()
  expect(result.current.states.get('news')).toBe('failed')
  expect(toast).not.toHaveBeenCalled()
  const available = { ...failure, conversation: { state: 'available', projectId: 'moved-project', threadId: 'conversation' } } as const
  vi.mocked(fetchRunStatus).mockResolvedValue(available)
  await act(() => vi.advanceTimersByTimeAsync(30_000))
  expect(toast).toHaveBeenCalledExactlyOnceWith('error', expect.stringContaining('运行失败'), expect.objectContaining({ label: '查看关联会话' }))
  toast.mock.calls[0][2].onSelect()
  expect(open).toHaveBeenCalledExactlyOnceWith(available)
  expect(fetchRunStatus).toHaveBeenLastCalledWith('project-1', 'run', expect.any(AbortSignal))
  await act(() => vi.advanceTimersByTimeAsync(90_000))
  expect(fetchRunStatus).toHaveBeenCalledTimes(2)
})

it.each(['initial', 'reconnect', 'identity', 'hidden'] as const)('%s 后不会把会话公开当作新的在线完成通知', async mode => {
  const toast = vi.fn()
  vi.mocked(fetchRunStatus).mockResolvedValue(runFixture({ conversation: { state: 'pending' } }))
  const { result } = renderHook(() => useAutomationRuns(1, ['project-1'], toast, vi.fn()))
  await settle()
  if (mode !== 'initial') { act(() => change()); await settle() }
  if (mode === 'reconnect') {
    act(() => emit({ kind: 'connection', state: 'disconnected' }))
    act(() => emit({ kind: 'resync' })); await settle()
  }
  if (mode === 'identity') act(() => {
    saveAuthSession(createAuthSession({ access_token: 'new-session', token_type: 'Bearer', expires_at: '2099-01-01T00:00:00Z',
      user: { user_id: 1, username: 'owner', avatar_url: null, roles: [], disabled: false } }))
  })
  if (mode === 'hidden') {
    const hidden = vi.spyOn(document, 'hidden', 'get').mockReturnValue(true)
    act(() => document.dispatchEvent(new Event('visibilitychange')))
    hidden.mockReturnValue(false)
    act(() => { document.dispatchEvent(new Event('visibilitychange')); emit({ kind: 'resync' }) })
    await settle()
  }
  vi.mocked(fetchRunStatus).mockResolvedValue(runFixture({ conversation: { state: 'available', projectId: 'project-1', threadId: 'conversation' } }))
  act(() => published()); await settle()
  expect(result.current.states.get('news')).toBe('succeeded')
  expect(toast).not.toHaveBeenCalled()
})

it('读取期间合并终态与公开通知时保留在线提示资格，并忽略其他身份或项目', async () => {
  const baseline = deferred<{ items: AutomationRunRecord[]; nextCursor: null }>(), toast = vi.fn()
  vi.mocked(fetchActiveRunPage).mockReturnValueOnce(baseline.promise)
  vi.mocked(fetchRunStatus).mockResolvedValue(runFixture({ conversation: { state: 'available', projectId: 'moved', threadId: 'conversation' } }))
  renderHook(() => useAutomationRuns(1, ['project-1'], toast, vi.fn()))
  act(() => { change(); published(); published('foreign', 'other'); published('foreign', 'project-1', 2) })
  await act(async () => baseline.resolve({ items: [], nextCursor: null })); await settle()
  expect(fetchRunStatus).toHaveBeenCalledExactlyOnceWith('project-1', 'run', expect.any(AbortSignal))
  expect(toast.mock.calls.filter(call => call[0] === 'success')).toHaveLength(1)
})

it('匹配提交回执的终态先于 POST 回复时立即解除 loading，公开后提示一次', async () => {
  const reply = deferred<AutomationRunRecord>(), toast = vi.fn()
  vi.mocked(executeTask).mockReturnValue(reply.promise)
  const done = runFixture({ updatedAt: '2030-01-01T00:00:02Z', conversation: { state: 'pending' } })
  vi.mocked(fetchRunStatus).mockResolvedValue(done)
  const { result } = renderHook(() => useAutomationRuns(1, ['project-1'], toast, vi.fn()))
  await settle()
  let submitted!: Promise<void>
  act(() => { submitted = result.current.execute(taskFixture()) })
  await settle()
  expect(result.current.states.get('news')).toBe('submitting')
  vi.mocked(fetchSubmission).mockResolvedValue(done)
  act(() => change()); await settle()
  expect(result.current.states.get('news')).toBe('succeeded')
  expect(fetchSubmission).toHaveBeenLastCalledWith('project-1', 'news', vi.mocked(executeTask).mock.calls[0][1], expect.any(AbortSignal))
  expect(toast).not.toHaveBeenCalled()
  await act(async () => { reply.resolve(runFixture({ status: 'queued', updatedAt: '2030-01-01T00:00:00Z' })); await submitted })
  await settle()
  expect(result.current.states.get('news')).toBe('succeeded')
  expect(toast).not.toHaveBeenCalled()
  vi.mocked(fetchRunStatus).mockResolvedValue({ ...done, conversation: { state: 'available', projectId: 'project-1', threadId: 'conversation' } })
  act(() => published()); await settle()
  expect(toast.mock.calls.filter(call => call[0] === 'success')).toHaveLength(1)
})

it('会话公开早于提交响应时，迟到的 pending 不会重新启动开放等待', async () => {
  const reply = deferred<AutomationRunRecord>(), toast = vi.fn()
  vi.mocked(executeTask).mockReturnValue(reply.promise)
  const done = runFixture({ conversation: { state: 'available', projectId: 'project-1', threadId: 'conversation' } })
  vi.mocked(fetchRunStatus).mockResolvedValue(done)
  const { result } = renderHook(() => useAutomationRuns(1, ['project-1'], toast, vi.fn()))
  await settle()
  let submitted!: Promise<void>
  act(() => { submitted = result.current.execute(taskFixture()) })
  vi.mocked(fetchSubmission).mockResolvedValue(done)
  act(() => change()); await settle()
  expect(result.current.states.get('news')).toBe('succeeded')
  await act(async () => { reply.resolve({ ...done, conversation: { state: 'pending' } }); await submitted })
  await settle()
  await act(() => vi.advanceTimersByTimeAsync(90_000))
  expect(result.current.states.get('news')).toBe('succeeded')
  expect(fetchRunStatus).toHaveBeenCalledOnce()
  expect(toast.mock.calls.filter(call => call[0] === 'success')).toHaveLength(1)
})

it.each(['manual', 'scheduled'])('同一任务的其他 %s 终态没有匹配回执时不能解除本页提交', async trigger => {
  const reply = deferred<AutomationRunRecord>()
  vi.mocked(executeTask).mockReturnValue(reply.promise)
  vi.mocked(fetchRunStatus).mockResolvedValue(runFixture({ id: 'other-run', trigger, conversation: { state: 'pending' } }))
  const { result, unmount } = renderHook(() => useAutomationRuns(1, ['project-1'], vi.fn(), vi.fn()))
  await settle()
  let submitted!: Promise<void>
  act(() => { submitted = result.current.execute(taskFixture()) })
  act(() => change('other-run')); await settle()
  expect(result.current.states.get('news')).toBe('submitting')
  expect(fetchSubmission).toHaveBeenCalledWith('project-1', 'news', vi.mocked(executeTask).mock.calls[0][1], expect.any(AbortSignal))
  unmount()
  await act(async () => { reply.resolve(runFixture()); await submitted })
})

it.each(['queued', 'running'] as const)('匹配回执为 %s 时保持运行中状态，迟到 POST 不重复提示入队', async status => {
  const reply = deferred<AutomationRunRecord>(), toast = vi.fn()
  const active = runFixture({ status, conversation: { state: 'pending' }, finishedAt: null })
  vi.mocked(executeTask).mockReturnValue(reply.promise)
  vi.mocked(fetchSubmission).mockResolvedValue(active)
  vi.mocked(fetchRunStatus).mockResolvedValue(active)
  const { result } = renderHook(() => useAutomationRuns(1, ['project-1'], toast, vi.fn()))
  await settle()
  let submitted!: Promise<void>
  act(() => { submitted = result.current.execute(taskFixture()) }); await settle()
  expect(result.current.states.get('news')).toBe(status)
  expect(toast).toHaveBeenCalledExactlyOnceWith('info', '已加入运行队列')
  await act(async () => { reply.resolve(active); await submitted }); await settle()
  expect(result.current.states.get('news')).toBe(status)
  expect(toast).toHaveBeenCalledOnce()
  expect(executeTask).toHaveBeenCalledOnce()
})

it.each(['success', 'failure'] as const)('旧 POST 的迟到 %s 不影响已开始的下一次提交', async outcome => {
  const first = deferred<AutomationRunRecord>(), second = deferred<AutomationRunRecord>(), toast = vi.fn()
  vi.mocked(executeTask).mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
  const done = runFixture({ conversation: { state: 'pending' } })
  vi.mocked(fetchRunStatus).mockResolvedValue(done)
  const { result } = renderHook(() => useAutomationRuns(1, ['project-1'], toast, vi.fn()))
  await settle()
  let firstRequest!: Promise<void>, secondRequest!: Promise<void>
  act(() => { firstRequest = result.current.execute(taskFixture()) }); await settle()
  const firstId = vi.mocked(executeTask).mock.calls[0][1]
  vi.mocked(fetchSubmission).mockImplementation(async (_project, _task, requestId) => requestId === firstId ? done : null)
  act(() => change()); await settle()
  expect(result.current.states.get('news')).toBe('succeeded')
  act(() => { secondRequest = result.current.execute(taskFixture()) }); await settle()
  expect(vi.mocked(executeTask).mock.calls[1][1]).not.toBe(firstId)
  expect(result.current.states.get('news')).toBe('submitting')
  await act(async () => {
    if (outcome === 'success') first.resolve(runFixture({ status: 'queued' }))
    else first.reject(new Error('late response failed'))
    await firstRequest
  })
  expect(result.current.states.get('news')).toBe('submitting')
  expect(toast).not.toHaveBeenCalled()
  const active = runFixture({ id: 'next-run', status: 'queued', conversation: { state: 'pending' }, finishedAt: null })
  vi.mocked(fetchRunStatus).mockResolvedValue(active)
  await act(async () => { second.resolve(active); await secondRequest }); await settle()
  expect(result.current.states.get('news')).toBe('queued')
  expect(toast).toHaveBeenCalledExactlyOnceWith('info', '已加入运行队列')
})

it('旧回执读取在途时 POST 已确认并开始新提交，旧回执不能释放新提交', async () => {
  const first = deferred<AutomationRunRecord>(), second = deferred<AutomationRunRecord>(), receipt = deferred<AutomationRunRecord | null>()
  const toast = vi.fn()
  vi.mocked(executeTask).mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
  vi.mocked(fetchSubmission).mockReturnValueOnce(receipt.promise)
  const { result, unmount } = renderHook(() => useAutomationRuns(1, ['project-1'], toast, vi.fn()))
  await settle()
  let firstRequest!: Promise<void>, secondRequest!: Promise<void>
  act(() => { firstRequest = result.current.execute(taskFixture()) }); await settle()
  const done = runFixture()
  await act(async () => { first.resolve(done); await firstRequest })
  act(() => { secondRequest = result.current.execute(taskFixture()) }); await settle()
  expect(result.current.states.get('news')).toBe('submitting')
  await act(async () => receipt.resolve(done)); await settle()
  expect(result.current.states.get('news')).toBe('submitting')
  expect(toast.mock.calls.filter(call => call[0] === 'success')).toHaveLength(1)
  unmount()
  await act(async () => { second.resolve(runFixture({ id: 'next-run' })); await secondRequest })
})

it('未知提交通过只读回执校准恢复，null 保留待确认且不自动重发 POST', async () => {
  vi.mocked(executeTask).mockRejectedValueOnce(new Error('network'))
  const toast = vi.fn(), done = runFixture({ conversation: { state: 'pending' } })
  const { result } = renderHook(() => useAutomationRuns(1, ['project-1'], toast, vi.fn()))
  await settle(); await act(async () => result.current.execute(taskFixture())); await settle()
  expect(result.current.states.get('news')).toBe('uncertain')
  vi.mocked(fetchSubmission).mockResolvedValue(done)
  await act(() => vi.advanceTimersByTimeAsync(30_000))
  expect(result.current.states.get('news')).toBe('succeeded')
  expect(executeTask).toHaveBeenCalledOnce()
  expect(fetchSubmission).toHaveBeenCalledTimes(2)
  expect(toast.mock.calls.filter(call => call[0] === 'success')).toHaveLength(0)
})

it.each(['reconnect', 'identity'] as const)('提交回执在途时 %s 不应用旧查询或补发完成提示', async mode => {
  const post = deferred<AutomationRunRecord>(), receipt = deferred<AutomationRunRecord | null>(), toast = vi.fn()
  vi.mocked(executeTask).mockReturnValue(post.promise)
  vi.mocked(fetchSubmission).mockReturnValueOnce(receipt.promise)
  const { result } = renderHook(() => useAutomationRuns(1, ['project-1'], toast, vi.fn()))
  await settle()
  let submitted!: Promise<void>
  act(() => { submitted = result.current.execute(taskFixture()) }); await settle()
  if (mode === 'reconnect') act(() => { emit({ kind: 'connection', state: 'disconnected' }); emit({ kind: 'resync' }) })
  else act(() => saveAuthSession(createAuthSession({ access_token: 'replacement-session', token_type: 'Bearer', expires_at: '2099-01-01T00:00:00Z',
    user: { user_id: 1, username: 'owner', avatar_url: null, roles: [], disabled: false } })))
  const done = runFixture({ conversation: { state: 'available', projectId: 'project-1', threadId: 'conversation' } })
  await act(async () => receipt.resolve(done)); await settle()
  expect(result.current.states.get('news')).toBe(mode === 'reconnect' ? 'submitting' : undefined)
  expect(toast).not.toHaveBeenCalled()
  await act(async () => { post.resolve(done); await submitted }); await settle()
  expect(result.current.states.get('news')).toBe(mode === 'reconnect' ? 'succeeded' : undefined)
  expect(toast).not.toHaveBeenCalled()
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
