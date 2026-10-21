import { requestJson } from '../../api/shared/http'
import { deviceAuthorizationServer } from './authorizationRoute'

export interface LocalDevice {
  device_id: string
  name: string
  online: boolean
  revoked: boolean
  paired_at: string
  last_seen_at: string | null
  capabilities: { browser: boolean; mcp: boolean; desktop: boolean; desktop_capture: boolean; open_url: boolean }
}
export type DeviceAuthorizationState = 'waiting' | 'authorized' | 'expired' | 'cancelled' | 'superseded'
export interface DeviceAuthorizationPreview {
  authorization_id: string
  name: string
  expires_at: string
  state: DeviceAuthorizationState
}
const root = '/api/devices'
const options = (signal?: AbortSignal) => ({ signal, suppressGlobalError: true })

export async function fetchDevices(signal?: AbortSignal): Promise<LocalDevice[]> {
  const deadline = new AbortController()
  const timer = setTimeout(() => deadline.abort(new DOMException('设备状态查询超时', 'TimeoutError')), 15_000)
  try {
    const devices = await requestJson<LocalDevice[]>(root, options(signal ? AbortSignal.any([signal, deadline.signal]) : deadline.signal))
    if (!Array.isArray(devices)) throw new Error('设备列表格式无效')
    return devices
  } finally { clearTimeout(timer) }
}
export function requestDeviceAuthorization(
  id: string, action: 'preview' | 'approve' | 'deny', browserToken: string,
  authorization: string, userId: number, signal: AbortSignal,
) {
  return requestJson<DeviceAuthorizationPreview>(`${deviceAuthorizationServer()}${root}/authorizations/${encodeURIComponent(id)}/${action}`, {
    ...options(signal), method: 'POST', headers: { Authorization: authorization },
    body: { browser_token: browserToken, ...(action === 'approve' ? { expected_user_id: userId } : {}) },
  })
}
export const denyDeviceAuthorization = (id: string, browserToken: string, signal: AbortSignal) =>
  requestJson<DeviceAuthorizationPreview>(`${deviceAuthorizationServer()}${root}/authorizations/${encodeURIComponent(id)}/deny`, {
    ...options(signal), method: 'POST', requiresAuth: false, body: { browser_token: browserToken },
  })
export const revokeDevice = (id: string, signal: AbortSignal) => requestJson<void>(`${root}/${encodeURIComponent(id)}`, { ...options(signal), method: 'DELETE' })
