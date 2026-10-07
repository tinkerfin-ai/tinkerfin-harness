import { installProjectScope } from './fixtures/projects'
import { expect, test, type Page } from '@playwright/test'
import { installNotificationStream } from './fixtures/notifications'
import { emptyServices, type SavedServices, type ServiceCapability, type ServiceConfiguration } from '../../src/features/settings/serviceSettings'

const user = { user_id: 17, username: 'services-test', display_name: '服务验收', avatar_url: null, roles: [], disabled: false }

async function openServices(page: Page, theme = 'light', language = 'zh-CN') {
  const saved: SavedServices = emptyServices()
  const calls: string[] = []
  let failSave = false
  let finishTest: (() => void) | undefined
  let holdTest = false
  let holdClear = false
  let finishClear: (() => void) | undefined
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.addInitScript(({ user, theme, language }) => {
    localStorage.setItem('tinkerfin.auth.session', JSON.stringify({ token: 'isolated-test', serverAddress: 'http://127.0.0.1:8090', tokenType: 'Bearer', expiresAt: '2099-01-01T00:00:00Z', user }))
    localStorage.setItem('tinkerfin:language', language)
    localStorage.setItem('tinkerfin:theme', theme)
  }, { user, theme, language })
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    let data: unknown = {}
    if (path === '/api/auth/me') data = { expires_at: '2099-01-01T00:00:00Z', user }
    else if (path === '/api/skills/installations') data = []
    else if (path === '/api/models') data = { items: [], defaultModelId: null }
    else if (path === '/api/conversation/config') data = { dayRanges: [7, 30] }
    else if (path === '/api/conversation/history') data = { items: [], nextCursor: null }
    else if (path === '/api/services/settings') data = saved
    else if (path.startsWith('/api/services/')) {
      calls.push(`${route.request().method()} ${path}`)
      const capability = path.split('/')[3] as ServiceCapability
      if (route.request().method() === 'DELETE' && holdClear) await new Promise<void>(resolve => { finishClear = resolve })
      if (path.endsWith('/test')) {
        if (holdTest) await new Promise<void>(resolve => { finishTest = resolve })
        data = { outcome: 'success', code: 'success' }
      } else if (failSave) {
        await route.fulfill({ status: 503, json: { code: 1, message: '保存失败，请重试' } })
        return
      } else if (route.request().method() === 'DELETE') {
        saved[capability] = null
        data = null
      } else {
        const body = route.request().postDataJSON() as { configuration: ServiceConfiguration; enabled: boolean; api_key: string | null }
        saved[capability] = { id: capability, configuration: body.configuration, enabled: body.enabled, has_key: Boolean(body.api_key) || Boolean(saved[capability]?.has_key), test_status: null, test_code: null, tested_at: null }
        data = saved[capability]
      }
    }
    await route.fulfill({ json: { code: 0, message: 'success', data } })
  })
  await installNotificationStream(page)
  await installProjectScope(page)
  await page.goto('/')
  if ((page.viewportSize()?.width ?? 1440) < 768) await page.getByRole('button', { name: language === 'en' ? 'Open navigation' : '打开导航', exact: true }).click()
  await page.getByRole('button', { name: language === 'en' ? 'Open user menu' : '打开用户菜单' }).click()
  await page.getByRole('menuitem', { name: language === 'en' ? 'Settings' : '设置', exact: true }).click()
  await page.getByRole('button', { name: language === 'en' ? 'Services' : '服务连接', exact: true }).click()
  await expect(page.getByLabel('API Key', { exact: true })).toBeVisible()
  return { saved, calls, failSave: (value = true) => { failSave = value }, holdTest: () => { holdTest = true }, finishTest: () => finishTest?.(), holdClear: () => { holdClear = true }, finishClear: () => { holdClear = false; finishClear?.() } }
}

for (const theme of ['light', 'dark']) {
  test(`服务双页签、统一控件和多选菜单 ${theme}`, async ({ page }, info) => {
    const errors: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    const api = await openServices(page, theme)
    const dialog = page.getByRole('dialog', { name: '设置', exact: true })
    for (const width of [320, 768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 960 })
      await dialog.getByRole('tab', { name: '网页搜索' }).click()
      await expect(dialog.getByRole('button', { name: '测试搜索', exact: true })).toBeDisabled()
      await expect(dialog.getByRole('button', { name: '添加服务' })).toHaveCount(0)
      await page.screenshot({ path: info.outputPath(`search-${width}.png`), animations: 'disabled' })
      const preset = dialog.getByRole('button', { name: '接入方式', exact: true })
      const addressHeight = await dialog.getByLabel('服务地址', { exact: true }).evaluate(element => element.closest('.ui-text-field__control')!.getBoundingClientRect().height)
      expect(addressHeight).toBe(40)
      expect((await preset.boundingBox())!.height).toBe(addressHeight)
      await preset.click()
      const presets = dialog.getByRole('listbox', { name: '接入方式', exact: true })
      expect(await presets.evaluate(element => getComputedStyle(element).borderTopWidth)).toBe('0px')
      await presets.press('Escape')
      await dialog.getByRole('tab', { name: '图片生成' }).click()
      await expect(dialog.getByText('未配置专用服务，代码绘图仍可使用')).toBeVisible()
      await dialog.getByLabel('模型 ID', { exact: true }).fill('image-model')
      const formats = dialog.getByRole('button', { name: '输出格式', exact: true })
      expect((await formats.boundingBox())!.height).toBe(addressHeight)
      await formats.click()
      const list = dialog.getByRole('listbox', { name: '输出格式', exact: true })
      await expect(list).toHaveAttribute('aria-multiselectable', 'true')
      for (const name of ['PNG', 'JPEG']) {
        const option = list.getByRole('option', { name, exact: true })
        if (await option.getAttribute('aria-selected') !== 'true') await option.click()
      }
      await list.press('Home')
      const png = list.getByRole('option', { name: 'PNG', exact: true })
      await expect(png).toHaveAttribute('aria-selected', 'true')
      await expect(list).toHaveAttribute('aria-activedescendant', (await png.getAttribute('id'))!)
      await expect(png).toHaveCSS('outline-style', 'solid')
      await page.screenshot({ path: info.outputPath(`formats-${width}.png`), animations: 'disabled' })
      await list.press('Escape')
      await expect(formats).toBeFocused()
      await expect(formats).toContainText('PNG / JPEG')
      await page.screenshot({ path: info.outputPath(`image-${width}.png`), animations: 'disabled' })
      const size = await dialog.evaluate(element => ({ width: element.clientWidth, scroll: element.scrollWidth }))
      expect(size.scroll).toBeLessThanOrEqual(size.width + 1)
    }
    expect(api.calls).toEqual([])
    expect(errors).toEqual([])
  })
}

test('独立保存、单按钮停止、错误恢复和退出草稿确认', async ({ page }, info) => {
  const api = await openServices(page)
  const dialog = page.getByRole('dialog')
  await dialog.getByLabel('API Key', { exact: true }).fill('isolated-search-key')
  await dialog.getByRole('tab', { name: '图片生成' }).click()
  await dialog.getByLabel('模型 ID', { exact: true }).fill('unsaved-image-model')
  await dialog.getByRole('tab', { name: '网页搜索' }).click()
  await dialog.getByRole('button', { name: '保存', exact: true }).click()
  await expect(dialog.getByText('已保存', { exact: true })).toBeVisible()
  expect(api.calls).toEqual(['PUT /api/services/web_search'])
  expect(api.saved.image_generation).toBeNull()
  await expect(dialog.getByLabel('API Key', { exact: true })).toHaveValue('')
  await dialog.getByRole('button', { name: '测试搜索', exact: true }).click()
  await expect(dialog.getByText('测试通过', { exact: true })).toBeVisible()
  await page.screenshot({ path: info.outputPath('success-inline.png'), animations: 'disabled' })
  api.holdTest()
  await dialog.getByRole('button', { name: '测试搜索', exact: true }).click()
  const stop = dialog.getByRole('button', { name: '停止', exact: true })
  await expect(stop).toBeVisible()
  await expect(dialog.getByRole('button', { name: '测试搜索', exact: true })).toHaveCount(0)
  await dialog.getByText('搜索参数', { exact: true }).click()
  await dialog.getByText('附加参数（JSON）', { exact: true }).click()
  await expect(dialog.getByRole('textbox', { name: '高级参数 JSON' })).toHaveAttribute('contenteditable', 'false')
  await stop.click()
  api.finishTest()
  await expect(dialog.getByText('已停止等待，可能已消耗额度')).toBeVisible()
  api.failSave()
  await dialog.getByLabel('服务地址', { exact: true }).fill('https://different.example')
  await dialog.getByLabel('API Key', { exact: true }).fill('replacement-key')
  await dialog.getByRole('button', { name: '保存', exact: true }).click()
  await expect(dialog.getByText('服务暂不可用，请稍后重试', { exact: true })).toBeVisible()
  await expect(dialog.getByLabel('API Key', { exact: true })).toHaveValue('replacement-key')
  await dialog.getByRole('button', { name: '关闭对话框', exact: true }).click()
  await expect(dialog.getByText('有尚未保存的修改，关闭后将丢失')).toBeVisible()
  await expect(dialog.getByRole('button', { name: '继续编辑', exact: true })).toBeFocused()
  await page.screenshot({ path: info.outputPath('discard-draft.png'), animations: 'disabled' })
  await dialog.getByRole('button', { name: '放弃修改并关闭', exact: true }).click()
  await expect(dialog).toHaveCount(0)
})

test('图片试生成等待期间显示加载动效，停止后恢复操作', async ({ page }) => {
  const api = await openServices(page)
  const dialog = page.getByRole('dialog', { name: '设置', exact: true })
  await dialog.getByRole('tab', { name: '图片生成' }).click()
  await dialog.getByLabel('模型 ID', { exact: true }).fill('image-model')
  await dialog.getByLabel('API Key', { exact: true }).fill('image-key')
  await dialog.getByRole('button', { name: '保存', exact: true }).click()
  await expect(dialog.getByText('已保存', { exact: true })).toBeVisible()

  api.holdTest()
  await page.emulateMedia({ reducedMotion: 'no-preference' })
  await dialog.getByRole('button', { name: '试生成', exact: true }).click()
  const status = dialog.getByRole('status').filter({ hasText: '等待服务响应' })
  const spinner = status.locator('.settings-services__test-spinner')
  await expect(spinner).toBeVisible()
  await expect(spinner).toHaveCSS('animation-name', 'ui-spin')
  await expect(dialog.getByRole('button', { name: '停止', exact: true })).toBeEnabled()

  await page.emulateMedia({ reducedMotion: 'reduce' })
  await expect(spinner).toHaveCSS('animation-name', 'none')
  await dialog.getByRole('button', { name: '停止', exact: true }).click()
  api.finishTest()
  await expect(spinner).toHaveCount(0)
  await expect(dialog.getByRole('button', { name: '试生成', exact: true })).toBeEnabled()
})

test.describe('触控与放大重排', () => {
  test.use({ hasTouch: true, viewport: { width: 320, height: 960 } })
  test('英文窄屏控件、停止反馈和200%等效布局', async ({ page }, info) => {
    await openServices(page, 'dark', 'en')
    const dialog = page.getByRole('dialog')
    const preset = dialog.getByRole('button', { name: 'Connection type', exact: true })
    expect((await preset.boundingBox())!.height).toBe(48)
    await dialog.getByLabel('API Key', { exact: true }).fill('isolated-key')
    await dialog.getByRole('button', { name: 'Save', exact: true }).click()
    await expect(dialog.getByText('Saved', { exact: true })).toBeVisible()
    expect(await dialog.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true)
    await preset.click()
    const list = dialog.getByRole('listbox', { name: 'Connection type', exact: true })
    for (const item of await list.getByRole('option').all()) expect((await item.boundingBox())!.height).toBeGreaterThanOrEqual(44)
    await list.press('Escape')
    await dialog.getByRole('tab', { name: 'Image generation', exact: true }).click()
    await page.screenshot({ path: info.outputPath('touch-image-320.png'), animations: 'disabled' })
    await page.setViewportSize({ width: 720, height: 480 })
    await dialog.getByRole('button', { name: 'Output format', exact: true }).click()
    await expect(dialog.getByRole('option', { name: 'WEBP', exact: true })).toBeVisible()
    await page.screenshot({ path: info.outputPath('image-reflow-200.png'), animations: 'disabled' })
    expect(await dialog.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true)
  })
})

test('自定义 HTTP 参数校验、保存和清除失败恢复', async ({ page }, info) => {
  const api = await openServices(page, 'dark')
  const dialog = page.getByRole('dialog')
  await dialog.getByRole('button', { name: '接入方式', exact: true }).click()
  await dialog.getByRole('option', { name: '自定义 HTTP', exact: true }).click()
  await dialog.getByLabel('请求地址').fill('http://127.0.0.1:9001/search')
  await dialog.getByRole('button', { name: '认证方式', exact: true }).click()
  await dialog.getByRole('option', { name: '无需认证', exact: true }).click()
  await expect(dialog.getByLabel('API Key', { exact: true })).toHaveCount(0)
  await dialog.getByRole('button', { name: '请求方式', exact: true }).click()
  await dialog.getByRole('option', { name: 'GET', exact: true }).click()
  const editor = dialog.getByRole('textbox', { name: '高级参数 JSON', exact: true })
  await editor.fill('{"options":{"apiKey":"forbidden"}}')
  await dialog.getByRole('button', { name: '保存', exact: true }).click()
  await expect(editor).toHaveAttribute('aria-invalid', 'true')
  expect(api.calls).toEqual([])
  await editor.fill('{"q":"${query}","limit":"${max_results}"}')
  await page.screenshot({ path: info.outputPath('custom-http.png'), animations: 'disabled' })
  await dialog.getByRole('button', { name: '保存', exact: true }).click()
  await expect(dialog.getByText('已保存', { exact: true })).toBeVisible()
  expect(api.saved.web_search?.configuration.request?.parameters).toEqual({ q: '${query}', limit: '${max_results}' })
  api.holdClear()
  api.failSave()
  await dialog.getByRole('button', { name: '清除配置', exact: true }).click()
  await expect(dialog.getByRole('button', { name: '保留配置', exact: true })).toBeFocused()
  const deleting = page.waitForRequest(request => request.method() === 'DELETE')
  await dialog.getByRole('button', { name: '清除配置', exact: true }).click()
  await deleting
  await expect(dialog.getByRole('button', { name: '保留配置', exact: true })).toBeDisabled()
  await expect(dialog.getByRole('button', { name: '清除配置', exact: true })).toBeDisabled()
  await expect(dialog.getByRole('button', { name: '关闭对话框', exact: true })).toBeDisabled()
  api.finishClear()
  await expect(dialog.getByRole('alert')).toHaveText('服务暂不可用，请稍后重试')
  await page.screenshot({ path: info.outputPath('clear-failure.png'), animations: 'disabled' })
  await dialog.getByRole('button', { name: '保留配置', exact: true }).click()
  await expect(dialog.getByRole('button', { name: '清除配置', exact: true })).toBeFocused()
  api.failSave(false)
  await dialog.getByRole('button', { name: '清除配置', exact: true }).click()
  await dialog.getByRole('button', { name: '清除配置', exact: true }).click()
  await expect(dialog.getByText('已清除配置', { exact: true })).toBeVisible()
  expect(api.saved.web_search).toBeNull()
})
