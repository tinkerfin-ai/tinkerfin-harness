import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { buildEmptyConversation } from '../../lib/workspace'
import { buildInitialPayload } from '../conversation/agui'
import { DeviceSettings } from './DeviceSettings'
import * as api from './api'
import type { LocalDevice } from './api'

const device: LocalDevice = {
  device_id: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', name: '我的 Mac', online: false, revoked: false,
  paired_at: '2030-01-01T00:00:00Z', last_seen_at: null,
  capabilities: { browser: true, mcp: false, desktop: false, desktop_capture: false, open_url: true },
}
beforeEach(() => { vi.spyOn(api, 'fetchDevices').mockResolvedValue([device]) })
afterEach(() => { cleanup(); vi.restoreAllMocks() })

it('设备由客户端发起连接，网页仅显示当前设备并明确撤销目标', async () => {
  const revoke = vi.spyOn(api, 'revokeDevice').mockResolvedValue(undefined)
  render(<DeviceSettings />)
  await screen.findByText('我的 Mac')
  expect(screen.getByText('未连接')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '打开客户端' })).toBeInTheDocument()
  expect(screen.queryByText('自动管理')).not.toBeInTheDocument()
  expect(screen.queryByText('~/.tinkerfin/projects')).not.toBeInTheDocument()
  expect(screen.queryByLabelText('配对码')).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '撤销授权' }))
  expect(screen.queryByRole('button', { name: '重连' })).not.toBeInTheDocument()
  expect(revoke).not.toHaveBeenCalled()
  expect(screen.getByRole('group', { name: '撤销授权' })).toHaveTextContent('再次使用需重新授权')
  fireEvent.click(screen.getByRole('button', { name: '撤销授权' }))
  await waitFor(() => expect(revoke).toHaveBeenCalledWith(device.device_id, expect.any(AbortSignal)))
})

it('新会话默认修改前确认且请求不携带设备目标', () => {
  const conversation = buildEmptyConversation({ projectId: 'project', now: '2030-01-01T00:00:00Z' })
  const payload = buildInitialPayload(conversation, 'work')
  expect(payload.forwardedProps.accessMode).toBe('write_approval')
  expect(payload.forwardedProps).not.toHaveProperty('localTarget')
})

it('刷新连接时禁用重复点击，完成后保留入口且不追加成功提示', async () => {
  let complete!: (devices: LocalDevice[]) => void
  const fetch = vi.spyOn(api, 'fetchDevices').mockResolvedValueOnce([])
    .mockImplementationOnce(() => new Promise(resolve => { complete = resolve }))
  const open = vi.spyOn(window, 'open').mockReturnValue(null)
  render(<DeviceSettings />)
  fireEvent.click(await screen.findByRole('button', { name: '打开客户端' }))
  const refresh = screen.getByRole('button', { name: '刷新连接状态' })
  fireEvent.click(refresh)
  expect(fetch).toHaveBeenCalledTimes(2)
  expect(refresh).toBeDisabled()
  expect(refresh).toHaveAttribute('aria-busy', 'true')
  fireEvent.click(refresh)
  expect(fetch).toHaveBeenCalledTimes(2)
  const launch = screen.getByRole('button', { name: '打开客户端' })
  expect(launch).toBeEnabled()
  fireEvent.click(launch)
  expect(open).toHaveBeenCalledTimes(2)
  await act(async () => { complete([]) })
  expect(refresh).toBeEnabled()
  expect(refresh).not.toHaveAttribute('aria-busy')
  expect(screen.getByRole('status')).toHaveTextContent('设备连接成功后会显示在这里')
  expect(screen.queryByText(/刷新完成/)).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '打开客户端' }))
  expect(open).toHaveBeenCalledTimes(3)
  expect(screen.getByRole('link', { name: '下载客户端' })).toHaveAttribute('href', 'https://github.com/tinkerfin-ai/tinkerfin-harness/releases/latest/download/TinkerFin.zip')
})

it('刷新失败后保留下载和打开入口，重试成功自动显示设备', async () => {
  const fetch = vi.spyOn(api, 'fetchDevices').mockResolvedValueOnce([])
    .mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce([{ ...device, online: true }])
  vi.spyOn(window, 'open').mockReturnValue(null)
  render(<DeviceSettings />)
  fireEvent.click(await screen.findByRole('button', { name: '打开客户端' }))
  fireEvent.click(screen.getByRole('button', { name: '刷新连接状态' }))
  await screen.findByText('设备列表暂不可用')
  expect(screen.getByRole('button', { name: '刷新连接状态' })).toBeEnabled()
  expect(screen.getByRole('button', { name: '打开客户端' })).toBeEnabled()
  expect(screen.getByRole('link', { name: '下载客户端' })).toBeVisible()
  fireEvent.click(screen.getByRole('button', { name: '重试' }))
  await screen.findByText('我的 Mac')
  expect(screen.queryByText('设备列表暂不可用')).not.toBeInTheDocument()
  expect(screen.getByRole('status')).toHaveTextContent('已连接')
  expect(screen.queryByRole('button', { name: '刷新连接状态' })).not.toBeInTheDocument()
  expect(fetch).toHaveBeenCalledTimes(3)
})

it('离线重连只打开客户端，真实在线结果到达后隐藏入口', async () => {
  const open = vi.spyOn(window, 'open').mockReturnValue(null)
  const revoke = vi.spyOn(api, 'revokeDevice').mockResolvedValue(undefined)
  vi.spyOn(api, 'fetchDevices').mockResolvedValueOnce([device]).mockResolvedValueOnce([{ ...device, online: true }])
  render(<DeviceSettings />)
  fireEvent.click(await screen.findByRole('button', { name: '重连' }))
  expect(open).toHaveBeenCalledExactlyOnceWith('tinkerfin://connect', '_self')
  expect(revoke).not.toHaveBeenCalled()
  expect(screen.getAllByRole('status')).toHaveLength(1)
  expect(screen.getByRole('status')).toHaveTextContent('未连接')
  fireEvent.click(screen.getByRole('button', { name: '刷新设备状态' }))
  await screen.findByText('已连接')
  expect(screen.queryByRole('button', { name: '重连' })).not.toBeInTheDocument()
  expect(screen.getAllByRole('status')).toHaveLength(1)
})
