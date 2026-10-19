import { DEFAULT_SERVER_ADDRESS, normalizeServerAddress, setServerAddress } from '../../api/shared/config'
import { writeDeviceSettingsToLocation } from '../../lib/threadRoute'

export interface DeviceAuthorizationIntent { id: string; browserToken: string }

export function readDeviceAuthorizationIntent(): DeviceAuthorizationIntent | null {
  const id = new URLSearchParams(window.location.search).get('device_authorization')
  const browserToken = new URLSearchParams(window.location.hash.slice(1)).get('token')
  if (!id || !browserToken || !/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(id)
    || !/^[0-9a-f]{64}$/.test(browserToken)) return null
  return { id, browserToken }
}

/** 授权目的地来自部署配置，URL 不得覆盖 API 地址或复用其他服务的登录凭证 */
export function deviceAuthorizationServer(): string {
  return normalizeServerAddress(import.meta.env.VITE_STUDIO_API_ADDRESS || DEFAULT_SERVER_ADDRESS)
}

export function prepareDeviceAuthorization(): void {
  if (!new URLSearchParams(window.location.search).has('device_authorization')) return
  setServerAddress(deviceAuthorizationServer())
  if (!readDeviceAuthorizationIntent()) {
    writeDeviceSettingsToLocation()
    openDeviceClient()
  }
}

/** 从设备设置或失效入口打开客户端，由客户端发起连接 */
export function openDeviceClient(): void {
  try {
    window.open('tinkerfin://connect', '_self')
  } catch { /* 浏览器限制唤起时保留当前页面和可重试入口 */ }
}
