import { expect, test, type Page } from '@playwright/test'
import { join } from 'node:path'
import { installNotificationStream } from './fixtures/notifications'

declare global { interface Window { deviceClientOpenAttempts: string[] } }

const id = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
const authorizationUrl = `/?device_authorization=${id}#token=${'a'.repeat(64)}`
const successMessage = '授权成功，你可以关闭此页面'
const user = { user_id: 1, username: 'design-test', avatar_url: null, roles: [], disabled: false }

async function prepare(page: Page, theme: string, signedIn = true) {
  await page.clock.setFixedTime(new Date('2030-01-01T00:00:00Z'))
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.addInitScript(({ theme, user, signedIn }) => {
    localStorage.setItem('tinkerfin:theme', theme)
    localStorage.setItem('tinkerfin:language', 'zh-CN')
    if (signedIn) localStorage.setItem('tinkerfin.auth.session', JSON.stringify({ token: 'test-only', serverAddress: 'http://127.0.0.1:8090', tokenType: 'Bearer', expiresAt: '2099-01-01T00:00:00Z', user }))
    const open = window.open.bind(window)
    window.deviceClientOpenAttempts = []
    window.open = (url, target, features) => {
      const value = String(url ?? '')
      if (value.startsWith('tinkerfin://')) { window.deviceClientOpenAttempts.push(value); return null }
      return open(url, target, features)
    }
  }, { theme, user, signedIn })
  await installNotificationStream(page, true, false)
  let outcome: 'temporary' | 'waiting' | 'authorized' | 'conflict' = 'temporary'
  let connected = false
  let approval: Promise<void> | null = null
  const pageErrors: string[] = []
  const requests: string[] = []
  page.on('pageerror', error => pageErrors.push(error.message))
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    requests.push(path)
    const response = (data: unknown) => route.fulfill({ json: { code: 0, message: 'success', data } })
    if (path === '/api/auth/me') return response({ user, expires_at: '2099-01-01T00:00:00Z' })
    if (path === '/api/auth/login') return response({ access_token: 'signed-in', token_type: 'Bearer', user, expires_at: '2099-01-01T00:00:00Z' })
    if (path === '/api/projects') return response([])
    if (path === '/api/models') return response({ items: [], defaultModelId: null })
    if (path === '/api/devices') return response(connected ? [{ device_id: id, name: '设计验收 MacBook Pro', online: true, revoked: false,
      paired_at: '2030-01-01T00:00:00Z', last_seen_at: null, capabilities: { browser: false, desktop: false, mcp: false } }] : [])
    if (!path.startsWith(`/api/devices/authorizations/${id}/`)) return route.abort()
    if (outcome === 'temporary' || outcome === 'conflict') return route.fulfill({
      status: outcome === 'conflict' ? 409 : 503,
      json: { code: outcome === 'conflict' ? 1_001_012_002 : 500, message: 'isolated API failure', data: null },
    })
    if (path.endsWith('/approve')) { await approval; outcome = 'authorized'; connected = true }
    return response({ authorization_id: id, name: '设计验收 MacBook Pro', state: outcome, expires_at: '2099-01-01T00:00:00Z' })
  })
  return {
    pageErrors,
    requests,
    setOutcome: (value: typeof outcome) => { outcome = value },
    holdApproval: () => {
      let release: () => void = () => undefined
      approval = new Promise<void>(resolve => { release = resolve })
      return release
    },
  }
}

for (const theme of ['light', 'dark']) for (const width of [320, 768, 1024, 1440]) {
  test(`设备授权与客户端交接 ${theme} ${width}`, async ({ page }, info) => {
    await page.setViewportSize({ width, height: 900 })
    if (width === 320) {
      const cdp = await page.context().newCDPSession(page)
      await cdp.send('Emulation.setTouchEmulationEnabled', { enabled: true, maxTouchPoints: 1 })
    }
    const { setOutcome, holdApproval, pageErrors, requests } = await prepare(page, theme)
    const screenshot = async (state: string) => {
      await page.evaluate(() => document.fonts.ready)
      const path = process.env.TINKERFIN_UI_EVIDENCE_DIR
        ? join(process.env.TINKERFIN_UI_EVIDENCE_DIR, `authorization-${state}-${theme}-${width}.png`)
        : info.outputPath(`authorization-${state}.png`)
      await page.screenshot({ path })
    }
    await page.goto(authorizationUrl)
    await expect(page.getByRole('alert')).toHaveText('授权暂不可用，请重试')
    await expect(page.getByRole('button')).toHaveCount(1)
    await expect(page.getByRole('button', { name: '重试', exact: true })).toBeEnabled()
    await screenshot('temporary')

    setOutcome('waiting')
    await page.getByRole('button', { name: '重试', exact: true }).click()
    await expect(page.getByText('设计验收 MacBook Pro', { exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: '授权', exact: true })).toBeEnabled()
    const panel = page.locator('.device-authorization__body')
    expect((await panel.boundingBox())!.width).toBeLessThanOrEqual(400)
    await expect(panel).toHaveCSS('text-align', 'center')
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    await screenshot('waiting')
    const logoBounds = await page.getByRole('img', { name: 'TinkerFin', exact: true }).boundingBox()

    const releaseApproval = holdApproval()
    const authorize = page.getByRole('button', { name: '授权', exact: true })
    await authorize.click()
    try {
      await expect(authorize).toHaveAttribute('aria-busy', 'true')
      await expect(authorize).toBeDisabled()
      await expect(page.getByRole('button', { name: '取消', exact: true })).toBeDisabled()
      await expect(page.getByText(successMessage, { exact: true })).toHaveCount(0)
      await screenshot('submitting')
    } finally {
      releaseApproval()
    }
    await expect(page.getByText(successMessage, { exact: true })).toBeVisible()
    await expect(page.getByText(successMessage, { exact: true })).toHaveAttribute('role', 'status')
    await expect(page.getByRole('dialog', { name: '设置', exact: true })).toHaveCount(0)
    await expect(page).toHaveURL(authorizationUrl)
    expect(await page.evaluate(() => window.deviceClientOpenAttempts)).toEqual([])
    await expect(page.getByText('设计验收 MacBook Pro', { exact: true })).toHaveCount(0)
    await expect(page.getByText(user.username, { exact: true })).toHaveCount(0)
    await expect(page.getByRole('button')).toHaveCount(0)
    await expect(panel).toHaveCount(0)
    expect(await page.getByRole('img', { name: 'TinkerFin', exact: true }).boundingBox()).toEqual(logoBounds)
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    await screenshot('success')
    await page.reload()
    await expect(page.getByText(successMessage, { exact: true })).toBeVisible()
    expect(await page.evaluate(() => window.deviceClientOpenAttempts)).toEqual([])
    expect(await page.evaluate(() => sessionStorage.getItem('tinkerfin:last-valid-location') ?? '')).not.toContain('device_authorization')

    setOutcome('conflict')
    await page.reload()
    await expect(page.getByRole('heading', { name: '本地设备', exact: true })).toBeVisible()
    await expect(page.locator('.device-authorization')).toHaveCount(0)
    await expect(page).toHaveURL(/\?settings=devices$/)
    expect(await page.evaluate(() => window.deviceClientOpenAttempts)).toEqual(['tinkerfin://connect'])
    await expect(page.getByRole('button', { name: '打开客户端', exact: true })).toBeVisible()
    const dialog = page.getByRole('dialog', { name: '设置', exact: true })
    const close = dialog.getByRole('button', { name: '关闭对话框' })
    await expect(dialog.getByRole('heading', { name: '设置', exact: true })).toBeFocused()
    await expect(close).toHaveCSS('outline-style', 'none')
    await screenshot('settings-url')
    await page.keyboard.press('Tab')
    await expect(close).toBeFocused()
    await expect(close).toHaveCSS('outline-style', 'solid')
    await expect(close).toHaveCSS('outline-width', '2px')
    await screenshot('settings-keyboard')
    await page.keyboard.press('Shift+Tab')
    await expect(dialog.getByRole('button', { name: '撤销授权', exact: true })).toBeFocused()
    await page.keyboard.press('Tab')
    await expect(close).toBeFocused()
    expect(requests).not.toContain('/api/devices/authorizations')
    expect(pageErrors).toEqual([])
  })
}

test('622×320滚动授权内容后成功，Logo保留屏幕位置', async ({ page }) => {
  await page.setViewportSize({ width: 622, height: 320 })
  const { setOutcome } = await prepare(page, 'light')
  setOutcome('waiting')
  await page.goto(authorizationUrl)
  const authorize = page.getByRole('button', { name: '授权', exact: true })
  await expect(authorize).toBeEnabled()
  const logo = page.getByRole('img', { name: 'TinkerFin', exact: true })
  const logoBounds = await logo.boundingBox()
  await authorize.scrollIntoViewIfNeeded()
  expect(await logo.boundingBox()).toEqual(logoBounds)
  await authorize.click()
  await expect(page.getByText(successMessage, { exact: true })).toBeVisible()
  expect(await logo.boundingBox()).toEqual(logoBounds)
})

test('622×320失效入口回到设备设置，不保留独立结果页', async ({ page }, info) => {
  await page.setViewportSize({ width: 622, height: 320 })
  const { setOutcome } = await prepare(page, 'light')
  setOutcome('conflict')
  await page.goto(authorizationUrl)
  await expect(page.getByRole('heading', { name: '本地设备' })).toBeVisible()
  await expect(page.locator('.device-authorization')).toHaveCount(0)
  await expect(page.getByRole('button', { name: '打开客户端' })).toBeVisible()
  await expect(page.getByText('尚未连接设备', { exact: true })).toBeInViewport({ ratio: 1 })
  await page.evaluate(() => document.fonts.ready)
  const bounds = (await page.getByRole('dialog', { name: '设置', exact: true }).boundingBox())!
  expect(bounds.y).toBeGreaterThanOrEqual(0)
  expect(bounds.y + bounds.height).toBeLessThanOrEqual(320)
  await page.screenshot({ path: info.outputPath('authorization-returned-to-settings.png') })
})

for (const signedIn of [true, false]) {
  test(`取消已被其他账号批准的入口，登录状态 ${signedIn} 不显示成功`, async ({ page }) => {
    const { setOutcome } = await prepare(page, 'light', signedIn)
    setOutcome('waiting')
    await page.goto(authorizationUrl)
    if (signedIn) await expect(page.getByRole('button', { name: '授权', exact: true })).toBeEnabled()
    setOutcome('authorized')
    await page.getByRole('button', { name: '取消', exact: true }).click()
    await expect(page.getByRole('heading', { name: signedIn ? '本地设备' : '欢迎回来', exact: true })).toBeVisible()
    await expect(page.getByText(successMessage, { exact: true })).toHaveCount(0)
    await expect(page).toHaveURL(/\?settings=devices$/)
    expect(await page.evaluate(() => window.deviceClientOpenAttempts)).toEqual(['tinkerfin://connect'])
  })
}

for (const invalidEntry of ['malformed', 'unavailable']) {
  test(`未登录 ${invalidEntry} 入口清理后登录可达设备设置`, async ({ page }) => {
    const { setOutcome, requests } = await prepare(page, 'light', false)
    setOutcome('conflict')
    await page.goto(invalidEntry === 'malformed' ? '/?device_authorization=invalid#token=invalid' : authorizationUrl)
    if (invalidEntry === 'unavailable') await page.getByRole('button', { name: '取消', exact: true }).click()
    await expect(page.getByRole('heading', { name: '欢迎回来', exact: true })).toBeVisible()
    await expect(page).toHaveURL(/\?settings=devices$/)
    expect(await page.evaluate(() => window.deviceClientOpenAttempts)).toEqual(['tinkerfin://connect'])
    await page.getByLabel('用户名', { exact: true }).fill('design-test')
    await page.getByLabel('密码', { exact: true }).fill('test-password')
    await page.getByRole('button', { name: '登录', exact: true }).click()
    await expect(page.getByRole('heading', { name: '本地设备', exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: '打开客户端', exact: true })).toBeEnabled()
    expect(requests).not.toContain('/api/devices/authorizations')
  })
}
