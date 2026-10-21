import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
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
