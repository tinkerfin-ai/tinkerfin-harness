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

it('pending 终态立即解除执行按钮 loading 并提示一次，Toast 不包含会话操作', async () => {
  const reply = deferred<AutomationRunRecord>(), toast = vi.fn(), task = taskFixture()
  vi.mocked(executeTask).mockReturnValue(reply.promise)
  const queued = runFixture({ status: 'queued', updatedAt: '2030-01-01T00:00:00Z', finishedAt: null })
  vi.mocked(fetchRunStatus).mockResolvedValue(queued)
  function Harness() {
    const runs = useAutomationRuns(1, ['project-1'], toast)
    return <AutomationTaskList tasks={[task]} query="" status="all" busy={new Set()} executions={runs.states} batch={false} onFinishSelection={vi.fn()} onEdit={vi.fn()}
      onExecute={() => { void runs.execute(task) }} onToggle={vi.fn()} onPause={async () => new Set()} onDelete={async () => new Set()} onClearFilter={vi.fn()} />
  }
  render(<Harness />); await settle()
  const button = screen.getByRole('button', { name: `执行 ${task.name}` })
  fireEvent.click(button)
  expect(button).toBeDisabled(); expect(button).toHaveAttribute('aria-busy', 'true')
  await act(async () => reply.resolve(queued)); await settle()
  expect(button).toBeDisabled(); expect(screen.getByRole('status')).toHaveTextContent('排队中')
  const done = runFixture({ updatedAt: '2030-01-01T00:00:01Z', conversation: { state: 'pending' } })
  vi.mocked(fetchRunStatus).mockResolvedValue(done)
  act(() => change()); await settle()
  expect(button).toBeEnabled(); expect(screen.getByRole('status')).toHaveTextContent('运行完成')
  expect(toast.mock.calls).toEqual([
    ['info', '已加入运行队列'],
    ['success', expect.stringContaining('运行完成')],
  ])
  const reads = vi.mocked(fetchRunStatus).mock.calls.length
  vi.mocked(fetchRunStatus).mockResolvedValue({ ...done, conversation: { state: 'available', projectId: 'project-1', threadId: 'conversation' } })
  act(() => published()); await settle()
  expect(fetchRunStatus).toHaveBeenCalledTimes(reads)
  act(() => change()); await settle()
  expect(toast.mock.calls).toEqual([
    ['info', '已加入运行队列'],
    ['success', expect.stringContaining('运行完成')],
  ])
})

it('在线失败在会话 pending 时立即提示一次并停止轮询', async () => {
  const toast = vi.fn()
  const failure = runFixture({ status: 'failed', conversation: { state: 'pending' } })
  vi.mocked(fetchRunStatus).mockResolvedValue(failure)
  const { result } = renderHook(() => useAutomationRuns(1, ['project-1'], toast))
  await settle(); act(() => change()); await settle()
  expect(result.current.states.get('news')).toBe('failed')
  expect(toast).toHaveBeenCalledExactlyOnceWith('error', expect.stringContaining('运行失败'))
  act(() => published()); await settle()
  await act(() => vi.advanceTimersByTimeAsync(90_000))
  expect(fetchRunStatus).toHaveBeenCalledExactlyOnceWith('project-1', 'run', expect.any(AbortSignal))
  expect(fetchActiveRunPage).toHaveBeenCalledOnce()
  expect(toast).toHaveBeenCalledExactlyOnceWith('error', expect.stringContaining('运行失败'))
  act(() => change()); await settle()
  expect(toast).toHaveBeenCalledExactlyOnceWith('error', expect.stringContaining('运行失败'))
})

it.each(['initial', 'reconnect', 'identity', 'hidden'] as const)('%s 基线与会话公开不会补弹完成提示', async mode => {
  const toast = vi.fn()
  vi.mocked(fetchActiveRunPage).mockResolvedValueOnce({ items: [runFixture({ status: 'running', conversation: { state: 'pending' } })], nextCursor: null })
  vi.mocked(fetchRunStatus).mockResolvedValue(runFixture({ conversation: { state: 'pending' } }))
  const { result } = renderHook(() => useAutomationRuns(1, ['project-1'], toast))
  await settle()
  expect(result.current.states.get('news')).toBe('running')
  if (mode === 'initial') { act(() => result.current.refresh()); await settle() }
  if (mode === 'reconnect') {
    act(() => emit({ kind: 'connection', state: 'disconnected' }))
    act(() => emit({ kind: 'resync' })); await settle()
  }
  if (mode === 'identity') {
    act(() => {
      saveAuthSession(createAuthSession({ access_token: 'new-session', token_type: 'Bearer', expires_at: '2099-01-01T00:00:00Z',
        user: { user_id: 1, username: 'owner', avatar_url: null, roles: [], disabled: false } }))
      emit({ kind: 'resync' })
    }); await settle()
  }
  if (mode === 'hidden') {
    const hidden = vi.spyOn(document, 'hidden', 'get').mockReturnValue(true)
    act(() => { document.dispatchEvent(new Event('visibilitychange')); change() }); await settle()
    expect(fetchRunStatus).not.toHaveBeenCalled()
    hidden.mockReturnValue(false)
    act(() => { document.dispatchEvent(new Event('visibilitychange')); emit({ kind: 'resync' }) })
    await settle()
  }
  expect(result.current.states.get('news')).toBe(mode === 'identity' ? undefined : 'succeeded')
  expect(toast).not.toHaveBeenCalled()
  const reads = vi.mocked(fetchRunStatus).mock.calls.length
  const baselines = vi.mocked(fetchActiveRunPage).mock.calls.length
  vi.mocked(fetchRunStatus).mockResolvedValue(runFixture({ conversation: { state: 'available', projectId: 'project-1', threadId: 'conversation' } }))
  act(() => published()); await settle()
  expect(fetchRunStatus).toHaveBeenCalledTimes(reads)
  expect(fetchActiveRunPage).toHaveBeenCalledTimes(baselines)
  expect(toast).not.toHaveBeenCalled()
})

it('读取期间的在线终态仍提示，并忽略会话发布与其他身份或项目事件', async () => {
  const baseline = deferred<{ items: AutomationRunRecord[]; nextCursor: null }>(), toast = vi.fn()
  vi.mocked(fetchActiveRunPage).mockReturnValueOnce(baseline.promise)
  vi.mocked(fetchRunStatus).mockResolvedValue(runFixture({ conversation: { state: 'available', projectId: 'moved', threadId: 'conversation' } }))
  renderHook(() => useAutomationRuns(1, ['project-1'], toast))
  act(() => { change(); published(); change('foreign', 'other'); change('foreign', 'project-1', 2) })
  await act(async () => baseline.resolve({ items: [], nextCursor: null })); await settle()
  expect(fetchRunStatus).toHaveBeenCalledExactlyOnceWith('project-1', 'run', expect.any(AbortSignal))
  expect(toast).toHaveBeenCalledExactlyOnceWith('success', expect.stringContaining('运行完成'))
  act(() => { published(); published('foreign', 'other'); published('foreign', 'project-1', 2) }); await settle()
  expect(fetchRunStatus).toHaveBeenCalledOnce()
  expect(toast).toHaveBeenCalledOnce()
})

it('匹配提交回执的 pending 终态先于 POST 回复时立即解除 loading 并提示一次', async () => {
  const reply = deferred<AutomationRunRecord>(), toast = vi.fn()
  vi.mocked(executeTask).mockReturnValue(reply.promise)
  const done = runFixture({ updatedAt: '2030-01-01T00:00:02Z', conversation: { state: 'pending' } })
  vi.mocked(fetchRunStatus).mockResolvedValue(done)
  const { result } = renderHook(() => useAutomationRuns(1, ['project-1'], toast))
  await settle()
  let submitted!: Promise<void>
  act(() => { submitted = result.current.execute(taskFixture()) })
  await settle()
  expect(result.current.states.get('news')).toBe('submitting')
  vi.mocked(fetchSubmission).mockResolvedValue(done)
  act(() => change()); await settle()
  expect(result.current.states.get('news')).toBe('succeeded')
  expect(fetchSubmission).toHaveBeenLastCalledWith('project-1', 'news', vi.mocked(executeTask).mock.calls[0][1], expect.any(AbortSignal))
  expect(toast).toHaveBeenCalledExactlyOnceWith('success', expect.stringContaining('运行完成'))
  await act(async () => { reply.resolve(runFixture({ status: 'queued', updatedAt: '2030-01-01T00:00:00Z' })); await submitted })
  await settle()
  expect(result.current.states.get('news')).toBe('succeeded')
  expect(toast).toHaveBeenCalledExactlyOnceWith('success', expect.stringContaining('运行完成'))
  const reads = vi.mocked(fetchRunStatus).mock.calls.length
  vi.mocked(fetchRunStatus).mockResolvedValue({ ...done, conversation: { state: 'available', projectId: 'project-1', threadId: 'conversation' } })
  act(() => published()); await settle()
  expect(fetchRunStatus).toHaveBeenCalledTimes(reads)
  expect(toast).toHaveBeenCalledExactlyOnceWith('success', expect.stringContaining('运行完成'))
})

it.each(['manual', 'scheduled'])('同一任务的其他 %s 终态没有匹配回执时不能解除本页提交', async trigger => {
  const reply = deferred<AutomationRunRecord>()
  vi.mocked(executeTask).mockReturnValue(reply.promise)
  vi.mocked(fetchRunStatus).mockResolvedValue(runFixture({ id: 'other-run', trigger, conversation: { state: 'pending' } }))
  const { result, unmount } = renderHook(() => useAutomationRuns(1, ['project-1'], vi.fn()))
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
  const { result } = renderHook(() => useAutomationRuns(1, ['project-1'], toast))
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
  const { result } = renderHook(() => useAutomationRuns(1, ['project-1'], toast))
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
  expect(toast).toHaveBeenCalledExactlyOnceWith('success', expect.stringContaining('运行完成'))
  const active = runFixture({ id: 'next-run', status: 'queued', conversation: { state: 'pending' }, finishedAt: null })
  vi.mocked(fetchRunStatus).mockResolvedValue(active)
  await act(async () => { second.resolve(active); await secondRequest }); await settle()
  expect(result.current.states.get('news')).toBe('queued')
  expect(toast.mock.calls).toEqual([
    ['success', expect.stringContaining('运行完成')],
    ['info', '已加入运行队列'],
  ])
})

it('旧回执读取在途时 POST 已确认并开始新提交，旧回执不能释放新提交', async () => {
  const first = deferred<AutomationRunRecord>(), second = deferred<AutomationRunRecord>(), receipt = deferred<AutomationRunRecord | null>()
  const toast = vi.fn()
  vi.mocked(executeTask).mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
  vi.mocked(fetchSubmission).mockReturnValueOnce(receipt.promise)
  const { result, unmount } = renderHook(() => useAutomationRuns(1, ['project-1'], toast))
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
  const { result } = renderHook(() => useAutomationRuns(1, ['project-1'], toast))
  await settle(); await act(async () => result.current.execute(taskFixture())); await settle()
  expect(result.current.states.get('news')).toBe('uncertain')
  vi.mocked(fetchSubmission).mockResolvedValue(done)
  await act(() => vi.advanceTimersByTimeAsync(30_000))
  expect(result.current.states.get('news')).toBe('succeeded')
  expect(executeTask).toHaveBeenCalledOnce()
  expect(fetchSubmission).toHaveBeenCalledTimes(2)
  expect(toast.mock.calls.filter(call => call[0] === 'success')).toEqual([['success', expect.stringContaining('运行完成')]])
})

it.each(['reconnect', 'identity'] as const)('提交回执在途时 %s 不应用旧查询或补发完成提示', async mode => {
  const post = deferred<AutomationRunRecord>(), receipt = deferred<AutomationRunRecord | null>(), toast = vi.fn()
  vi.mocked(executeTask).mockReturnValue(post.promise)
  vi.mocked(fetchSubmission).mockReturnValueOnce(receipt.promise)
  const { result } = renderHook(() => useAutomationRuns(1, ['project-1'], toast))
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
  const { result, rerender } = renderHook(({ projects }) => useAutomationRuns(1, projects, toast), { initialProps: { projects: ['project-1', 'second'] } })
  await settle(); expect(result.current.states.get('news')).toBe('running'); expect(toast).not.toHaveBeenCalled()
  act(() => emit({ kind: 'connection', state: 'disconnected' }))
  vi.mocked(fetchRunStatus).mockResolvedValue(runFixture({ projectId: 'second' }))
  act(() => emit({ kind: 'resync' })); await settle()
  expect(result.current.states.get('news')).toBe('succeeded'); expect(toast).not.toHaveBeenCalled()
  rerender({ projects: ['second', 'project-1'] })
  vi.mocked(fetchRunStatus).mockResolvedValue(runFixture({ id: 'scheduled', taskId: 'scheduled-task', projectId: 'second', status: 'failed' }))
  act(() => change('scheduled', 'second')); await settle()
  expect(toast).toHaveBeenCalledExactlyOnceWith('error', expect.stringContaining('运行失败'))
  const count = vi.mocked(fetchRunStatus).mock.calls.length
  act(() => change('foreign', 'project-1', 2)); await settle()
  expect(fetchRunStatus).toHaveBeenCalledTimes(count)
})

it('未知提交重试保留原命令与请求 ID，任务编辑不改写它，卸载取消在途请求', async () => {
  const task = taskFixture(), toast = vi.fn()
  vi.mocked(executeTask).mockRejectedValueOnce(new Error('network'))
  const { result, unmount } = renderHook(() => useAutomationRuns(1, ['project-1'], toast))
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
  const { result } = renderHook(() => useAutomationRuns(1, ['project-1'], toast))
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
  const { result } = renderHook(() => useAutomationRuns(1, ['project-1'], toast))
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
  const { result } = renderHook(() => useAutomationRuns(1, ['project-1'], toast))
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
  const { result } = renderHook(() => useAutomationRuns(1, ['project-1'], vi.fn()))
  await settle()
  let submission!: Promise<void>
  act(() => { submission = result.current.execute(taskFixture()) })
  act(() => change()); await settle()
  await act(async () => { response.resolve(runFixture({ status: 'queued', updatedAt: '2030-01-01T00:00:00.000100Z' })); await submission })
  expect(result.current.states.get('news')).toBe('running')
})
