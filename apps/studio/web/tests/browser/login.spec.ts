import { expect, test } from '@playwright/test'

test.beforeEach(async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' })
})

test('登录页保留服务器地址并将认证失败显示在公共页头下方', async ({ page }) => {
  await page.goto('/')
  const address = page.getByRole('textbox', { name: '服务器地址', exact: true })
  await expect(address).toBeVisible()
  await expect(address).toHaveAttribute('placeholder', 'http://127.0.0.1:8090')
  await address.fill('https://login-server.example/studio')
  await address.blur()
  await page.reload()
  await expect(address).toHaveValue('https://login-server.example/studio')
  const requested: string[] = []
  await page.route('https://login-server.example/studio/api/auth/login', async route => {
    requested.push(route.request().url())
    await route.fulfill({ status: 401, contentType: 'application/json', body: JSON.stringify({ code: 401, message: '用户名或密码错误', data: null }) })
  })
  const username = page.getByRole('textbox', { name: '用户名', exact: true })
  const password = page.getByLabel('密码', { exact: true })
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await expect(username).toHaveAttribute('aria-invalid', 'true')
  await expect(password).toHaveAttribute('aria-invalid', 'true')
  await expect(username).toBeFocused()
  expect(requested).toEqual([])
  await username.fill('test')
  await password.fill('invalid')
  await page.getByRole('button', { name: '登录', exact: true }).click()
  const message = page.getByRole('status').filter({ hasText: '用户名或密码错误' })
  await expect(message).toBeVisible()
  await expect(message).toHaveCount(1)
  const header = await page.locator('header').boundingBox()
  const notices = await page.getByRole('list', { name: '系统提示' }).boundingBox()
  expect(header!.height).toBe(64)
  expect(notices!.y).toBe(header!.y + header!.height + 12)
  expect(requested).toEqual(['https://login-server.example/studio/api/auth/login'])
})

for (const theme of ['light', 'dark']) {
  test(`登录表单居中、页头控件对齐且没有远程资源 ${theme}`, async ({ page }, testInfo) => {
    await page.addInitScript(theme => localStorage.setItem('tinkerfin:theme', theme), theme)
    const external: string[] = []
    page.on('request', request => {
      if (!request.isNavigationRequest() && new URL(request.url()).origin !== new URL(page.url()).origin) external.push(request.url())
    })
    await page.goto('/')
    const panel = page.getByRole('region', { name: '欢迎回来' })
    await expect(panel).toBeVisible()
    for (const width of [320, 768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 900 })
      const bounds = (await panel.boundingBox())!
      expect(bounds.y).toBe(900 - bounds.y - bounds.height)
      expect(bounds.width).toBe(Math.min(width - 48, 344))
      await expect(page.getByRole('textbox', { name: '服务器地址', exact: true })).toBeInViewport()
      for (const control of [page.getByRole('heading', { name: '欢迎回来' }), page.getByRole('textbox', { name: '用户名', exact: true }), page.getByRole('button', { name: '登录', exact: true })]) {
        await expect(control).toHaveCSS('font-weight', '400')
      }
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
      await page.screenshot({ path: testInfo.outputPath(`login-typography-${theme}-${width}.png`), animations: 'disabled' })
    }
    const github = page.getByRole('link', { name: '在 GitHub 查看 TinkerFin（新标签页）' })
    await expect(github).toHaveAttribute('href', 'https://github.com/tinkerfin-ai/tinkerfin-harness')
    const box = (await github.boundingBox())!
    expect(box.width).toBe(44)
    expect(box.height).toBe(44)
    expect(external.filter(url => !url.startsWith('data:'))).toEqual([])
  })
}


test('减少动态效果时主题折叠与展开均不覆盖品牌', async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 500 })
  await page.goto('/')
  const light = page.getByRole('radio', { name: '浅色', exact: true })
  const dark = page.getByRole('radio', { name: '深色', exact: true })
  await expect(light).toBeVisible()
  await expect(dark).toBeHidden()
  await page.getByRole('group', { name: '主题', exact: true }).hover()
  await expect(dark).toBeVisible()
  await dark.click()
  await expect(light).toBeHidden()
  await expect(dark).toBeVisible()
  await expect(dark).toBeChecked()
  await page.emulateMedia({ reducedMotion: 'no-preference' })
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await expect(light).toBeHidden()
  await expect(dark).toBeVisible()
})
