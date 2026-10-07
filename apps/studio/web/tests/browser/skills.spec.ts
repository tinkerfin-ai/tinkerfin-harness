import { installProjectScope } from './fixtures/projects'
import type { ConversationHistoryDetail, TraceMessage } from '../../src/api/conversation/history'
import { emptyTraceGraph } from '../../src/test/traceFixtures'
import { expect, test, type Locator, type Page } from '@playwright/test'
import { strToU8, zipSync } from 'fflate'
import { installNotificationStream } from './fixtures/notifications'
import type { InstalledSkill } from '../../src/features/skills/model'
import type { ChatRequestPayload } from '../../src/api/conversation/types'

const user = { user_id: 1, username: 'skill-preview', display_name: '技能预览', avatar_url: null, roles: [], disabled: false }

async function prepare(page: Page, installed: InstalledSkill[] = [], language: 'zh-CN' | 'en' = 'zh-CN') {
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.clock.setFixedTime(new Date('2026-09-28T09:00:00Z'))
  await page.addInitScript(({ user, language }) => {
    localStorage.setItem('tinkerfin.auth.session', JSON.stringify({ token: 'browser-token', serverAddress: 'http://127.0.0.1:8090', tokenType: 'Bearer', expiresAt: '2099-01-01T00:00:00.000Z', user }))
    localStorage.setItem('tinkerfin:language', language)
  }, { user, language })
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url())
    const path = url.pathname
    let data: unknown
    if (path === '/api/auth/me') data = { expires_at: '2099-01-01T00:00:00.000Z', user }
    else if (path === '/api/models') data = { items: [{ modelId: 'main', displayName: '主模型', connectionId: 'provider', connectionDisplayName: '测试提供方', reasoningEnabled: true, isDefault: true }], defaultModelId: 'main' }
    else if (path === '/api/conversation/config') data = { dayRanges: [7, 30] }
    else if (path === '/api/conversation/history') data = { items: [], nextCursor: null }
    else if (path === '/api/conversation/skill-thread/title') data = { threadId: 'skill-thread', title: '整理本周资料', titleSource: 'user', titleGenerationStatus: 'skipped', titleSeq: 1 }
    else if (path === '/api/skills/sources') data = [{ id: 'clawhub', name: 'ClawHub', url: 'https://clawhub.ai' }]
    else if (path === '/api/skills/installations') data = installed.filter(skill => skill.project_id === null || skill.project_id === url.searchParams.get('project_id'))
    else if (path === '/api/skills/catalog') data = { items: [{ id: 'author/reports', source_id: 'clawhub', name: 'Reports', description: '整理资料并生成报告', revision: 'fixed', author: 'Author', topics: [], updated_at: null }], cursor: null }
    else if (path.startsWith('/api/automation/') && path.endsWith('/counts')) data = {}
    else if (path.startsWith('/api/automation/')) data = { items: [], nextCursor: null }
    else throw new Error(`Unexpected request: ${path}`)
    await route.fulfill({ json: { code: 0, message: 'success', data } })
  })
  await installNotificationStream(page)
}

const composerSkill: InstalledSkill = {project_id: 'project-1', overridden: false,
  id: 'reports-id', name: 'reports', description: '整理报告', enabled: true,
  source_id: 'clawhub', source_kind: 'catalog', source_name: 'ClawHub', external_id: 'author/reports',
  author: 'Author', topics: [], file_count: 1, byte_size: 64,
  created_at: '2026-09-28T00:00:00Z', updated_at: '2026-09-28T00:00:00Z',
}

for (const accepted of [false, true]) {
  test(`对话多选技能随请求发送并${accepted ? '在受理后清空' : '在失败后保留'}`, async ({ page }) => {
    await prepare(page, [composerSkill, { ...composerSkill, id: 'research-id', name: 'research' }, { ...composerSkill, id: 'disabled-id', name: 'disabled', enabled: false }])
    await page.addInitScript(accepted => {
      const originalFetch = window.fetch
      window.fetch = async (input, init) => {
        const request = new Request(input, init)
        if (new URL(request.url).pathname !== '/api/conversation/chat') return originalFetch(input, init)
        const payload = await request.json() as ChatRequestPayload
        document.body.dataset.skillRequest = JSON.stringify(payload)
        if (!accepted) return new Response(JSON.stringify({ code: 1_001_008_001, message: '技能不可用', data: null }), { status: 422, headers: { 'Content-Type': 'application/json' } })
        let cleanup: () => void
        return new Response(new ReadableStream<Uint8Array>({
          start(controller) {
            const abort = () => { cleanup(); controller.error(new DOMException('Aborted', 'AbortError')) }
            cleanup = () => request.signal.removeEventListener('abort', abort)
            request.signal.addEventListener('abort', abort, { once: true })
            controller.enqueue(new TextEncoder().encode(`id: 1\ndata: ${JSON.stringify({ type: 'RUN_STARTED', threadId: 'skill-thread', runId: payload.runId })}\n\n`))
          },
          cancel() { cleanup() },
        }), { headers: { 'Content-Type': 'text/event-stream' } })
      }
    }, accepted)
    await installProjectScope(page)
    await page.goto('/')
    const draft = page.getByRole('textbox', { name: '消息输入' })
    await draft.fill('整理本周资料')
    for (const name of ['reports', 'research']) {
      await page.getByRole('button', { name: '打开命令和技能', exact: true }).click()
      const menu = page.getByRole('listbox', { name: '命令和技能建议' })
      await expect(menu.getByRole('option', { name: /disabled/ })).toHaveCount(0)
      await menu.getByRole('option', { name: new RegExp(name) }).click()
      await expect(draft).toHaveValue(name === 'reports' ? '整理本周资料/reports' : '整理本周资料/reports/research')
    }
    await page.getByRole('button', { name: '发送消息', exact: true }).click()
    await expect(page.locator('body')).toHaveAttribute('data-skill-request', /reports-id/)
    const payload = JSON.parse((await page.locator('body').getAttribute('data-skill-request'))!) as ChatRequestPayload
    expect(payload.forwardedProps.skillIds).toEqual(['reports-id', 'research-id'])
    expect(payload.messages[0]).toMatchObject({ role: 'user', content: '整理本周资料/reports/research' })
    await expect(draft).toHaveValue(accepted ? '' : '整理本周资料/reports/research')
  })
}

async function tabGeometry(tab: Locator) {
  return tab.evaluate(element => {
    const rect = element.getBoundingClientRect()
    const underline = getComputedStyle(element, '::after')
    return { x: rect.x, y: rect.y, width: rect.width, height: rect.height,
      underlineY: rect.bottom - parseFloat(underline.bottom) - parseFloat(underline.height) }
  })
}

async function actionGeometry(button: Locator) {
  return button.evaluate(element => {
    const box = element.getBoundingClientRect()
    const style = getComputedStyle(element)
    return { x: box.x, y: box.y, width: box.width, height: box.height, background: style.backgroundColor, color: style.color, radius: style.borderRadius, padding: style.padding, font: style.font, gap: style.gap }
  })
}

for (const width of [320, 768, 1024, 1440]) {
  test(`技能页和自动化页的页签位置与下划线一致 ${width}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 })
    await prepare(page)
    await installProjectScope(page)
    await page.goto('/?page=skills')
    await expect(page.getByRole('article', { name: 'Reports', exact: true })).toBeVisible()
    await page.evaluate(() => document.fonts.ready)
    const skills = await tabGeometry(page.getByRole('tab', { name: '发现', exact: true }))
    const importButton = await actionGeometry(page.getByRole('button', { name: '导入技能', exact: true }))
    const card = await page.getByRole('article', { name: 'Reports', exact: true }).boundingBox()
    expect(card?.height).toBeGreaterThanOrEqual(width < 768 ? 232 : 222)
    await installProjectScope(page)
    await page.goto('/?page=automation')
    await expect(page.getByRole('tab', { name: '任务', exact: true })).toBeVisible()
    await page.getByRole('tab', { name: '任务', exact: true }).click()
    await expect(page.getByRole('tab', { name: '任务', exact: true })).toHaveAttribute('aria-selected', 'true')
    await page.evaluate(() => document.fonts.ready)
    const automation = await tabGeometry(page.getByRole('tab', { name: '任务', exact: true }))
    const createButton = await actionGeometry(page.getByRole('button', { name: '新建自动化', exact: true }))
    expect(skills).toEqual(automation)
    expect(importButton).toEqual(createButton)
    await testInfo.attach('header-geometry', { body: JSON.stringify({ width, skills, automation }), contentType: 'application/json' })
  })
}

const galleryNames = ['market-research', 'financial-report', 'data-analysis', 'document-review', 'web-research', 'spreadsheet', 'presentations', 'meeting-notes', 'risk-analysis', 'earnings-summary', 'portfolio-review', 'industry-outlook']
const gallery = galleryNames.map((name, index) => ({
  ...composerSkill, id: `skill-${index}`, name, enabled: index % 3 !== 0,
  description: ['检索公开资料，梳理市场背景与关键变化，保留可追溯的信息来源', '整理财务数据与业务指标，生成结构清晰、便于核对的分析报告', '处理表格和原始数据，检查异常并解释结果'][index % 3],
  author: ['TinkerFin', 'Research Lab', 'Community'][index % 3],
  topics: [index % 2 ? '数据分析' : '研究'],
  external_id: `author/${name}`,
}))

async function prepareGallery(page: Page, theme: string) {
  await prepare(page, gallery)
  await page.addInitScript(theme => localStorage.setItem('tinkerfin:theme', theme), theme)
  await page.route('**/api/skills/**', async route => {
    const url = new URL(route.request().url())
    let data: unknown
    if (url.pathname === '/api/skills/sources') data = [
      { id: 'clawhub', name: 'ClawHub', url: 'https://clawhub.ai' },
      { id: 'anthropic', name: 'Anthropic', url: 'https://github.com/anthropics/skills' },
      { id: 'vercel', name: 'Vercel', url: 'https://github.com/vercel-labs/agent-skills' },
    ]
    else if (url.pathname === '/api/skills/catalog') data = { items: gallery.map(item => ({ ...item, id: item.external_id, source_id: url.searchParams.get('source_id'), revision: 'fixed' })), cursor: null }
    else if (url.pathname === '/api/skills/catalog/detail' || /^\/api\/skills\/installations\//.test(url.pathname)) {
      const item = gallery[0]
      const detail = { ...item, source_url: 'https://clawhub.ai/author/market-research',
        markdown: '# Market research\n\n收集与问题相关的公开资料，记录来源与日期。\n\n## 工作步骤\n\n1. 确定研究范围\n2. 交叉核对关键数据\n3. 汇总结论与待确认事项\n\n```python\npython scripts/research.py\n```',
        files: ['SKILL.md', 'scripts/research.py', 'references/sources.md'],
      }
      data = url.pathname.endsWith('/detail') ? { skill: { ...item, id: item.external_id, revision: 'fixed' }, detail } : detail
    } else if (url.pathname === '/api/skills/imports/github') data = { id: 'preview', source: 'github', candidates: gallery.slice(0, 2).map(item => ({ ...item, digest: item.id })) }
    else { await route.fallback(); return }
    await route.fulfill({ json: { code: 0, message: 'success', data } })
  })
}

for (const theme of ['light', 'dark']) for (const width of [320, 768, 1024, 1265, 1440]) {
  test(`技能库的主题、固定布局与自定义选择器 ${theme} ${width}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 960 })
    await prepareGallery(page, theme)
    await installProjectScope(page)
    await page.goto('/?page=skills')
    await expect(page.getByRole('article')).toHaveCount(gallery.length)
    await page.evaluate(() => document.fonts.ready)
    await expect(page.getByRole('switch')).toHaveCount(0)
    await expect(page.getByRole('tablist', { name: '技能范围' })).toHaveCount(0)
    await expect(page.getByRole('button', { name: '技能状态', exact: true })).toHaveCount(0)
    if (width > 1024) {
      const sources = page.getByRole('navigation', { name: '技能来源' })
      await expect(sources.getByRole('button', { name: 'ClawHub', exact: true })).toHaveText('ClawHub')
      await expect(sources.getByRole('button', { name: 'Anthropic', exact: true })).toBeVisible()
      await expect(sources.getByRole('button', { name: 'Vercel', exact: true })).toBeVisible()
    }
    const header = await tabGeometry(page.getByRole('tab', { name: '发现', exact: true }))
    const search = await page.getByRole('searchbox').boundingBox()
    if (width > 1024) {
      const source = await page.getByRole('navigation', { name: '技能来源' }).getByRole('button', { name: 'ClawHub', exact: true }).boundingBox()
      const viewport = await page.locator('.skills-scroll').boundingBox()
      expect(viewport!.y).toBe(source!.y)
    }
    await page.locator('.skills-scroll').evaluate(element => { element.scrollTop = element.scrollHeight })
    expect(await tabGeometry(page.getByRole('tab', { name: '发现', exact: true }))).toEqual(header)
    expect(await page.getByRole('searchbox').boundingBox()).toEqual(search)
    expect(await page.getByRole('searchbox').evaluate(element => {
      const box = element.getBoundingClientRect()
      return Boolean(document.elementFromPoint(box.x + 8, box.bottom + 8)?.closest('article'))
    })).toBe(false)
    if (width === 320 || width === 1440) await page.screenshot({ path: testInfo.outputPath(`skills-scrolled-${theme}-${width}.png`), fullPage: true })
    await page.locator('.skills-scroll').evaluate(element => { element.scrollTop = 0 })
    if (width === 320 || width === 1440) await page.screenshot({ path: testInfo.outputPath(`skills-discover-${theme}-${width}.png`), fullPage: true })
    await page.emulateMedia({ reducedMotion: 'no-preference' })
    const firstCard = page.getByRole('article').first()
    await firstCard.hover()
    await firstCard.evaluate(element => element.getAnimations({ subtree: true }).forEach(animation => animation.finish()))
    const scrollBounds = (await page.locator('.skills-scroll').boundingBox())!
    expect((await firstCard.boundingBox())!.y - scrollBounds.y).toBeGreaterThanOrEqual(16)
    await page.screenshot({ path: testInfo.outputPath(`skills-hover-${theme}-${width}.png`), fullPage: true })
    await page.emulateMedia({ reducedMotion: 'reduce' })
    const install = firstCard.getByRole('button', { name: '安装技能：market-research', exact: true })
    await install.click()
    const personal = firstCard.getByRole('radio', { name: /个人共用/ })
    await expect(personal).toBeChecked()
    await expect(personal).toBeFocused()
    await expect(firstCard.getByRole('radio', { name: /当前项目/ })).toBeDisabled()
    await expect(firstCard.getByRole('button', { name: '安装为个人共用', exact: true })).toBeVisible()
    await page.screenshot({ path: testInfo.outputPath(`skills-install-destination-${theme}-${width}.png`), fullPage: true })
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    await personal.press('Escape')
    await expect(install).toBeFocused()
    await page.getByRole('button', { name: '导入技能', exact: true }).click()
    const importDialog = page.getByRole('dialog', { name: '导入技能', exact: true })
    await expect(importDialog.getByRole('radio', { name: /当前项目/ })).toBeChecked()
    for (const option of await importDialog.getByRole('radio').all()) {
      expect(await option.evaluate(element => element.closest('label')!.getBoundingClientRect().height)).toBeGreaterThanOrEqual(44)
    }
    await page.screenshot({ path: testInfo.outputPath(`skills-import-destination-${theme}-${width}.png`), fullPage: true })
    await importDialog.getByRole('button', { name: '取消', exact: true }).click()
    await page.getByRole('tab', { name: '我的', exact: true }).click()
    await expect(page.getByRole('tablist', { name: '技能范围' })).toBeVisible()
    await expect(page.getByRole('switch')).toHaveCount(gallery.length)
    await expect(page.locator('select')).toHaveCount(0)
    const picker = page.getByRole('button', { name: '技能状态', exact: true })
    await picker.click()
    const options = page.getByRole('listbox', { name: '技能状态' })
    await expect(options).toBeVisible()
    await expect(options).toHaveCSS('border-top-width', '0px')
    await expect(picker).toHaveCSS('border-top-width', '0px')
    await expect(picker).not.toHaveCSS('box-shadow', 'none')
    const bounds = (await options.boundingBox())!
    expect(bounds.x).toBeGreaterThanOrEqual(0)
    expect(bounds.x + bounds.width).toBeLessThanOrEqual(width)
    await page.screenshot({ path: testInfo.outputPath(`skills-mine-${theme}-${width}.png`), fullPage: true })
    await options.press('Escape')
    await expect(picker).toBeFocused()
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    if ((theme === 'light' && width === 1440) || (theme === 'dark' && width === 320)) {
      await page.getByRole('button', { name: '管理技能：market-research', exact: true }).click()
      const menu = page.getByRole('menu', { name: '技能操作' })
      await expect(menu.getByRole('menuitem')).toHaveText(['详情', '更新', '卸载'])
      await page.screenshot({ path: testInfo.outputPath(`skills-menu-${theme}-${width}.png`), fullPage: true })
      await menu.press('Escape')
      await page.getByRole('button', { name: '查看技能：market-research', exact: true }).click()
      const drawer = page.getByRole('dialog')
      await expect(drawer.getByText('技能说明', { exact: true })).toBeVisible()
      await expect(drawer.getByRole('switch')).toHaveCount(0)
      await expect(drawer.getByRole('button', { name: '卸载技能', exact: true })).toHaveCount(0)
      await page.screenshot({ path: testInfo.outputPath(`skills-detail-${theme}-${width}.png`), fullPage: true })
      await drawer.getByText('文件目录', { exact: true }).click()
      await expect(drawer.getByText('scripts/research.py', { exact: true })).toBeVisible()
      await page.screenshot({ path: testInfo.outputPath(`skills-detail-expanded-${theme}-${width}.png`), fullPage: true })
      await drawer.getByRole('button', { name: '关闭', exact: true }).click()
      await page.getByRole('button', { name: '导入技能', exact: true }).click()
      await page.getByRole('textbox', { name: 'GitHub 地址' }).fill('https://github.com/author/skills')
      await page.getByRole('button', { name: '预览技能', exact: true }).click()
      await expect(page.getByRole('checkbox')).toHaveCount(2)
      await page.screenshot({ path: testInfo.outputPath(`skills-import-${theme}-${width}.png`), fullPage: true })
    }
  })
}

test('卡片悬浮有层次阴影并遵守减少动态设置', async ({ page }, testInfo) => {
  await prepareGallery(page, 'light')
  await page.emulateMedia({ reducedMotion: 'no-preference' })
  await installProjectScope(page)
  await page.goto('/?page=skills')
  const card = page.getByRole('article', { name: 'market-research', exact: true })
  await expect(card).toHaveCSS('cursor', 'pointer')
  await expect(card.locator('.skills-icon')).toHaveCSS('font-weight', '600')
  await card.hover()
  const hover = await card.evaluate(element => {
    element.getAnimations({ subtree: true }).forEach(animation => animation.finish())
    return { transform: getComputedStyle(element).transform, opacity: getComputedStyle(element, '::after').opacity, shadow: getComputedStyle(element, '::after').boxShadow }
  })
  expect(hover.transform).not.toBe('none')
  expect(hover.opacity).toBe('1')
  expect(hover.shadow).not.toBe('none')
  const viewport = (await page.locator('.skills-scroll').boundingBox())!
  const raisedCard = (await card.boundingBox())!
  await page.screenshot({ path: testInfo.outputPath('skills-hover-clearance.png'), fullPage: true })
  expect(raisedCard.y - viewport.y).toBeGreaterThanOrEqual(16)
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await expect(card).toHaveCSS('transform', 'none')
  await page.mouse.move(0, 0)
  await page.emulateMedia({ reducedMotion: 'no-preference' })
  await page.getByRole('searchbox').focus()
  await page.keyboard.press('Tab')
  await expect(card.getByRole('button', { name: '查看技能：market-research', exact: true })).toBeFocused()
  await card.evaluate(element => element.getAnimations({ subtree: true }).forEach(animation => animation.finish()))
  expect((await card.boundingBox())!.y - viewport.y).toBeGreaterThanOrEqual(16)
})

test('滚动加载下一页，失败保留卡片并原位重试', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await prepare(page)
  const remote = (index: number) => ({ id: `author/skill-${index}`, source_id: 'clawhub', name: `Skill ${index}`, description: '测试目录分页', revision: 'fixed', author: 'Author', topics: [], updated_at: null })
  let nextRequests = 0
  await page.route('**/api/skills/catalog?**', async route => {
    const cursor = new URL(route.request().url()).searchParams.get('cursor')
    if (cursor) {
      expect(cursor).toBe('next-page')
      nextRequests += 1
      if (nextRequests === 1) {
        await route.fulfill({ status: 503, json: { code: 1_001_008_003, message: '加载更多失败', data: null } })
        return
      }
    }
    await route.fulfill({ json: { code: 0, message: 'success', data: {
      items: cursor ? [remote(23), remote(24)] : Array.from({ length: 24 }, (_, index) => remote(index)),
      cursor: cursor ? null : 'next-page',
    } } })
  })
  await installProjectScope(page)
  await page.goto('/?page=skills')
  await expect(page.getByRole('article')).toHaveCount(24)
  expect(nextRequests).toBe(0)
  await expect(page.getByRole('button', { name: '加载更多', exact: true })).toHaveCount(0)
  await page.locator('.skills-scroll').evaluate(element => { element.scrollTop = element.scrollHeight })
  const retry = page.getByRole('button', { name: '重试', exact: true })
  await expect(retry).toBeVisible()
  await expect(page.getByRole('article')).toHaveCount(24)
  expect(nextRequests).toBe(1)
  await retry.click()
  await expect(page.getByRole('article')).toHaveCount(25)
  expect(nextRequests).toBe(2)
  await expect(retry).toHaveCount(0)
})

for (const target of [{ projectId: 'project-1', label: '当前项目', action: '安装到当前项目', status: '当前项目已安装' }, { projectId: null, label: '个人共用', action: '安装为个人共用', status: '个人共用已安装' }]) test(`卡片独立打开详情并明确安装到${target.label}`, async ({ page }) => {
  const installed: InstalledSkill[] = []
  const requests: { project_id: string | null }[] = []
  await prepare(page, installed)
  await page.route('**/api/skills/catalog/detail?**', async route => {
    await route.fulfill({ json: { code: 0, message: 'success', data: {
      skill: { id: 'author/reports', source_id: 'clawhub', name: 'Reports', description: '报告', revision: 'fixed', author: 'Author', topics: [], updated_at: null },
      detail: { name: 'Reports', description: '报告', markdown: '# 使用说明', files: ['SKILL.md'], topics: [], source_url: null, author: 'Author' },
    } } })
  })
  await page.route('**/api/skills/installations', async route => {
    if (route.request().method() !== 'POST') { await route.fallback(); return }
    requests.push(route.request().postDataJSON())
    const skill = { ...composerSkill, project_id: target.projectId }
    installed.push(skill)
    await route.fulfill({ json: { code: 0, message: 'success', data: skill } })
  })
  await installProjectScope(page)
  await page.goto('/?page=skills')
  const card = page.getByRole('article', { name: 'Reports', exact: true })
  await card.click({ position: { x: 10, y: 10 } })
  await expect(page.getByRole('dialog', { name: 'Reports', exact: true })).toBeVisible()
  await page.getByRole('button', { name: '关闭', exact: true }).click()
  await card.getByRole('button', { name: '安装技能：Reports', exact: true }).click()
  await expect(card.getByRole('radio', { name: /当前项目/ })).toBeFocused()
  expect(requests).toHaveLength(0)
  if (target.projectId === null) await page.keyboard.press('ArrowDown')
  await expect(card.getByRole('radio', { name: new RegExp(target.label) })).toBeChecked()
  await card.getByRole('button', { name: target.action, exact: true }).click()
  await expect(card.getByText(target.status, { exact: true })).toBeVisible()
  expect(requests).toEqual([expect.objectContaining({ project_id: target.projectId })])
  await expect(page.getByRole('dialog')).toHaveCount(0)
})

test('个人管理与发现之间可连续使用方向键导航', async ({ page }) => {
  await prepare(page)
  await installProjectScope(page)
  await page.goto('/?page=skills&skillView=mine')
  await page.getByRole('tab', { name: '当前项目', exact: true }).focus()
  await page.keyboard.press('ArrowRight')
  await expect(page.getByRole('tab', { name: '个人共用', exact: true })).toBeFocused()
  await page.getByRole('tab', { name: '我的', exact: true }).focus()
  await page.keyboard.press('ArrowLeft')
  await expect(page.getByRole('tab', { name: '发现', exact: true })).toBeFocused()
  await expect(page.getByRole('tablist', { name: '技能范围' })).toHaveCount(0)
  await page.keyboard.press('ArrowRight')
  await expect(page.getByRole('tab', { name: '我的', exact: true })).toBeFocused()
  await expect(page.getByRole('tab', { name: '个人共用', exact: true })).toHaveAttribute('aria-selected', 'true')
  await page.keyboard.press('ArrowLeft')
  await page.getByRole('button', { name: '导入技能', exact: true }).click()
  await page.getByRole('textbox', { name: 'GitHub 地址' }).fill('https://github.com/author/repository')
  await expect(page.getByRole('radio', { name: /当前项目/ })).toBeChecked()
  await page.goBack()
  await expect(page.getByRole('dialog')).toHaveCount(0)
  await expect(page.getByRole('tab', { name: '个人共用', exact: true })).toHaveAttribute('aria-selected', 'true')
  await page.goForward()
  await expect(page.getByRole('tab', { name: '发现', exact: true })).toHaveAttribute('aria-selected', 'true')
  await expect(page.getByRole('dialog')).toHaveCount(0)
})

test('英文安装和导入位置在窄屏容纳长项目名', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 320, height: 960 })
  await prepare(page, [], 'en')
  await page.route('**/api/projects', route => route.fulfill({ json: { code: 0, message: 'success', data: [{ id: 'project-1', name: 'InvestmentResearchProjectWithAnUnbrokenLongName', createdAt: '2030-01-01', updatedAt: '2030-01-01' }] } }))
  await page.goto('/?page=skills')
  await page.getByRole('button', { name: 'Install skill: Reports', exact: true }).click()
  await expect(page.getByRole('radio', { name: /Current project/ })).toBeChecked()
  await expect(page.getByRole('button', { name: 'Install in current project', exact: true })).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({ path: testInfo.outputPath('skills-install-destination-en-320.png'), fullPage: true })
  await page.getByRole('button', { name: 'Import skills', exact: true }).click()
  await expect(page.getByRole('dialog').getByRole('group', { name: 'Install location' })).toBeVisible()
  expect(await page.getByRole('dialog').evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true)
  await page.screenshot({ path: testInfo.outputPath('skills-import-destination-en-320.png'), fullPage: true })
})

test('更新失败保留卡片，重试复用操作 ID 并区分最新内容', async ({ page }, testInfo) => {
  const installed = [{ ...composerSkill, enabled: false }]
  await prepare(page, installed)
  const requests: { request_id: string }[] = []
  await page.route('**/api/skills/installations/reports-id/update', async route => {
    requests.push(route.request().postDataJSON())
    if (requests.length === 1) {
      await route.fulfill({ status: 503, json: { code: 1_001_008_003, message: '技能来源暂不可用，请稍后重试', data: null } })
      return
    }
    const changed = requests.length === 2
    installed[0] = { ...installed[0], description: '更新后的报告技能' }
    await route.fulfill({ json: { code: 0, message: 'success', data: { installation: installed[0], changed } } })
  })
  await installProjectScope(page)
  await page.goto('/?page=skills')
  await page.getByRole('tab', { name: '我的', exact: true }).click()
  const card = page.getByRole('article', { name: 'reports', exact: true })
  for (const attempt of [1, 2, 3]) {
    await card.getByRole('button', { name: '管理技能：reports', exact: true }).click()
    await page.getByRole('menuitem', { name: '更新', exact: true }).click()
    if (attempt === 1) {
      await expect(card.getByRole('alert')).toHaveText('技能来源暂不可用，请稍后重试')
      await expect(card).toContainText('整理报告')
      await page.screenshot({ path: testInfo.outputPath('skills-update-error.png'), fullPage: true })
    } else {
      await expect(page.getByText(attempt === 2 ? '技能已更新，下次新运行生效' : '已是最新内容', { exact: true })).toBeVisible()
      await expect(card).toContainText('更新后的报告技能')
      await expect(card.getByRole('alert')).toHaveCount(0)
    }
    await expect(card.getByRole('switch')).toHaveAttribute('aria-checked', 'false')
    await expect(page.getByRole('dialog')).toHaveCount(0)
    await expect(card.getByRole('button', { name: '管理技能：reports', exact: true })).toBeFocused()
  }
  expect(requests[0].request_id).toBeTruthy()
  expect(requests[1].request_id).toBe(requests[0].request_id)
  expect(requests[2].request_id).not.toBe(requests[1].request_id)
})

for (const theme of ['light', 'dark']) for (const width of [320, 1440]) {
  test(`ZIP 更新校验同名内容并保留失败输入 ${theme} ${width}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 960 })
    const installed: InstalledSkill[] = [{ ...composerSkill, source_kind: 'zip', source_id: null, source_name: 'ZIP', external_id: null, enabled: false }]
    await prepare(page, installed)
    await page.addInitScript(theme => localStorage.setItem('tinkerfin:theme', theme), theme)
    let previews = 0
    const requests: { request_id: string; replacement: { draft_id: string; digest: string } }[] = []
    await page.route('**/api/skills/imports/zip', async route => {
      previews += 1
      expect(route.request().headers()['content-type']).toBe('application/zip')
      await route.fulfill({ json: { code: 0, message: 'success', data: {
        id: 'zip-preview', source: 'zip', candidates: [{ name: previews === 1 ? 'other-skill' : 'reports', description: '更新后的报告技能', digest: 'a'.repeat(64), file_count: 2, byte_size: 128, topics: [], author: null }],
      } } })
    })
    await page.route('**/api/skills/installations/reports-id/update', async route => {
      requests.push(route.request().postDataJSON())
      if (requests.length === 1) {
        await route.fulfill({ status: 503, json: { code: 1, message: '更新失败，请重试', data: null } })
        return
      }
      installed[0] = { ...installed[0], description: '更新后的报告技能' }
      await route.fulfill({ json: { code: 0, message: 'success', data: { installation: installed[0], changed: true } } })
    })
    await installProjectScope(page)
    await page.goto('/?page=skills')
    await page.getByRole('tab', { name: '我的', exact: true }).click()
    const trigger = page.getByRole('button', { name: '管理技能：reports', exact: true })
    await trigger.click()
    await page.getByRole('menuitem', { name: '更新', exact: true }).click()
    const dialog = page.getByRole('dialog', { name: '更新「reports」', exact: true })
    await expect(dialog.getByRole('tab')).toHaveCount(0)
    await dialog.getByLabel('ZIP 文件', { exact: true }).setInputFiles({ name: 'reports.zip', mimeType: 'application/zip', buffer: Buffer.from(zipSync({ 'SKILL.md': strToU8('---\nname: reports\ndescription: 整理报告\n---\n报告') })) })
    await dialog.getByRole('button', { name: '预览技能', exact: true }).click()
    await expect(dialog.getByRole('alert')).toHaveText('ZIP 中没有与「reports」同名的技能')
    await expect(dialog.getByText('reports.zip', { exact: true })).toBeVisible()
    await page.screenshot({ path: testInfo.outputPath(`skills-zip-mismatch-${theme}-${width}.png`), fullPage: true })
    await dialog.getByRole('button', { name: '预览技能', exact: true }).click()
    await expect(dialog.getByRole('group', { name: '可安装技能' })).toContainText('reports')
    await expect(dialog.getByRole('checkbox')).toHaveCount(0)
    await dialog.getByRole('button', { name: '更新', exact: true }).click()
    await expect(dialog.getByRole('alert')).toHaveText('更新失败，请重试')
    await expect(dialog.getByRole('group', { name: '可安装技能' })).toContainText('更新后的报告技能')
    await page.screenshot({ path: testInfo.outputPath(`skills-zip-preview-error-${theme}-${width}.png`), fullPage: true })
    await dialog.getByRole('button', { name: '更新', exact: true }).click()
    await expect(dialog).toHaveCount(0)
    await expect(trigger).toBeFocused()
    await expect(page.getByRole('article', { name: 'reports', exact: true })).toContainText('更新后的报告技能')
    await expect(page.getByRole('switch')).toHaveAttribute('aria-checked', 'false')
    expect(requests[1]).toEqual(requests[0])
    expect(requests[0].replacement).toEqual({ draft_id: 'zip-preview', digest: 'a'.repeat(64) })
  })
}

test('菜单不被卡片滚动区裁切，键盘关闭恢复焦点且不触发开关', async ({ page }) => {
  await page.setViewportSize({ width: 768, height: 700 })
  await prepareGallery(page, 'light')
  await installProjectScope(page)
  await page.goto('/?page=skills')
  await page.getByRole('tab', { name: '我的', exact: true }).click()
  const trigger = page.getByRole('button', { name: '管理技能：industry-outlook', exact: true })
  await trigger.scrollIntoViewIfNeeded()
  await trigger.click()
  const menu = page.getByRole('menu', { name: '技能操作' })
  await expect(menu.getByRole('menuitem', { name: '详情', exact: true })).toBeFocused()
  const bounds = (await menu.boundingBox())!
  expect(bounds.y).toBeGreaterThanOrEqual(0)
  expect(bounds.y + bounds.height).toBeLessThanOrEqual(700)
  expect(await menu.evaluate(element => {
    const rect = element.getBoundingClientRect()
    return element.contains(document.elementFromPoint(rect.x + 10, rect.bottom - 10))
  })).toBe(true)
  await page.keyboard.press('ArrowDown')
  await expect(menu.getByRole('menuitem', { name: '更新', exact: true })).toBeFocused()
  await page.keyboard.press('End')
  await expect(menu.getByRole('menuitem', { name: '卸载', exact: true })).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(trigger).toBeFocused()
  await expect(page.getByRole('dialog')).toHaveCount(0)
  await expect(page.getByRole('switch', { name: '启用技能：industry-outlook', exact: true })).toHaveAttribute('aria-checked', 'true')
  await trigger.click()
  await page.locator('.skills-scroll').evaluate(element => { element.scrollTop = 0 })
  await expect(menu).toHaveCount(0)
  await expect(trigger).toBeFocused()
})

test('通知同步技能页和对话选择，停用后的草稿保留并提示处理', async ({ page, browser, baseURL }, testInfo) => {
  const installed = [{ ...composerSkill }]
  const context = await browser.newContext({ baseURL })
  try {
    const chat = await context.newPage()
    await prepare(page, installed)
    await prepare(chat, installed)
    await chat.addInitScript(() => localStorage.setItem('tinkerfin:theme', 'system'))
    await installProjectScope(page)
    await page.goto('/?page=skills')
    await page.getByRole('tab', { name: '我的', exact: true }).click()
    await expect(page.getByRole('switch')).toHaveAttribute('aria-checked', 'true')
    await installProjectScope(chat)
    await chat.goto('/')
    const input = chat.getByRole('textbox', { name: '消息输入' })
    await input.fill('保留这段分析草稿')
    await chat.getByRole('button', { name: '打开命令和技能', exact: true }).click()
    await chat.getByRole('option', { name: /reports/ }).click()
    await expect(chat.getByRole('button', { name: '发送消息', exact: true })).toBeEnabled()
    installed[0] = { ...installed[0], enabled: false }
    for (const view of [page, chat]) expect(await view.evaluate(() => window.emitResourceChange('studio.skills.changed', 'reports-id'))).toBe(1)
    await expect(page.getByRole('switch')).toHaveAttribute('aria-checked', 'false')
    await expect(input).toHaveValue('保留这段分析草稿/reports')
    await expect(chat.getByRole('button', { name: '发送消息', exact: true })).toBeDisabled()
    await expect(chat.getByText('所选技能已停用或卸载，请移除后再发送', { exact: true })).toBeVisible()
    for (const theme of ['light', 'dark'] as const) {
      await chat.emulateMedia({ colorScheme: theme })
      await expect(chat.locator('html')).toHaveAttribute('data-theme', theme)
      for (const width of [320, 768, 1024, 1440]) {
        await chat.setViewportSize({ width, height: 960 })
        await expect(input).toHaveValue('保留这段分析草稿/reports')
        await expect(chat.getByText('所选技能已停用或卸载，请移除后再发送', { exact: true })).toBeInViewport()
        expect(await chat.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
        await chat.screenshot({ path: testInfo.outputPath(`composer-unavailable-skill-${theme}-${width}.png`), fullPage: true })
      }
    }
    await input.evaluate(element => { const field = element as HTMLTextAreaElement; field.setSelectionRange(field.value.length, field.value.length) })
    await input.press('Backspace')
    await expect(input).toHaveValue('保留这段分析草稿')
    await expect(chat.getByRole('button', { name: '发送消息', exact: true })).toBeEnabled()
  } finally {
    await context.close()
  }
})


for (const theme of ['light', 'dark']) for (const width of [320, 768, 1024, 1440]) {
  test(`行内技能引用支持中文编辑、删除和撤销 ${theme} ${width}`, async ({ page }, testInfo) => {
    const skill = { ...composerSkill, name: 'ai-report-interpreter', description: '解读报告' }
    await page.setViewportSize({ width, height: 900 })
    await prepare(page, [skill])
    await page.addInitScript(theme => localStorage.setItem('tinkerfin:theme', theme), theme)
    let submitted: ChatRequestPayload | undefined
    await page.route('**/api/conversation/chat', async route => {
      submitted = route.request().postDataJSON() as ChatRequestPayload
      await route.fulfill({ status: 422, json: { code: 1_001_008_014, message: '诊断信息不进入界面', data: null } })
    })
    await installProjectScope(page)
    await page.goto('/')
    const input = page.getByRole('textbox', { name: '消息输入' })
    await page.getByRole('button', { name: '打开命令和技能', exact: true }).click()
    await page.getByRole('option', { name: /ai-report-interpreter 解读报告/ }).click()
    await expect(input).toHaveValue('用 /ai-report-interpreter 技能帮我')
    await page.evaluate(() => document.fonts.ready)
    await testInfo.attach(`composer-${theme}-${width}`, { body: await page.locator('.composer-dock').screenshot(), contentType: 'image/png' })
    const colors = await page.locator('.composer-skill-reference').evaluate(element => {
      const probe = document.createElement('span')
      probe.style.color = 'var(--color-brand-text)'
      element.append(probe)
      const expected = getComputedStyle(probe).color
      probe.remove()
      return { actual: getComputedStyle(element).color, expected }
    })
    expect(colors.actual).toBe(colors.expected)
    await input.evaluate(element => { const field = element as HTMLTextAreaElement; field.setSelectionRange(24, field.value.length) })
    await input.press('Backspace')
    await expect(input).toHaveValue('用 /ai-report-interpreter')
    await input.evaluate(element => (element as HTMLTextAreaElement).setSelectionRange(1, 2))
    await input.press('Backspace')
    await expect(input).toHaveValue('用/ai-report-interpreter')
    await input.evaluate(element => (element as HTMLTextAreaElement).setSelectionRange(0, 1))
    await input.press('Backspace')
    await expect(input).toHaveValue('/ai-report-interpreter')
    await expect(page.getByRole('button', { name: '发送消息', exact: true })).toBeEnabled()
    await input.press('Delete')
    await expect(input).toHaveValue('')
    await input.press('ControlOrMeta+z')
    await expect(input).toHaveValue('/ai-report-interpreter')
    await page.getByRole('button', { name: '发送消息', exact: true }).click()
    await expect(page.getByRole('status')).toHaveText('所选技能正文合计超过 512 KiB，请减少所选技能后重试')
    expect(submitted?.messages).toMatchObject([{ content: '/ai-report-interpreter' }])
    expect(submitted?.forwardedProps.skillIds).toEqual(['reports-id'])
    await expect(input).toHaveValue('/ai-report-interpreter')
  })
}

test('中文组合输入不会拆散技能引用或触发发送', async ({ page }) => {
  await prepare(page, [composerSkill])
  await installProjectScope(page)
  await page.goto('/')
  const input = page.getByRole('textbox', { name: '消息输入' })
  await page.getByRole('button', { name: '打开命令和技能', exact: true }).click()
  await page.getByRole('option', { name: /reports 整理报告/ }).click()
  const session = await page.context().newCDPSession(page)
  await session.send('Input.imeSetComposition', { text: '分析', selectionStart: 2, selectionEnd: 2 })
  await session.send('Input.insertText', { text: '分析' })
  await expect(input).toHaveValue('用 /reports 技能帮我分析')
  await expect(input).toHaveAttribute('aria-describedby')
  await expect(page.getByRole('button', { name: '发送消息', exact: true })).toBeEnabled()
  await session.detach()
})


for (const theme of ['light', 'dark']) for (const width of [320, 768, 1024, 1440]) {
  test(`技能历史按来源折叠展示，刷新保留正文 ${theme} ${width}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 })
    await prepare(page)
    await page.addInitScript(theme => localStorage.setItem('tinkerfin:theme', theme), theme)
    const time = '2026-09-29T00:00:00Z'
    const request: TraceMessage = { id: 'question', agui: { kind: 'message', messageId: 'question' }, traceSeq: 1, graphNamespace: [], runId: 'run', role: 'user', content: '用 /ai-report-interpreter 技能帮我', contentOmitted: false, status: 'completed', createdAt: time }
    const history: ConversationHistoryDetail = {projectId: 'project-1', archived: false,
      id: 1, threadId: 'skill-history', title: '报告分析', accessMode: 'full', titleSource: 'user', titleGenerationStatus: 'idle', titleSeq: 1,
      lastModel: 'main', pinned: false, asOfSeq: 4, generation: 'skills', observedAt: time, headRunId: 'run', availableHeads: ['run'], historyCursor: null,
      messageCount: 3, toolCallCount: 0, messages: [request,
        { ...request, id: 'skill-context', agui: { kind: 'message', messageId: 'skill-context' }, traceSeq: 2, content: '## 固定技能正文\n\n先阅读报告，核对资料来源，保留可追溯的计算依据。\n\n- 区分已确认的信息和推断\n- 使用用户提供的数据\n- 明确列出需要核验的内容', source: { kind: 'context', name: 'skill-invocation', metadata: { skills: [{ id: 'removed-skill', name: 'ai-report-interpreter', digest: 'fixed' }] } } },
        { ...request, id: 'answer', agui: { kind: 'message', messageId: 'answer' }, traceSeq: 3, role: 'assistant', content: '请提供需要解读的报告' },
      ], reasoning: [], interactionAvailability: [], interactions: [], graph: emptyTraceGraph(4), state: { root: {}, subgraphs: {} }, status: { execution: 'succeeded', headRunId: 'run' },
      completeness: { missingPrefix: false, missingTail: false, payloadOmitted: false }, taskTrace: null, runFailures: [], createdAt: time, updatedAt: time,
    }
    await page.route('**/api/conversation/**', async route => {
      const url = new URL(route.request().url())
      let data: unknown
      if (url.pathname === '/api/conversation/history') data = { items: [{ ...history, status: 'idle', lastRunId: 'run', hasPendingInterrupt: false, pendingInteractionKind: null }], nextCursor: null }
      else if (url.pathname === '/api/conversation/skill-history/history') data = { ...history, taskTrace: url.searchParams.get('includeTaskTrace') === 'false' ? null : { status: 'ready', todoGroups: [] } }
      else { await route.fallback(); return }
      await route.fulfill({ json: { code: 0, message: 'success', data } })
    })
    await installProjectScope(page)
    await page.goto('/?project=project-1&thread=skill-history')
    await expect(page.getByText('技能指令', { exact: true })).toBeVisible()
    await expect(page.getByRole('heading', { name: '固定技能正文' })).toHaveCount(0)
    await expect(page.locator('.user-message')).toHaveCount(1)
    await page.getByText('技能指令', { exact: true }).click()
    await expect(page.getByRole('heading', { name: '固定技能正文' })).toBeVisible()
    await page.evaluate(() => document.fonts.ready)
    const spacing = await page.locator('.context-message').evaluate(element => {
      const answer = document.querySelector('.assistant-message')!
      return { gap: answer.getBoundingClientRect().top - element.getBoundingClientRect().bottom,
        expected: parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--space-4')) }
    })
    expect(spacing.gap).toBe(spacing.expected)
    await testInfo.attach(`skill-history-${theme}-${width}`, { body: await page.screenshot(), contentType: 'image/png' })
    await page.reload()
    await expect(page.getByText('技能指令', { exact: true })).toBeVisible()
    await expect(page.getByRole('heading', { name: '固定技能正文' })).toHaveCount(0)
    await page.getByText('技能指令', { exact: true }).click()
    await expect(page.getByText('使用用户提供的数据', { exact: true })).toBeVisible()
    await expect(page.locator('.user-message')).toHaveCount(1)
  })
}
