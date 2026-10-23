import { expect, test, type Route } from '@playwright/test'
import type { LocalDevice } from '../../src/features/devices/api'
import { installNotificationStream } from './fixtures/notifications'
import { installProjectScope } from './fixtures/projects'

declare global {
  interface Window { clientLaunches: string[] }
}

const device: LocalDevice = {
  device_id: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', name: '我的 Mac', online: true, revoked: false,
  paired_at: '2030-01-01T00:00:00Z', last_seen_at: null,
  capabilities: { browser: true, mcp: false, desktop: false, desktop_capture: false, open_url: true },
}

for (const theme of ['light', 'dark'] as const) for (const width of [320, 768, 1024, 1440]) {
  test(`设备连接入口与刷新反馈 ${theme} ${width}`, async ({ page }, info) => {
    await page.setViewportSize({ width, height: 960 })
    await page.emulateMedia({ colorScheme: theme, reducedMotion: 'reduce' })
    if (width === 320) {
      const cdp = await page.context().newCDPSession(page)
      await cdp.send('Emulation.setTouchEmulationEnabled', { enabled: true, maxTouchPoints: 1 })
    }
    await page.clock.install({ time: new Date('2030-01-01T00:00:00Z') })
    await page.addInitScript(theme => {
      const user = { user_id: 1, username: 'device-connection-test', avatar_url: null, roles: [], disabled: false }
      localStorage.setItem('tinkerfin.auth.session', JSON.stringify({ token: 'isolated-device-test', serverAddress: 'http://127.0.0.1:8090', tokenType: 'Bearer', expiresAt: '2099-01-01T00:00:00Z', user }))
      localStorage.setItem('tinkerfin:theme', theme)
      localStorage.setItem('tinkerfin:language', 'zh-CN')
      window.clientLaunches = []
      window.open = url => { window.clientLaunches.push(String(url)); return null }
    }, theme)
    let devices: LocalDevice[] = []
    let hold = false
    let failure = false
    let requests = 0
    let received!: (route: Route) => void
    const pending = new Promise<Route>(resolve => { received = resolve })
    await page.route('**/api/**', async route => {
      const path = new URL(route.request().url()).pathname
      let data: unknown = {}
      if (path === '/api/devices') {
        requests++
        if (hold) { received(route); return }
        if (failure) {
          await route.fulfill({ status: 503, json: { code: 500, message: 'unavailable', data: null } })
          return
        }
        data = devices
      } else if (path === '/api/auth/me') data = { user: { user_id: 1, username: 'device-connection-test', avatar_url: null, roles: [], disabled: false }, expires_at: '2099-01-01T00:00:00Z' }
      else if (path === '/api/models') data = { items: [], defaultModelId: null }
      else if (path === '/api/skills/installations') data = []
      else if (path === '/api/conversation/config') data = { dayRanges: [7, 30] }
      else if (path === '/api/conversation/history') data = { items: [], nextCursor: null }
      await route.fulfill({ json: { code: 0, message: 'success', data } })
    })
    await installProjectScope(page)
    await installNotificationStream(page, true, false)
    await page.goto('/?settings=devices')
    const dialog = page.getByRole('dialog', { name: '设置', exact: true })
    const open = dialog.getByRole('button', { name: '打开客户端', exact: true })
    await expect(open).toBeVisible()
    await page.clock.pauseAt(new Date('2030-01-01T00:01:00Z'))
    const download = dialog.getByRole('link', { name: '下载客户端', exact: true })
    await expect(download).toHaveAttribute('href', 'https://github.com/tinkerfin-ai/tinkerfin-harness/releases/latest/download/TinkerFin.zip')
    await download.focus()
    await expect(download).toBeFocused()
    const resting = await open.boundingBox()
    await open.hover()
    await expect(open).toHaveCSS('transform', 'none')
    expect(await open.boundingBox()).toEqual(resting)
    await page.mouse.down()
    await expect(open).toHaveCSS('transform', 'none')
    expect(await open.boundingBox()).toEqual(resting)
    await page.mouse.move(0, 0)
    await page.mouse.up()
    await page.screenshot({ path: info.outputPath('device-setup.png'), animations: 'disabled' })
    await open.click()
    await expect(dialog.getByRole('heading', { name: '等待设备连接' })).toBeVisible()
    const refresh = dialog.getByRole('button', { name: '刷新连接状态', exact: true })
    const before = await refresh.boundingBox()
    hold = true
    const beforeRequests = requests
    await refresh.click()
    const route = await pending
    await expect(refresh).toBeDisabled()
    await expect(refresh).toHaveAttribute('aria-busy', 'true')
    await expect(open).toBeEnabled()
    await expect(download).toBeVisible()
    await expect(dialog.getByRole('status')).toHaveText('设备连接成功后会显示在这里')
    const during = await refresh.boundingBox()
    expect(during?.width).toBe(before?.width)
    expect(during?.height).toBe(before?.height)
    await refresh.evaluate(element => (element as HTMLButtonElement).click())
    expect(requests).toBe(beforeRequests + 1)
    if (width === 320) {
      for (const control of [refresh, open, download]) {
        const bounds = (await control.boundingBox())!
        expect(bounds.height).toBeGreaterThanOrEqual(44)
        expect(bounds.width).toBeGreaterThanOrEqual(44)
      }
    }
    await page.screenshot({ path: info.outputPath('device-refresh-pending.png'), animations: 'disabled' })
    hold = false
    await route.fulfill({ json: { code: 0, message: 'success', data: [] } })
    await expect(refresh).toBeEnabled()
    await expect(dialog.getByRole('status')).toHaveText('设备连接成功后会显示在这里')
    await open.click()
    expect(await page.evaluate(() => window.clientLaunches)).toEqual(['tinkerfin://connect', 'tinkerfin://connect'])
    failure = true
    await refresh.click()
    await expect(dialog.getByRole('alert')).toContainText('设备列表暂不可用')
    await expect(open).toBeEnabled()
    await expect(download).toBeVisible()
    await expect(refresh).toBeEnabled()
    await page.screenshot({ path: info.outputPath('device-refresh-error.png'), animations: 'disabled' })
    failure = false
    await dialog.getByRole('button', { name: '重试', exact: true }).click()
    await expect(dialog.getByRole('alert')).toHaveCount(0)
    await expect(dialog.getByRole('status')).toHaveText('设备连接成功后会显示在这里')
    devices = [{ ...device, online: false }]
    await page.evaluate(() => window.emitResourceChange('studio.devices.changed', 'devices'))
    await expect(dialog.getByRole('region', { name: '已授权设备' })).toContainText('我的 Mac')
    const reconnect = dialog.getByRole('button', { name: '重连', exact: true })
    await reconnect.click()
    expect(await page.evaluate(() => window.clientLaunches)).toEqual(['tinkerfin://connect', 'tinkerfin://connect', 'tinkerfin://connect'])
    await expect(dialog.getByRole('status')).toHaveText('未连接')
    await expect(dialog.getByText(/刷新完成/)).toHaveCount(0)
    await page.screenshot({ path: info.outputPath('device-reconnect.png'), animations: 'disabled' })
    devices = [device]
    await page.evaluate(() => window.emitResourceChange('studio.devices.changed', 'devices'))
    await expect(dialog.getByRole('status')).toHaveText('已连接')
    await expect(reconnect).toHaveCount(0)
    await expect(dialog.getByRole('heading', { name: '等待设备连接' })).toHaveCount(0)
    expect(await dialog.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true)
    devices = [{ ...device, name: 'Research Mac · Daily reports and planning', online: false }]
    await page.evaluate(() => {
      localStorage.setItem('tinkerfin:language', 'en')
      window.dispatchEvent(new StorageEvent('storage', { key: 'tinkerfin:language', newValue: 'en' }))
      window.emitResourceChange('studio.devices.changed', 'devices')
    })
    const english = page.getByRole('dialog', { name: 'Settings', exact: true })
    await expect(english.getByRole('button', { name: 'Reconnect', exact: true })).toBeVisible()
    await expect(english.getByRole('button', { name: 'Revoke access', exact: true })).toBeVisible()
    expect(await english.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true)
    await page.screenshot({ path: info.outputPath('device-reconnect-english.png'), animations: 'disabled' })
  })
}
