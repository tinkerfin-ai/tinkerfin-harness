import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { buildEmptyConversation } from '../../lib/workspace'
import { buildInitialPayload } from '../conversation/agui'
import { DeviceSettings } from './DeviceSettings'
import { DevicePicker } from './DevicePicker'
import * as api from './api'
import type { LocalDevice } from './api'

const device: LocalDevice = {
  device_id: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', name: '我的 Mac', online: false, revoked: false,
  paired_at: '2030-01-01T00:00:00Z', last_seen_at: null,
  capabilities: { browser: true, mcp: false, desktop: false, desktop_capture: false, open_url: true },
}
beforeEach(() => { vi.spyOn(api, 'fetchDevices').mockResolvedValue([device]) })
afterEach(() => { cleanup(); vi.restoreAllMocks() })

it('未配置设备不显示本地图标，已有会话不能切换位置且不提供配置入口', () => {
  const change = vi.fn()
  const base = { targetState: 'ready' as const, onRetry: vi.fn(), projectId: 'reports', value: undefined, onChange: change, disabled: false }
  const { rerender } = render(<DevicePicker {...base} locked={false} devices={{ devices: [], status: 'ready', refresh: vi.fn() }} />)
  expect(screen.queryByRole('button', { name: /本地/ })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: '配置' })).not.toBeInTheDocument()
  rerender(<DevicePicker {...base} locked devices={{ devices: [device], status: 'ready', refresh: vi.fn() }} />)
  expect(screen.getByRole('button', { name: '本地 · 未连接' })).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: '本地 · 未连接' }))
  expect(change).not.toHaveBeenCalled()
})

it('历史本地会话显示原设备状态，不借新设备显示已连接', () => {
  render(<DevicePicker targetState="ready" onRetry={vi.fn()} projectId="reports" locked disabled={false}
    devices={{ devices: [{ ...device, device_id: 'replacement', online: true }], status: 'ready', refresh: vi.fn() }}
    value={{ device_id: device.device_id, workspace_id: 'reports' }} onChange={vi.fn()} />)
  expect(screen.getByRole('button', { name: '本地 · 未连接' })).toHaveAttribute('aria-pressed', 'true')
  expect(screen.getByRole('button', { name: '云端' })).toBeDisabled()
})

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
  expect(revoke).not.toHaveBeenCalled()
  expect(screen.getByRole('group', { name: '撤销授权' })).toHaveTextContent('已执行的操作不会撤回')
  fireEvent.click(screen.getByRole('button', { name: '确认撤销设备' }))
  await waitFor(() => expect(revoke).toHaveBeenCalledWith(device.device_id, expect.any(AbortSignal)))
})

it('离线目录仍可排队，失效选择保持可见且不会隐式改选云端', async () => {
  const change = vi.fn()
  const state = { devices: [device], status: 'ready' as const, refresh: vi.fn() }
  const target = { device_id: device.device_id, workspace_id: 'reports' }
  const { rerender } = render(<DevicePicker targetState="ready" onRetry={vi.fn()} projectId="reports" devices={state} value={target} onChange={change} locked={false} disabled={false} />)
  expect(screen.getByRole('button', { name: '本地 · 未连接' })).toHaveAttribute('aria-pressed', 'true')
  rerender(<DevicePicker targetState="ready" onRetry={vi.fn()} projectId="reports" devices={{ ...state, devices: [] }} value={target} onChange={change} locked={false} disabled={false} />)
  expect(screen.getByRole('button', { name: '本地 · 未连接' })).toHaveAttribute('aria-pressed', 'true')
  expect(change).not.toHaveBeenCalled()
  const conversation = buildEmptyConversation({ projectId: 'project', now: '2030-01-01T00:00:00Z' })
  expect(buildInitialPayload(conversation, 'work', [], [], target).forwardedProps.localTarget).toEqual(target)
  expect(buildInitialPayload(conversation, 'work').forwardedProps).not.toHaveProperty('localTarget')
})
