import { expect, test, type Page } from '@playwright/test'
import { runCalendarFixture, runFixture, taskFixture } from '../../src/test/automationFixtures'
import type { AutomationTask } from '../../src/features/automation/model'
import type { ServiceConfiguration, ServiceSettings } from '../../src/features/settings/serviceSettings'
import { installProjectScope } from './fixtures/projects'
import { installNotificationStream } from './fixtures/notifications'

const widths = [320, 768, 1024, 1440]
const user = { user_id: 1, username: 'feature-check', avatar_url: null, roles: [], disabled: false }
const reportText = Array.from({ length: 35 }, (_, index) => `第${index + 1}段结果：自动化报告中的完整正文可以逐段阅读`).join('\n\n')

async function prepare(page: Page, theme: 'light' | 'dark', { locale = 'zh-CN', task = taskFixture() }: { locale?: 'zh-CN' | 'en'; task?: AutomationTask } = {}) {
  const run = runFixture({ id: 'report-run', name: '审计长结果' })
  let downloadAttempts = 0
  const writes: ServiceConfiguration[] = []
  const configuration: ServiceConfiguration = { capability: 'web_search', provider_id: 'tavily', endpoint: 'https://api.tavily.com', extra: { topic: 'finance' }, request: null, depth: 'basic', max_results: 5 }
  const service: ServiceSettings = { id: 'search', configuration, enabled: true, has_key: true, test_status: null, test_code: null, tested_at: null }
  await page.clock.setFixedTime(new Date('2026-09-10T07:00:00Z'))
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.addInitScript(({ user, theme, locale }) => {
    localStorage.setItem('tinkerfin.auth.session', JSON.stringify({ token: 'isolated-feature-check', serverAddress: 'http://127.0.0.1:8090', tokenType: 'Bearer', expiresAt: '2099-01-01T00:00:00Z', user }))
    localStorage.setItem('tinkerfin:theme', theme)
    localStorage.setItem('tinkerfin:language', locale)
  }, { user, theme, locale })
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url())
    const path = url.pathname
    let data: unknown
    if (path === '/api/auth/me') data = { expires_at: '2099-01-01T00:00:00Z', user }
    else if (path === '/api/models') data = { items: [{ modelId: 'main', displayName: '主模型', connectionId: 'provider', connectionDisplayName: '测试提供方', reasoningEnabled: true, isDefault: true }], defaultModelId: 'main' }
    else if (path === '/api/conversation/config') data = { dayRanges: [7, 30] }
    else if (path === '/api/conversation/history') data = { items: [], nextCursor: null }
    else if (path === '/api/automation/tasks') data = { items: [task], nextCursor: null }
    else if (path === '/api/automation/runs') data = { items: Date.parse(run.queuedAt) >= Date.parse(url.searchParams.get('from') ?? '2000-01-01') && Date.parse(run.queuedAt) < Date.parse(url.searchParams.get('until') ?? '2100-01-01') ? [run] : [], nextCursor: null }
    else if (path === '/api/automation/runs/calendar') data = runCalendarFixture([run], url.searchParams.get('weekStart')!)
    else if (path === '/api/automation/runs/report-run') data = { ...run, threadId: 'report-thread', runId: run.id, resultAvailable: true, messages: [{ id: 'result', role: 'assistant', content: reportText }], outputFiles: [{ id: 'report', name: 'report.md', mime_type: 'text/markdown', size_bytes: 6 }] }
    else if (path === '/api/attachments/report/download-url') {
      downloadAttempts += 1
      if (downloadAttempts === 1) { await route.fulfill({ status: 503, json: { code: 500, message: '下载失败', data: null } }); return }
      data = { url: 'https://storage.example/report.md' }
    } else if (path === '/api/skills/installations' || path === '/api/skills/sources') data = []
    else if (path === '/api/projects/project-1/memories') data = { items: [], nextOffset: null }
    else if (path === '/api/services/settings') data = { web_search: service, image_generation: null }
    else if (path === '/api/services/web_search') {
      const body = route.request().postDataJSON() as { configuration: ServiceConfiguration }
      writes.push(body.configuration)
      data = { ...service, configuration: body.configuration }
    } else throw new Error(`Unexpected request: ${path}`)
    await route.fulfill({ json: { code: 0, message: 'success', data } })
  })
  await page.route('https://storage.example/report.md', route => route.fulfill({ headers: { 'Access-Control-Allow-Origin': '*' }, contentType: 'text/markdown', body: 'report' }))
  await installNotificationStream(page)
  await installProjectScope(page)
  await page.goto('/')
  await expect(page.getByRole('textbox', { name: locale === 'en' ? 'Message input' : '消息输入', exact: true })).toBeVisible()
  return { writes, downloadAttempts: () => downloadAttempts }
}

async function navigate(page: Page, name: string, locale: 'zh-CN' | 'en' = 'zh-CN') {
  const open = page.getByRole('button', { name: locale === 'en' ? 'Open navigation' : '打开导航', exact: true })
  if (await open.isVisible()) await open.click()
  await page.getByRole('button', { name, exact: true }).click()
}

for (const hasTouch of [false, true]) {
  test.describe(`自动化时间输入 ${hasTouch ? '触控' : '鼠标'}`, () => {
    test.use({ hasTouch })
    for (const locale of ['zh-CN', 'en'] as const) for (const theme of ['light', 'dark'] as const) {
      test(`日期和时间完整显示并可打开 ${locale} ${theme}`, async ({ page }, info) => {
        const en = locale === 'en'
        const task = taskFixture({ schedule: { kind: 'once', date: '2026-09-14', time: '09:00' }, startsOn: '2026-09-10', endsOn: '2026-10-01' })
        await prepare(page, theme, { locale, task })
        expect(await page.evaluate(() => matchMedia('(any-pointer: coarse)').matches)).toBe(hasTouch)
        await navigate(page, en ? 'Automation' : '自动化', locale)
        await page.getByRole('tab', { name: en ? 'Tasks' : '任务', exact: true }).click()
        await page.getByRole('button', { name: task.name, exact: true }).click()
        const dialog = page.getByRole('dialog', { name: en ? 'Edit automation' : '编辑自动化' })
        await page.evaluate(() => document.fonts.ready)
        for (const section of [
          { name: en ? /Frequency/ : /执行频率/, labels: en ? ['Run date', 'Run time'] : ['执行日期', '执行时间'] },
          { name: en ? /Active dates/ : /有效期/, labels: en ? ['Start date', 'End date'] : ['开始日期', '结束日期'] },
        ]) {
          await dialog.getByRole('button', { name: section.name }).click()
          for (const width of widths) {
            await page.setViewportSize({ width, height: 900 })
            for (const label of section.labels) {
              const control = dialog.getByRole('button', { name: label, exact: true })
              await control.scrollIntoViewIfNeeded()
              const display = control.getByText(await control.innerText(), { exact: true })
              const textWidth = await display.evaluate(element => ({ scroll: element.scrollWidth, available: element.clientWidth }))
              expect(textWidth.scroll, `${label} ${width}px`).toBeLessThanOrEqual(textWidth.available)
              const bounds = await control.boundingBox()
              expect(bounds!.x).toBeGreaterThanOrEqual(0)
              expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(width)
              if (hasTouch) {
                expect(bounds!.width).toBeGreaterThanOrEqual(44)
                expect(bounds!.height).toBeGreaterThanOrEqual(44)
              }
              if (width === 320) {
                if (hasTouch) await control.tap()
                else await control.click()
                const picker = label === (en ? 'Run time' : '执行时间')
                  ? page.getByRole('dialog', { name: en ? 'Choose a time' : '选择时间', exact: true })
                  : page.getByRole('application', { name: en ? 'Choose a date' : '选择日期', exact: true })
                await expect(picker).toBeVisible()
                await page.keyboard.press('Escape')
                await expect(control).toBeFocused()
              }
            }
            expect(await dialog.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true)
            await page.screenshot({ path: info.outputPath(`automation-values-${section.labels[0]}-${width}.png`) })
          }
        }
      })
    }
  })
}

for (const theme of ['light', 'dark'] as const) {
  test(`长运行结果可完整阅读且附件下载可恢复 ${theme}`, async ({ page }, info) => {
    const api = await prepare(page, theme)
    await navigate(page, '自动化')
    await page.getByRole('button', { name: /查看运行：审计长结果/ }).click()
    const dialog = page.getByRole('dialog', { name: '运行结果' })
    const content = dialog.getByRole('region', { name: '运行结果' })
    await expect(content.getByText('第35段结果：自动化报告中的完整正文可以逐段阅读', { exact: true })).toBeAttached()
    for (const width of widths) {
      await page.setViewportSize({ width, height: 768 })
      await expect(dialog.getByRole('button', { name: '关闭对话框' })).toBeInViewport()
      await expect(dialog.getByRole('heading', { name: '运行结果', exact: true })).toBeInViewport()
      await content.evaluate(element => { element.scrollTop = element.scrollHeight })
      await expect(content.getByText('第35段结果：自动化报告中的完整正文可以逐段阅读', { exact: true })).toBeInViewport()
      await expect(dialog.getByRole('button', { name: '关闭对话框' })).toBeInViewport()
      await page.screenshot({ path: info.outputPath(`result-${theme}-${width}.png`) })
    }
    const file = content.getByRole('group', { name: 'report.md' })
    await file.getByRole('button', { name: 'report.md' }).click()
    await expect(file.getByRole('alert')).toContainText('下载失败，请重试')
    const received = page.waitForEvent('download')
    await file.getByRole('button', { name: '重试下载' }).click()
    expect((await received).suggestedFilename()).toBe('report.md')
    await expect(file.getByRole('alert')).toHaveCount(0)
    expect(api.downloadAttempts()).toBe(2)
  })

  test(`服务参数资源失败保留可编辑草稿 ${theme}`, async ({ page }, info) => {
    let chunkRequests = 0
    await page.route('**/assets/ModelOptionsEditor-*.js', async route => {
      chunkRequests += 1
      if (chunkRequests === 1) await route.abort('failed')
      else await route.continue()
    })
    const api = await prepare(page, theme)
    await page.getByRole('button', { name: '打开用户菜单' }).click()
    await page.getByRole('menuitem', { name: '设置', exact: true }).click()
    await page.getByRole('button', { name: '服务连接', exact: true }).click()
    await page.getByText('搜索参数', { exact: true }).click()
    await page.getByText('附加参数（JSON）', { exact: true }).click()
    await expect(page.getByText('高级编辑器不可用，可继续使用纯文本编辑')).toBeVisible()
    const input = page.getByRole('textbox', { name: '高级参数 JSON' })
    await expect(input).toHaveValue(JSON.stringify({ topic: 'finance' }, null, 2))
    for (const width of widths) {
      await page.setViewportSize({ width, height: 768 })
      await input.scrollIntoViewIfNeeded()
      await expect(input).toBeInViewport()
      await expect(page.getByRole('button', { name: '保存', exact: true })).toBeInViewport()
      await page.screenshot({ path: info.outputPath(`service-json-${theme}-${width}.png`) })
    }
    await input.fill('{invalid')
    await page.getByRole('button', { name: '保存', exact: true }).click()
    await expect(input).toHaveAttribute('aria-invalid', 'true')
    expect(api.writes).toHaveLength(0)
    await input.fill('{"topic":"economy"}')
    await page.getByRole('button', { name: '保存', exact: true }).click()
    await expect(page.getByText('已保存', { exact: true })).toBeVisible()
    expect(api.writes[0].extra).toEqual({ topic: 'economy' })
    expect(chunkRequests).toBe(1)
  })

  test(`登录字段错误持续可见且背景静止 ${theme}`, async ({ page }, info) => {
    await page.emulateMedia({ reducedMotion: 'reduce' })
    await page.addInitScript(theme => { localStorage.setItem('tinkerfin:theme', theme); localStorage.setItem('tinkerfin:language', 'zh-CN') }, theme)
    await page.goto('/')
    await page.getByRole('button', { name: '登录', exact: true }).click()
    for (const width of widths) {
      await page.setViewportSize({ width, height: 768 })
      for (const text of ['请输入用户名', '请输入密码']) {
        const error = page.getByText(text, { exact: true })
        await expect(error).toBeVisible()
        const bounds = await error.boundingBox()
        expect(bounds!.width).toBeGreaterThan(1)
        expect(bounds!.height).toBeGreaterThan(1)
      }
      await page.screenshot({ path: info.outputPath(`login-${theme}-${width}.png`) })
    }
    await page.emulateMedia({ reducedMotion: 'no-preference' })
    await page.getByRole('textbox', { name: '用户名', exact: true }).focus()
    for (const ribbon of await page.locator('img[src^="/auth/ribbon-"]').all()) await expect(ribbon).toHaveCSS('transform', 'none')
    expect(await page.getByRole('button', { name: '登录', exact: true }).evaluate(element => getComputedStyle(element, '::before').animationName)).toBe('none')
  })
}

test.describe('触控控件与搜索标签', () => {
  test.use({ hasTouch: true, isMobile: true })
  for (const theme of ['light', 'dark'] as const) {
    test(`日期网格、跨月日期与搜索目标 ${theme}`, async ({ page }, info) => {
      await prepare(page, theme)
      await navigate(page, '自动化')
      for (const width of widths) {
        await page.setViewportSize({ width, height: 768 })
        await page.getByRole('button', { name: '新建自动化', exact: true }).click()
        const dialog = page.getByRole('dialog', { name: '新建自动化' })
        await dialog.getByRole('button', { name: /执行频率/ }).click()
        await dialog.getByRole('button', { name: '重复', exact: true }).click()
        await dialog.getByRole('option', { name: '单次', exact: true }).click()
        await dialog.getByRole('button', { name: '执行日期', exact: true }).click()
        const calendar = page.getByRole('application', { name: '选择日期' })
        const grid = calendar.getByRole('grid')
        await expect(grid.getByRole('row')).toHaveCount(6)
        const outside = grid.getByRole('button', { name: /2026年8月31日/ })
        const contrast = await outside.evaluate(element => {
          const style = getComputedStyle(element), background = getComputedStyle(element.closest('[role="application"]')!).backgroundColor
          const numbers = (value: string) => value.match(/[\d.]+/g)!.slice(0, 3).map(Number)
          const fg = numbers(style.color), bg = numbers(background), opacity = Number(style.opacity)
          const luminance = (rgb: number[]) => rgb.map(value => value / 255).map(value => value <= .04045 ? value / 12.92 : ((value + .055) / 1.055) ** 2.4).reduce((sum, value, index) => sum + value * [.2126, .7152, .0722][index], 0)
          const a = luminance(fg.map((value, index) => value * opacity + bg[index] * (1 - opacity))), b = luminance(bg)
          return (Math.max(a, b) + .05) / (Math.min(a, b) + .05)
        })
        expect(contrast).toBeGreaterThanOrEqual(4.5)
        const bounds = await outside.boundingBox()
        expect(bounds!.width).toBeGreaterThanOrEqual(44)
        expect(bounds!.height).toBeGreaterThanOrEqual(44)
        await page.screenshot({ path: info.outputPath(`date-${theme}-${width}.png`) })
        await calendar.getByRole('button', { name: '选择月份和年份' }).click()
        await expect(grid).toHaveAttribute('aria-roledescription', '月份网格')
        expect(await grid.getByRole('row').count()).toBeGreaterThan(0)
        await calendar.getByRole('button', { name: '选择年份', exact: true }).click()
        await expect(grid).toHaveAttribute('aria-roledescription', '年份网格')
        expect(await grid.getByRole('row').count()).toBeGreaterThan(0)
        await page.keyboard.press('Escape')
        await dialog.getByRole('button', { name: '关闭对话框' }).click()
      }
      await navigate(page, '技能库')
      const search = page.getByRole('searchbox', { name: '搜索技能' })
      for (const width of widths) {
        await page.setViewportSize({ width, height: 768 })
        await search.fill('报告')
        const close = page.getByRole('button', { name: '清除搜索', exact: true })
        expect((await search.boundingBox())!.height).toBeGreaterThanOrEqual(44)
        expect((await close.boundingBox())!.height).toBeGreaterThanOrEqual(44)
        const association = await search.evaluate(element => {
          const input = element as HTMLInputElement
          const label = input.labels![0]
          return { onlyInput: label.querySelectorAll('input,button').length === 1, bounds: label.getBoundingClientRect().toJSON() }
        })
        expect(association.onlyInput).toBe(true)
        await page.touchscreen.tap(association.bounds.x + 2, association.bounds.y + association.bounds.height / 2)
        await expect(search).toBeFocused()
        await close.click()
        await expect(search).toHaveValue('')
        await expect(search).toBeFocused()
        await page.screenshot({ path: info.outputPath(`skills-${theme}-${width}.png`) })
      }
      for (const name of ['自动化', '记忆管理']) {
        await navigate(page, name)
        await expect(page).toHaveTitle(`TinkerFin - ${name}`)
        const label = name === '自动化' ? '搜索任务或运行历史' : '搜索记忆'
        await page.getByRole('button', { name: label, exact: true }).click()
        const input = page.getByRole('searchbox', { name: label, exact: true })
        await expect(input).toBeFocused()
        await input.fill('查询')
        await input.press('Escape')
        await expect(page.getByRole('button', { name: label, exact: true })).toBeFocused()
      }
      await page.getByRole('button', { name: '打开用户菜单' }).click()
      await page.getByRole('menuitem', { name: '设置', exact: true }).click()
      await page.getByRole('button', { name: '通用', exact: true }).click()
      await page.getByRole('button', { name: '界面语言', exact: true }).click()
      await page.getByRole('option', { name: 'English', exact: true }).click()
      await expect(page).toHaveTitle('TinkerFin - Memory management')
    })
  }
})
