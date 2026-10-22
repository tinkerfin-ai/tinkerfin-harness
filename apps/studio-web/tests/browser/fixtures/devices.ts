import type { Page } from '@playwright/test'
import type { LocalDevice } from '../../../src/features/devices/api'

export async function installConnectedDevice(page: Page) {
  const device: LocalDevice = {
    device_id: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', name: 'Review Mac', online: true, revoked: false,
    paired_at: '2030-01-01T00:00:00Z', last_seen_at: '2030-01-01T00:00:00Z',
    capabilities: { browser: true, desktop: false, mcp: false, desktop_capture: false, open_url: true },
  }
  await page.route('**/api/devices', route => route.fulfill({ json: { code: 0, message: 'success', data: [device] } }))
  await page.evaluate(() => { if (typeof window.emitResourceChange === 'function') window.emitResourceChange('studio.devices.changed', 'devices') })
}
