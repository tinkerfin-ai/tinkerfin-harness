import { expect, test } from '@playwright/test'

const user = { user_id: 1, username: 'composer-test', display_name: '输入区验收', avatar_url: null, roles: [], disabled: false }
const names = ['deepseek-v4-pro', 'DeepSeek-V4-Flash', 'DeepSeek-V4-Flash-Vision']

test('模型菜单紧凑布局与文件选择等待反馈', async ({ page }, testInfo) => {
  await page.addInitScript(user => localStorage.setItem('tinkerfin.auth.session', JSON.stringify({ token: 'browser-token', tokenType: 'Bearer', expiresAt: '2099-01-01T00:00:00.000Z', user })), user)
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    let data: unknown = {}
    if (path === '/api/auth/me') data = { expires_at: '2099-01-01T00:00:00.000Z', user }
    else if (path === '/api/models') data = { items: names.map((name, index) => ({ modelId: name, displayName: name, imageSupport: 'supported', connectionId: 'test-provider', connectionDisplayName: '测试提供方', reasoningEnabled: true, isDefault: index === 1 })), defaultModelId: names[1] }
    else if (path === '/api/conversation/config') data = { dayRanges: [7, 30] }
    else if (path === '/api/conversation/history') data = { items: [], nextCursor: null }
    await route.fulfill({ json: { code: 0, message: 'success', data } })
  })
  await page.goto('/')
  const model = page.getByRole('button', { name: '选择模型', exact: true })
  await expect(model).toBeEnabled()
  for (const theme of ['light', 'dark']) {
    await page.getByRole('button', { name: '打开用户菜单' }).click()
    await page.getByRole('menuitem', { name: '设置', exact: true }).click()
    await page.getByRole('button', { name: '通用', exact: true }).click()
    await page.getByText(theme === 'light' ? '浅色' : '深色', { exact: true }).click()
    await page.getByRole('button', { name: '关闭对话框', exact: true }).click()
    await page.emulateMedia({ reducedMotion: 'reduce' })
    for (const width of [320, 768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 1000 })
      await model.click()
      const list = page.getByRole('listbox', { name: '模型选项' })
      await expect(list).toBeVisible()
      await expect(list).toHaveCSS('outline-width', '0px')
      await expect(list).toHaveCSS('border-top-width', '0px')
      const box = (await list.boundingBox())!
      expect(box.x).toBeGreaterThanOrEqual(0)
      expect(box.x + box.width).toBeLessThanOrEqual(width)
      await page.screenshot({ path: testInfo.outputPath(`models-${theme}-${width}.png`) })
      await list.press('Escape')
      await expect(model).toBeFocused()
    }
  }
  await model.click()
  await page.getByRole('listbox', { name: '模型选项' }).press('Home')
  await page.getByRole('listbox', { name: '模型选项' }).press('Enter')
  await expect(model).toContainText(names[0])
  const add = page.getByRole('button', { name: '添加本地附件', exact: true })
  const chooserEvent = page.waitForEvent('filechooser')
  await add.click()
  const chooser = await chooserEvent
  await expect(add).toHaveAttribute('aria-busy', 'true')
  await expect(add).toBeDisabled()
  await page.screenshot({ path: testInfo.outputPath('file-picker-loading.png') })
  await chooser.setFiles([])
  await expect(add).toBeEnabled()
  await expect(add).not.toHaveAttribute('aria-busy')
  await expect(add).toBeFocused()
  const device = await page.context().newCDPSession(page)
  await device.send('Emulation.setTouchEmulationEnabled', { enabled: true })
  await page.setViewportSize({ width: 320, height: 1000 })
  await model.click()
  const touchList = page.getByRole('listbox', { name: '模型选项' })
  const touchBox = (await touchList.boundingBox())!
  expect(touchBox.x).toBeGreaterThanOrEqual(0)
  expect(touchBox.x + touchBox.width).toBeLessThanOrEqual(320)
  for (const option of await touchList.getByRole('option').all()) {
    expect((await option.boundingBox())!.height).toBeGreaterThanOrEqual(44)
  }
  await page.screenshot({ path: testInfo.outputPath('models-touch-320.png') })
  await device.detach()
})
