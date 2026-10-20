import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import * as auth from '../../auth/session'
import type { AuthSession } from '../../auth/session'
import { DeviceAuthorization } from './DeviceAuthorization'
import * as api from './api'
import { prepareDeviceAuthorization, readDeviceAuthorizationIntent } from './authorizationRoute'
import { getServerAddress, setServerAddress } from '../../api/shared/config'
import { ApiError } from '../../api/shared/http'

const intent = { id: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', browserToken: 'c'.repeat(64) }
const session: AuthSession = { serverAddress: 'http://127.0.0.1:8090', token: 'login-token', tokenType: 'Bearer', expiresAt: '2100-01-01T00:00:00Z', user: { user_id: 1, username: 'demo', avatar_url: null, roles: [], disabled: false } }
const onExit = vi.fn<(id: string) => void>()
const preview: api.DeviceAuthorizationPreview = { authorization_id: intent.id, name: '办公室 Mac', expires_at: '2100-01-01T00:00:00Z', state: 'waiting' }

function deferred<T>() {
  let resolve: (value: T) => void = () => undefined
  let reject: (error: unknown) => void = () => undefined
  const promise = new Promise<T>((complete, fail) => { resolve = complete; reject = fail })
  return { promise, resolve, reject }
}

beforeEach(() => { onExit.mockClear(); vi.spyOn(auth, 'getAuthSession').mockReturnValue(session) })
afterEach(() => { cleanup(); vi.restoreAllMocks(); window.history.replaceState(null, '', '/'); localStorage.clear() })

it('登录后的批准固定当前账号和浏览器能力，未提前显示成功', async () => {
  const approval = deferred<api.DeviceAuthorizationPreview>()
  const request = vi.spyOn(api, 'requestDeviceAuthorization').mockResolvedValueOnce(preview).mockReturnValueOnce(approval.promise)
  const open = vi.spyOn(window, 'open').mockReturnValue(null)
  render(<DeviceAuthorization intent={intent} session={session} checking={false} pending={false} onLogin={vi.fn()} onExit={onExit} />)
  expect(await screen.findByText('办公室 Mac')).toBeInTheDocument()
  const authorize = screen.getByRole('button', { name: '授权' })
  fireEvent.click(authorize)
  expect(authorize).toHaveAttribute('aria-busy', 'true')
  expect(authorize).toBeDisabled()
  expect(screen.getByRole('button', { name: '取消' })).toBeDisabled()
  expect(screen.queryByText('授权成功，你可以关闭此页面')).not.toBeInTheDocument()
  fireEvent.click(authorize)
  expect(request).toHaveBeenCalledTimes(2)
  await act(async () => approval.resolve({ ...preview, state: 'authorized' }))
  expect(screen.getByRole('status')).toHaveTextContent('授权成功，你可以关闭此页面')
  expect(screen.queryByText('demo')).not.toBeInTheDocument()
  expect(screen.queryByText('办公室 Mac')).not.toBeInTheDocument()
  expect(screen.queryAllByRole('button')).toHaveLength(0)
  expect(onExit).not.toHaveBeenCalled()
  expect(open).not.toHaveBeenCalled()
  expect(request).toHaveBeenLastCalledWith(intent.id, 'approve', intent.browserToken, 'Bearer login-token', 1, expect.any(AbortSignal))
})

it('重新读取已授权入口时仅显示成功提示，不唤起客户端', async () => {
  const request = vi.spyOn(api, 'requestDeviceAuthorization').mockResolvedValue({ ...preview, state: 'authorized' })
  const open = vi.spyOn(window, 'open').mockReturnValue(null)
  render(<DeviceAuthorization intent={intent} session={session} checking={false} pending={false} onLogin={vi.fn()} onExit={onExit} />)
  expect(await screen.findByText('授权成功，你可以关闭此页面')).toBeInTheDocument()
  expect(request).toHaveBeenCalledOnce()
  expect(request).toHaveBeenCalledWith(intent.id, 'preview', intent.browserToken, 'Bearer login-token', 1, expect.any(AbortSignal))
  expect(open).not.toHaveBeenCalled()
  expect(screen.queryAllByRole('button')).toHaveLength(0)
  expect(onExit).not.toHaveBeenCalled()
})

it('未登录取消只撤销浏览器入口，不要求填写账号或服务地址', async () => {
  vi.mocked(auth.getAuthSession).mockReturnValue(null)
  const deny = vi.spyOn(api, 'denyDeviceAuthorization').mockResolvedValue({ ...preview, state: 'cancelled' })
  render(<DeviceAuthorization intent={intent} session={null} checking={false} pending={false} onLogin={vi.fn()} onExit={onExit} />)
  expect(screen.queryByLabelText('服务器地址')).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '取消' }))
  await waitFor(() => expect(onExit).toHaveBeenCalledWith(intent.id))
  expect(deny).toHaveBeenCalledWith(intent.id, intent.browserToken, expect.any(AbortSignal))
})

it.each([true, false])('取消时另一账号已批准，登录状态 %s 仍退出而不显示成功', async signedIn => {
  const cancellation = deferred<api.DeviceAuthorizationPreview>()
  vi.mocked(auth.getAuthSession).mockReturnValue(signedIn ? session : null)
  vi.spyOn(api, 'requestDeviceAuthorization').mockResolvedValueOnce(preview).mockReturnValueOnce(cancellation.promise)
  vi.spyOn(api, 'denyDeviceAuthorization').mockReturnValue(cancellation.promise)
  const open = vi.spyOn(window, 'open').mockReturnValue(null)
  render(<DeviceAuthorization intent={intent} session={signedIn ? session : null} checking={false} pending={false} onLogin={vi.fn()} onExit={onExit} />)
  if (signedIn) await screen.findByText('办公室 Mac')
  fireEvent.click(screen.getByRole('button', { name: '取消' }))
  await act(async () => cancellation.resolve({ ...preview, state: 'authorized' }))
  expect(onExit).toHaveBeenCalledWith(intent.id)
  expect(screen.queryByText('授权成功，你可以关闭此页面')).not.toBeInTheDocument()
  expect(screen.queryAllByRole('button')).toHaveLength(0)
  expect(open).not.toHaveBeenCalled()
})

it('已登录取消失败后重试保留取消意图，已批准结果不显示成功', async () => {
  const request = vi.spyOn(api, 'requestDeviceAuthorization').mockResolvedValueOnce(preview)
    .mockRejectedValueOnce(new ApiError('offline', { status: 0 }))
    .mockResolvedValueOnce({ ...preview, state: 'authorized' })
  render(<DeviceAuthorization intent={intent} session={session} checking={false} pending={false} onLogin={vi.fn()} onExit={onExit} />)
  fireEvent.click(await screen.findByRole('button', { name: '取消' }))
  fireEvent.click(await screen.findByRole('button', { name: '重试' }))
  await waitFor(() => expect(request).toHaveBeenCalledTimes(3))
  expect(request.mock.calls.map(call => call[1])).toEqual(['preview', 'deny', 'deny'])
  await waitFor(() => expect(onExit).toHaveBeenCalledWith(intent.id))
  expect(screen.queryByText('授权成功，你可以关闭此页面')).not.toBeInTheDocument()
})

it('未登录取消失败后重试继续撤销，不要求登录', async () => {
  vi.mocked(auth.getAuthSession).mockReturnValue(null)
  const deny = vi.spyOn(api, 'denyDeviceAuthorization').mockRejectedValueOnce(new ApiError('offline', { status: 0 }))
    .mockResolvedValue({ ...preview, state: 'cancelled' })
  const onLogin = vi.fn()
  render(<DeviceAuthorization intent={intent} session={null} checking={false} pending={false} onLogin={onLogin} onExit={onExit} />)
  fireEvent.click(screen.getByRole('button', { name: '取消' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('授权暂不可用，请重试')
  fireEvent.click(screen.getByRole('button', { name: '重试' }))
  await waitFor(() => expect(onExit).toHaveBeenCalledWith(intent.id))
  expect(deny).toHaveBeenCalledTimes(2)
  expect(onLogin).not.toHaveBeenCalled()
})

it('离开授权页取消在途响应，失败后仍可重试', async () => {
  const request = vi.spyOn(api, 'requestDeviceAuthorization').mockRejectedValueOnce(new ApiError('offline', { status: 0 }))
  const { unmount } = render(<DeviceAuthorization intent={intent} session={session} checking={false} pending={false} onLogin={vi.fn()} onExit={onExit} />)
  expect(await screen.findByRole('alert')).toHaveTextContent('授权暂不可用，请重试')
  let finish: (value: api.DeviceAuthorizationPreview) => void = () => undefined
  request.mockImplementation(() => new Promise(resolve => { finish = resolve }))
  fireEvent.click(screen.getByRole('button', { name: '重试' }))
  expect(screen.getByRole('status')).toHaveTextContent('正在读取连接请求')
  const signal = request.mock.calls.at(-1)![5]
  unmount()
  expect(signal.aborted).toBe(true)
  await act(async () => finish(preview))
})

it.each([
  [1_001_012_000, 404],
  [1_001_012_002, 409],
  [1_001_012_001, 410],
] as const)('不可恢复的授权业务码 %s 交回宿主且不显示结果页', async (code, status) => {
  const request = vi.spyOn(api, 'requestDeviceAuthorization').mockRejectedValue(new ApiError('其他账号的内部信息', { code, status }))
  render(<DeviceAuthorization intent={intent} session={session} checking={false} pending={false} onLogin={vi.fn()} onExit={onExit} />)
  await waitFor(() => expect(onExit).toHaveBeenCalledWith(intent.id))
  expect(screen.queryByRole('main')).not.toBeInTheDocument()
  expect(screen.queryByRole('button')).not.toBeInTheDocument()
  expect(screen.queryByText('其他账号的内部信息')).not.toBeInTheDocument()
  expect(request).toHaveBeenCalledOnce()
})

it('重新核验提交结果期间明确等待并阻止再次授权或取消', async () => {
  const retry = deferred<api.DeviceAuthorizationPreview>()
  const request = vi.spyOn(api, 'requestDeviceAuthorization')
    .mockResolvedValueOnce(preview)
    .mockRejectedValueOnce(new ApiError('unavailable', { status: 503 }))
    .mockReturnValueOnce(retry.promise)
  render(<DeviceAuthorization intent={intent} session={session} checking={false} pending={false} onLogin={vi.fn()} onExit={onExit} />)
  fireEvent.click(await screen.findByRole('button', { name: '授权' }))
  fireEvent.click(await screen.findByRole('button', { name: '重试' }))
  expect(screen.getByRole('status')).toHaveTextContent('正在读取连接请求')
  expect(screen.getByRole('button', { name: '授权' })).toBeDisabled()
  expect(screen.getByRole('button', { name: '取消' })).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: '授权' }))
  expect(request).toHaveBeenCalledTimes(3)
  await act(async () => retry.resolve(preview))
  expect(screen.getByRole('button', { name: '授权' })).toBeEnabled()
})

it('同一身份资料更新不取消正在提交的授权，也不重复预览', async () => {
  const approval = deferred<api.DeviceAuthorizationPreview>()
  const request = vi.spyOn(api, 'requestDeviceAuthorization').mockResolvedValueOnce(preview).mockReturnValueOnce(approval.promise)
  const props = { intent, checking: false, pending: false, onLogin: vi.fn(), onExit }
  const view = render(<DeviceAuthorization {...props} session={session} />)
  fireEvent.click(await screen.findByRole('button', { name: '授权' }))
  const refreshed = { ...session, user: { ...session.user, username: '更新后的名字' } }
  vi.mocked(auth.getAuthSession).mockReturnValue(refreshed)
  view.rerender(<DeviceAuthorization {...props} intent={{ ...intent }} session={refreshed} />)
  expect(screen.getByText('更新后的名字')).toBeInTheDocument()
  expect(request.mock.calls[1][5].aborted).toBe(false)
  expect(request).toHaveBeenCalledTimes(2)
  await act(async () => approval.resolve({ ...preview, state: 'authorized' }))
  expect(screen.getByRole('status')).toBeInTheDocument()
  expect(onExit).not.toHaveBeenCalled()
})

it.each(['token', 'user', 'intent'] as const)('更换 %s 后取消旧请求，晚到结果不能覆盖当前入口', async boundary => {
  const approval = deferred<api.DeviceAuthorizationPreview>()
  const nextPreview = deferred<api.DeviceAuthorizationPreview>()
  const request = vi.spyOn(api, 'requestDeviceAuthorization')
    .mockResolvedValueOnce(preview).mockReturnValueOnce(approval.promise).mockReturnValueOnce(nextPreview.promise)
  const props = { checking: false, pending: false, onLogin: vi.fn(), onExit }
  const view = render(<DeviceAuthorization {...props} intent={intent} session={session} />)
  fireEvent.click(await screen.findByRole('button', { name: '授权' }))
  const nextSession = { ...session, token: boundary === 'token' ? 'replacement-token' : session.token,
    user: { ...session.user, user_id: boundary === 'user' ? 2 : session.user.user_id } }
  const nextIntent = boundary === 'intent' ? { ...intent, id: 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb' } : intent
  vi.mocked(auth.getAuthSession).mockReturnValue(nextSession)
  view.rerender(<DeviceAuthorization {...props} intent={nextIntent} session={nextSession} />)
  expect(request.mock.calls[1][5].aborted).toBe(true)
  expect(screen.queryByText('办公室 Mac')).not.toBeInTheDocument()
  await act(async () => nextPreview.resolve({ ...preview, authorization_id: nextIntent.id, name: '当前 Mac' }))
  await act(async () => {
    if (boundary === 'intent') approval.reject(new ApiError('expired', { code: 1_001_012_001, status: 410 }))
    else approval.resolve({ ...preview, state: 'authorized' })
  })
  expect(screen.getByText('当前 Mac')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '授权' })).toBeEnabled()
  expect(screen.queryByText('授权成功，你可以关闭此页面')).not.toBeInTheDocument()
  expect(onExit).not.toHaveBeenCalled()
})

it('取消只标记取消按钮为忙碌，重复点击不会发送第二次请求', async () => {
  const cancellation = deferred<api.DeviceAuthorizationPreview>()
  const request = vi.spyOn(api, 'requestDeviceAuthorization').mockResolvedValueOnce(preview).mockReturnValueOnce(cancellation.promise)
  render(<DeviceAuthorization intent={intent} session={session} checking={false} pending={false} onLogin={vi.fn()} onExit={onExit} />)
  const cancel = await screen.findByRole('button', { name: '取消' })
  fireEvent.click(cancel)
  expect(cancel).toHaveAttribute('aria-busy', 'true')
  expect(screen.getByRole('button', { name: '授权' })).not.toHaveAttribute('aria-busy')
  fireEvent.click(cancel)
  expect(request).toHaveBeenCalledTimes(2)
  await act(async () => cancellation.resolve({ ...preview, state: 'cancelled' }))
  await waitFor(() => expect(onExit).toHaveBeenCalledWith(intent.id))
})

it('授权 URL 不接受服务地址，初始化绑定部署配置', () => {
  setServerAddress('https://other.invalid')
  window.history.replaceState(null, '', `/?device_authorization=${intent.id}&server=https://attacker.invalid#token=${intent.browserToken}`)
  expect(readDeviceAuthorizationIntent()).toEqual(intent)
  prepareDeviceAuthorization()
  expect(getServerAddress()).toBe('http://127.0.0.1:8090')
})
