import { expect, test, type Page } from '@playwright/test'
import type { DesktopAction, DesktopState } from '../../src/state'
import { fixture } from '../fixture'

declare global {
  interface Window { desktopTestActions: DesktopAction[]; desktopTestState: DesktopState }
}

async function open(page: Page, patch: Partial<DesktopState> = {}) {
  await page.addInitScript(snapshot => {
    window.desktopTestActions = []
    window.desktopTestState = snapshot
    window.webkit = { messageHandlers: { desktop: { postMessage: async action => {
      window.desktopTestActions.push(action)
      if (action.type === 'ready') window.receiveDesktopState(window.desktopTestState)
      if (action.type === 'language') {
        window.desktopTestState = { ...window.desktopTestState, revision: window.desktopTestState.revision + 1, language: action.language, locale: action.language === 'en' ? 'en' : 'zh-Hans' }
        window.receiveDesktopState(window.desktopTestState)
      }
      return { ok: true }
    } } } }
  }, fixture(patch))
  await page.goto('/')
  await expect(page.getByRole('navigation')).toBeVisible()
  await page.evaluate(() => document.fonts.ready)
}

for (const theme of ['light', 'dark'] as const) {
  test(`${theme} 页面、边框与行末位置`, async ({ page }, info) => {
    const external: string[] = []
    page.on('request', request => { if (!request.url().startsWith('http://127.0.0.1:8770/')) external.push(request.url()) })
    await open(page, { theme })
    const refresh = page.getByRole('button', { name: '刷新', exact: true })
    const refreshBox = await refresh.boundingBox()
    expect(refreshBox?.x).toBe(594)
    expect(refreshBox?.width).toBe(32)
    await page.screenshot({ path: info.outputPath('workspace.png') })
    await page.getByRole('button', { name: '连接', exact: true }).click()
    const service = page.getByLabel('服务地址')
    const control = service.locator('..')
    const shadow = await control.evaluate(node => getComputedStyle(node).boxShadow)
    expect(shadow).not.toBe('none')
    await expect(control).toHaveCSS('border-top-width', '0px')
    await service.focus()
    await expect(control).toHaveCSS('outline-width', '0px')
    await expect(service).toHaveCSS('outline-width', '0px')
    await expect(control).toHaveCSS('box-shadow', shadow)
    await page.screenshot({ path: info.outputPath('connection.png') })
    await page.getByRole('button', { name: '权限', exact: true }).click()
    await page.screenshot({ path: info.outputPath('permissions.png') })
    await page.getByRole('button', { name: '我的', exact: true }).click()
    const update = page.getByRole('button', { name: '检查更新', exact: true })
    expect((await update.boundingBox())?.x).toBe(refreshBox?.x)
    await page.screenshot({ path: info.outputPath('account.png') })
    await page.getByRole('button', { name: '编辑设备名称', exact: true }).click()
    await page.getByLabel('设备名称', { exact: true }).fill('')
    await page.getByRole('button', { name: '保存设备名称', exact: true }).click()
    const name = page.getByLabel('设备名称', { exact: true })
    await expect(name).toHaveAttribute('aria-invalid', 'true')
    await expect(name.locator('..')).toHaveCSS('border-top-width', '0px')
    await page.screenshot({ path: info.outputPath('name-error.png') })
    expect(external).toEqual([])
  })
}

for (const width of [320, 768, 1024, 1440]) {
  test(`宽度 ${width} 的中英文与键盘布局`, async ({ page }, info) => {
    await page.setViewportSize({ width, height: width === 320 ? 600 : 360 })
    await open(page)
    await page.getByRole('button', { name: '我的', exact: true }).click()
    await page.getByRole('button', { name: '编辑设备名称', exact: true }).click()
    await page.getByLabel('设备名称', { exact: true }).fill('尚未提交的研究设备名称')
    await page.getByRole('button', { name: '语言：简体中文' }).click()
    await page.getByRole('listbox').press('End')
    await page.getByRole('listbox').press('Enter')
    await expect(page.getByRole('button', { name: 'Language：English' })).toBeFocused()
    await expect(page.getByLabel('Device name', { exact: true })).toHaveValue('尚未提交的研究设备名称')
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    await page.screenshot({ path: info.outputPath('account-english.png') })
  })
}

test('粗指针导航与动作均至少44px，强制颜色保留系统字段边界', async ({ browser }) => {
  const context = await browser.newContext({ viewport: { width: 320, height: 600 }, hasTouch: true, reducedMotion: 'reduce' })
  try {
    const page = await context.newPage()
    await open(page)
    for (const button of await page.getByRole('navigation').getByRole('button').all()) {
      const bounds = await button.boundingBox()
      expect(bounds!.height).toBeGreaterThanOrEqual(44)
      expect(bounds!.width).toBeGreaterThanOrEqual(44)
    }
    await page.getByRole('button', { name: '连接', exact: true }).click()
    await page.emulateMedia({ forcedColors: 'active' })
    const field = page.getByLabel('服务地址')
    await field.focus()
    expect(await field.locator('..').evaluate(node => getComputedStyle(node).borderTopWidth)).not.toBe('0px')
  } finally { await context.close() }
})

test('更新弹框阶段保留焦点，锁定阶段不接受Escape', async ({ page }, info) => {
  await open(page)
  await page.evaluate(() => {
    window.desktopTestState = { ...window.desktopTestState, revision: 2, update: { phase: 'available', percent: 0, currentVersion: '0.1.1', targetVersion: '0.1.2' } }
    window.receiveDesktopState(window.desktopTestState)
  })
  const dialog = page.getByRole('dialog')
  await expect(dialog).toBeVisible()
  await dialog.getByRole('button', { name: '取消' }).focus()
  await page.evaluate(() => {
    window.desktopTestState = { ...window.desktopTestState, revision: 3, update: { ...window.desktopTestState.update, phase: 'downloading', percent: 47 } }
    window.receiveDesktopState(window.desktopTestState)
  })
  await expect(dialog.getByRole('button', { name: '取消' })).toBeFocused()
  await expect(dialog.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '47')
  await page.screenshot({ path: info.outputPath('update-downloading.png') })
  await page.evaluate(() => {
    window.desktopTestState = { ...window.desktopTestState, revision: 4, update: { ...window.desktopTestState.update, phase: 'installing', percent: 24 } }
    window.receiveDesktopState(window.desktopTestState)
  })
  await dialog.press('Escape')
  expect(await page.evaluate(() => window.desktopTestActions.some(action => action.type === 'update-cancel'))).toBe(false)
  await expect(dialog.getByRole('button')).toHaveCount(0)
})
