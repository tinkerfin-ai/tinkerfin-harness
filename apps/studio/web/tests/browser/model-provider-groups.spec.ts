import { expect, test, type Page } from '@playwright/test'
import type { AgentModelCatalogItem } from '../../src/api/models/types'

const user = { user_id: 1, username: 'provider-test', display_name: '提供方分组验收', avatar_url: null, roles: [], disabled: false }
const models: AgentModelCatalogItem[] = Array.from({ length: 15 }, (_, index) => ({
  modelId: `model-${index}`,
  displayName: index < 3 ? ['DeepSeek-V4-Pro', 'DeepSeek-V4-Flash', 'DeepSeek-V4-Flash-Vision'][index] : `Qwen ${index} 长名称模型用于验证菜单中的文字省略`,
  connectionId: index < 3 ? 'deepseek' : 'custom',
  connectionDisplayName: index < 3 ? 'DeepSeek' : '自定义提供方连接名称较长时保留完整可访问名称',
reasoningEnabled: false, isDefault: index === 0,
}))

async function prepare(page: Page, theme: string) {
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.addInitScript(({ user, theme }) => {
    localStorage.setItem('tinkerfin.auth.session', JSON.stringify({ token: 'browser-token', serverAddress: 'http://127.0.0.1:8090', tokenType: 'Bearer', expiresAt: '2099-01-01T00:00:00Z', user }))
    localStorage.setItem('tinkerfin:language', 'zh-CN')
    localStorage.setItem('tinkerfin:theme', theme)
  }, { user, theme })
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    let data: unknown = {}
    if (path === '/api/auth/me') data = { expires_at: '2099-01-01T00:00:00Z', user }
    else if (path === '/api/models') data = { items: models, defaultModelId: 'model-0' }
    else if (path === '/api/conversation/config') data = { dayRanges: [7, 30] }
    else if (path === '/api/conversation/history' || path === '/api/automation/tasks' || path === '/api/automation/runs') data = { items: [], nextCursor: null }
    else if (path === '/api/automation/tasks/counts') data = { enabled: 0, paused: 0 }
    await route.fulfill({ json: { code: 0, message: 'success', data } })
  })
  await page.goto('/')
  await expect(page.getByRole('button', { name: '选择模型', exact: true })).toBeEnabled()
}

for (const theme of ['light', 'dark']) {
  for (const entry of ['conversation', 'automation']) {
    test(`提供方分组、滚动与选择 ${entry} ${theme}`, async ({ page }, testInfo) => {
      const errors: string[] = []
      page.on('pageerror', error => errors.push(error.message))
      await prepare(page, theme)
      if (entry === 'automation') {
        await page.getByRole('button', { name: '自动化', exact: true }).click()
        await page.getByRole('button', { name: '新建自动化', exact: true }).click()
      }
      const trigger = page.getByRole('button', { name: '选择模型', exact: true })
      for (const width of [320, 768, 1024, 1440]) {
        await page.setViewportSize({ width, height: 1000 })
        await trigger.click()
        const menu = page.getByRole('listbox', { name: '模型选项' })
        const groups = menu.getByRole('group')
        await expect(groups).toHaveCount(2)
        await expect(groups.nth(0)).toHaveAccessibleName('DeepSeek')
        await expect(groups.nth(1)).toHaveAccessibleName(models[3].connectionDisplayName)
        await expect(groups.nth(0).getByRole('option')).toHaveCount(3)
        const box = (await menu.boundingBox())!
        expect(box.x).toBeGreaterThanOrEqual(0)
        expect(box.x + box.width).toBeLessThanOrEqual(width)
        expect(await menu.evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true)
        const first = groups.nth(0).getByRole('option').first()
        await expect(first).toBeInViewport()
        expect(await first.evaluate(el => {
          const bounds = el.getBoundingClientRect()
          return el.contains(document.elementFromPoint(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2))
        })).toBe(true)
        expect(await groups.nth(0).evaluate(el => {
          const title = el.querySelector('[id]')!
          const bounds = title.getBoundingClientRect()
          return title.contains(document.elementFromPoint(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2))
        })).toBe(true)
        await page.screenshot({ path: testInfo.outputPath(`${entry}-${theme}-${width}.png`) })
        await menu.press('End')
        await menu.press('Enter')
        await expect(trigger).toContainText(models.at(-1)!.displayName)
        await expect(trigger).toBeFocused()
        await trigger.click()
        const selected = menu.getByRole('option', { selected: true })
        const selectedBox = (await selected.boundingBox())!
        const menuBox = (await menu.boundingBox())!
        expect(selectedBox.y).toBeGreaterThanOrEqual(menuBox.y)
        expect(selectedBox.y + selectedBox.height).toBeLessThanOrEqual(menuBox.y + menuBox.height + 1)
        await menu.press('Home')
        await menu.press('Enter')
        await expect(trigger).toContainText(models[0].displayName)
      }
      if (entry === 'conversation') {
        const access = page.getByRole('button', { name: '选择访问权限', exact: true })
        const closedIcon = (await access.locator('svg').first().boundingBox())!
        const gap = (button: typeof access) => button.evaluate(el => {
          const label = el.querySelector('span')!.getBoundingClientRect()
          const icons = el.querySelectorAll('svg')
          return icons[icons.length - 1].getBoundingClientRect().left - label.right
        })
        expect(await gap(access)).toBe(await gap(trigger))
        await access.click()
        for (const option of await page.getByRole('listbox', { name: '访问权限选项' }).getByRole('option').all()) {
          expect((await option.locator('svg').first().boundingBox())!.x).toBe(closedIcon.x)
        }
      }
      expect(errors).toEqual([])
    })
  }
}
