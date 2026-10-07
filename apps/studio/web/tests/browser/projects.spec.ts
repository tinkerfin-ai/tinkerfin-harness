import { emptyTraceGraph } from '../../src/test/traceFixtures'
import { expect, test, type Page } from '@playwright/test'
import { mkdir } from 'node:fs/promises'
import { resolve } from 'node:path'
import fixture from './fixtures/multimodal-history.json' with { type: 'json' }
import { installNotificationStream } from './fixtures/notifications'
import { fulfillExpectedHttpError } from './support/diagnostics'

const captured = resolve(process.cwd(), '../../../.agents/design/production-project-isolation/review')
const reviewCaptured = resolve(process.cwd(), '../../../.agents/review/project-isolation/frontend/screenshots')
const date = '2030-01-01T00:00:00Z'
const user = { user_id: 1, username: 'project-review', display_name: '个人账号', avatar_url: null, roles: [], disabled: false }
const record = { id: 1, projectId: 'research', archived: false, threadId: 'research-thread', title: '本周市场观察', titleSource: 'user', titleGenerationStatus: 'skipped', titleSeq: 1, status: 'idle', lastRunId: 'run', lastModel: 'main', accessMode: 'full', messageCount: 2, toolCallCount: 0, hasPendingInterrupt: false, pendingInteractionKind: null, pinned: false, createdAt: date, updatedAt: date }

async function prepare(page: Page, theme: 'light' | 'dark' = 'light') {
  const projects = [{ id: 'research', name: '市场研究', createdAt: date, updatedAt: date }, { id: 'product', name: '产品开发', createdAt: date, updatedAt: date }]
  const threads = [{ ...record }]
  const memories = new Map([['research', [{ path: '/preferences.md', content: '关注数据来源与统计口径，结论尽量简洁', preview: '关注数据来源与统计口径，结论尽量简洁', etag: 'a'.repeat(64), editable: true, sizeBytes: 60, updatedAt: date }]], ['product', []]])
  await page.clock.setFixedTime(new Date(date))
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.addInitScript(({ user, theme }) => {
    localStorage.setItem('tinkerfin.auth.session', JSON.stringify({ token: 'preview', tokenType: 'Bearer', serverAddress: 'http://127.0.0.1:8090', expiresAt: '2099-01-01T00:00:00Z', user }))
    localStorage.setItem('tinkerfin:theme', theme)
  }, { user, theme })
  await installNotificationStream(page)
  await page.route('**/api/**', async route => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname
    const body = request.postData() ? request.postDataJSON() : null
    const method = request.method()
    const response = (data: unknown, status = 200, message = 'success') => route.fulfill({ status, json: { code: status === 200 ? 0 : status, message, data } })
    if (path === '/api/auth/me') return response({ expires_at: '2099-01-01T00:00:00Z', user })
    if (path === '/api/projects') {
      if (method === 'POST') { const item = { id: `project-${projects.length}`, name: body.name, createdAt: date, updatedAt: date }; projects.push(item); memories.set(item.id, []); return response(item) }
      return response(projects)
    }
    if (path.startsWith('/api/projects/') && method === 'PATCH') {
      const project = projects.find(item => item.id === path.split('/').at(-1))
      if (!project) return response(null, 404)
      Object.assign(project, { name: body.name })
      return response(project)
    }
    if (path === '/api/models') return response({ items: [{ modelId: 'main', displayName: 'DeepSeek', connectionId: 'main', connectionDisplayName: '模型', reasoningEnabled: false, isDefault: true }], defaultModelId: 'main' })
    if (path === '/api/skills/installations' || path === '/api/skills/sources') return response([])
    if (path === '/api/conversation/config') return response({ dayRanges: [7, 30] })
    if (path === '/api/conversation/history') return response({ items: threads.filter(item => item.archived === (url.searchParams.get('archived') === 'true') && (url.searchParams.get('scope') === 'all' || item.projectId === url.searchParams.get('projectId')) && item.title.includes(url.searchParams.get('query') ?? '')), nextCursor: null })
    if (path === '/api/conversation/research-thread' && method === 'PATCH') { Object.assign(threads[0], body); return response(threads[0]) }
    const historyThread = threads.find(item => path === `/api/conversation/${item.threadId}/history`)
    if (historyThread) return response({
      ...fixture, ...historyThread, asOfSeq: 3, generation: 'project-preview', observedAt: date, headRunId: 'run', availableHeads: ['run'], historyCursor: null,
      messages: [
        { id: 'question', agui: { kind: 'message', messageId: 'question' }, sourceId: 'question', traceSeq: 1, graphNamespace: [], runId: 'run', role: 'user', content: '整理本周需要关注的市场变化', contentOmitted: false, status: 'completed', createdAt: date, completedAt: date },
        { id: 'answer', agui: { kind: 'message', messageId: 'answer' }, sourceId: 'answer', traceSeq: 2, graphNamespace: [], runId: 'run', role: 'assistant', content: '可以从宏观数据、行业动态和公司公告三个方面整理。先明确关注范围，再逐项核对来源。', contentOmitted: false, status: 'completed', createdAt: date, completedAt: date },
      ], reasoning: [], runFailures: [], interactions: [], interactionAvailability: [], status: { execution: 'succeeded', headRunId: 'run' }, state: { root: {}, subgraphs: {} },
      graph: emptyTraceGraph(3), taskTrace: { status: 'ready', todoGroups: [] },
    })
    const memory = /^\/api\/projects\/([^/]+)\/memories(\/file)?$/.exec(path)
    if (memory) {
      const items = memories.get(memory[1]) ?? []
      const name = body?.path ?? url.searchParams.get('path')
      const item = items.find(value => value.path === name)
      if (method === 'GET') return response(memory[2] ? item : { items: items.filter(value => `${value.path} ${value.content}`.includes(url.searchParams.get('query') ?? '')), nextOffset: null })
      if (method === 'POST') { const created = { ...items[0], path: body.path, content: body.content, preview: body.content, etag: 'd'.repeat(64), editable: true, sizeBytes: body.content.length, updatedAt: date }; items.push(created); memories.set(memory[1], items); return response(created) }
      if (!item || (body?.etag ?? url.searchParams.get('etag')) !== item.etag) return response(null, 409, '记忆已被修改，请重新读取后对照保存')
      if (method === 'PUT') { Object.assign(item, { content: body.content, preview: body.content, etag: 'c'.repeat(64) }); return response(item) }
      memories.set(memory[1], items.filter(value => value !== item)); return response(null)
    }
    return response(null, 404)
  })
  await page.goto('/?project=research')
  if (page.viewportSize()!.width >= 768 && page.viewportSize()!.width < 1024) await page.getByRole('button', { name: '打开侧边栏', exact: true }).click()
  await expect(page.getByRole('button', { name: '切换项目：市场研究' })).toBeVisible()
  await expect(page.getByRole('textbox', { name: '消息输入' })).toBeEnabled()
  return { projects, threads, memories }
}

test('创建、重命名与切换项目保留独立草稿，长名称不撑开侧栏', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await prepare(page)
  await page.getByRole('button', { name: '切换项目：市场研究' }).click()
  await page.getByRole('dialog', { name: '项目', exact: true }).getByRole('button', { name: '创建项目', exact: true }).click()
  await page.getByRole('textbox', { name: '项目名称' }).fill('新研究')
  await page.getByRole('dialog').getByRole('button', { name: '确认', exact: true }).click()
  await expect(page.getByRole('button', { name: '切换项目：新研究' })).toBeVisible()
  await page.getByRole('textbox', { name: '消息输入' }).fill('待研究的问题')
  await page.getByRole('button', { name: '切换项目：新研究' }).click()
  await page.getByRole('dialog', { name: '项目', exact: true }).getByRole('button', { name: '重命名项目', exact: true }).click()
  const name = '市场研究'.repeat(16)
  await page.getByRole('textbox', { name: '项目名称' }).fill(name)
  await page.getByRole('textbox', { name: '项目名称' }).press('Enter')
  const renamed = page.getByRole('button', { name: `切换项目：${name}` })
  await expect(renamed).toBeVisible()
  await expect(page.getByRole('textbox', { name: '消息输入' })).toHaveValue('待研究的问题')
  expect((await renamed.boundingBox())!.width).toBeLessThan(261)
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(1440)
  await page.getByRole('dialog', { name: '项目', exact: true }).getByRole('button', { name: '产品开发', exact: true }).click()
  await expect(page.getByRole('textbox', { name: '消息输入' })).toHaveValue('')
  await page.getByRole('button', { name: '切换项目：产品开发' }).click()
  await page.getByRole('dialog', { name: '项目', exact: true }).getByRole('button', { name, exact: true }).click()
  await expect(page.getByRole('textbox', { name: '消息输入' })).toHaveValue('待研究的问题')
})

test('会话移动、归档与恢复使用目标项目', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await prepare(page)
  await page.getByRole('button', { name: '管理会话：本周市场观察' }).click()
  await page.getByRole('button', { name: '移动到项目' }).click()
  await page.getByRole('dialog').getByRole('button', { name: '移动', exact: true }).click()
  await expect(page.getByRole('button', { name: '管理会话：本周市场观察' })).toHaveCount(0)
  await page.getByRole('button', { name: '切换项目：市场研究' }).click()
  await page.getByRole('dialog', { name: '项目', exact: true }).getByRole('button', { name: '产品开发', exact: true }).click()
  await page.getByRole('button', { name: '管理会话：本周市场观察' }).click()
  await page.getByRole('button', { name: '归档会话', exact: true }).click()
  await page.getByRole('button', { name: '已归档会话', exact: true }).click()
  await page.getByRole('button', { name: '管理会话：本周市场观察' }).click()
  await page.getByRole('button', { name: '恢复会话' }).click()
  await expect(page.getByRole('button', { name: '管理会话：本周市场观察' })).toHaveCount(0)
  await page.getByRole('button', { name: '返回会话记录', exact: true }).click()
  await expect(page.getByRole('button', { name: '管理会话：本周市场观察' })).toBeVisible()
  await page.setViewportSize({ width: 768, height: 900 })
  await page.getByRole('button', { name: '已归档会话', exact: true }).focus()
  await page.keyboard.press('Enter')
  await expect(page.getByRole('button', { name: '返回会话记录', exact: true })).toBeFocused()
  await expect(page.getByRole('tooltip')).toHaveCount(0)
  await page.getByRole('button', { name: '返回会话记录', exact: true }).click()
  await expect(page.getByRole('button', { name: '管理会话：本周市场观察' })).toBeVisible()
})

test('会话工具栏新会话从历史与技能页面进入空白会话，保留历史草稿', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await prepare(page)
  await page.getByRole('button', { name: '打开会话：本周市场观察', exact: true }).click()
  const composer = page.getByRole('textbox', { name: '消息输入' })
  await composer.fill('历史会话草稿')
  const nav = page.getByRole('navigation', { name: '工作区功能' })
  await page.locator('[aria-keyshortcuts~="Meta+K"]').click()
  await expect(composer).toHaveValue('')
  await expect(nav.getByRole('button', { name: '新会话', exact: true })).toHaveCount(0)
  await expect(nav.getByRole('button', { name: '更多', exact: true })).toBeDisabled()
  expect(new URL(page.url()).searchParams.get('project')).toBe('research')
  await nav.getByRole('button', { name: '技能库', exact: true }).click()
  await expect(nav.getByRole('button', { name: '技能库', exact: true })).toHaveAttribute('aria-current', 'page')
  await page.locator('[aria-keyshortcuts~="Meta+K"]').click()
  await expect(composer).toBeVisible()
  await expect(composer).toHaveValue('')
  await page.getByRole('button', { name: '打开会话：本周市场观察', exact: true }).click()
  await expect(composer).toHaveValue('历史会话草稿')
})

test('侧栏功能菜单的悬停和按下保持相同间距', async ({ page }) => {
  await prepare(page)
  const nav = page.getByRole('navigation', { name: '工作区功能' })
  const gaps = () => nav.getByRole('button').evaluateAll(buttons => buttons.slice(1).map((button, index) =>
    button.getBoundingClientRect().top - buttons[index].getBoundingClientRect().bottom))
  expect(await gaps()).toEqual([4, 4, 4])
  const memory = nav.getByRole('button', { name: '记忆管理' })
  await memory.hover()
  expect(await gaps()).toEqual([4, 4, 4])
  await page.mouse.down()
  expect(await gaps()).toEqual([4, 4, 4])
  await page.mouse.up()
})

test('后台悬停提示不阻止搜索弹框的Escape关闭', async ({ page }) => {
  await prepare(page)
  const archive = page.getByRole('button', { name: '已归档会话', exact: true })
  const search = page.getByRole('button', { name: '搜索会话', exact: true })
  await archive.hover()
  await archive.focus()
  await page.keyboard.press('Tab')
  await expect(search).toBeFocused()
  await page.keyboard.press('Enter')
  const dialog = page.getByRole('dialog', { name: '搜索会话', exact: true })
  await expect(dialog).toBeVisible()
  await expect(page.getByRole('tooltip')).toHaveCount(0)
  await page.keyboard.press('Escape')
  await expect(dialog).toHaveCount(0)
  await expect(search).toBeFocused()
  await expect(page.getByRole('tooltip')).toHaveCount(0)
})

test('搜索弹框跨响应式断点关闭后恢复可见入口', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await prepare(page)
  await page.getByRole('button', { name: '搜索会话', exact: true }).click()
  await expect(page.getByRole('combobox', { name: '搜索会话' })).toBeFocused()
  await page.setViewportSize({ width: 320, height: 900 })
  await expect(page.locator('.app-shell')).toHaveAttribute('data-sidebar-mode', 'overlay')
  await page.keyboard.press('Escape')
  await expect(page.getByRole('button', { name: '打开导航', exact: true })).toBeFocused()

  await page.setViewportSize({ width: 768, height: 900 })
  await page.getByRole('button', { name: '搜索会话', exact: true }).click()
  await page.setViewportSize({ width: 1440, height: 900 })
  await expect(page.locator('.app-shell')).toHaveAttribute('data-sidebar-mode', 'expanded')
  await page.keyboard.press('Escape')
  await expect(page.getByRole('button', { name: '搜索会话', exact: true })).toBeFocused()
  await expect(page.getByRole('tooltip')).toHaveCount(0)
})

test('会话搜索独立保留侧栏，支持范围、分页、重试、输入法和跨项目打开', async ({ page }) => {
  await page.clock.install({ time: new Date(date) })
  await page.setViewportSize({ width: 1440, height: 900 })
  const state = await prepare(page)
  const remote = { ...record, id: 50, projectId: 'product', threadId: 'product-search', title: '产品交互方案' }
  const next = { ...remote, id: 51, threadId: 'product-next', title: '接口交互记录' }
  state.threads.push(...Array.from({ length: 24 }, (_, index) => ({ ...record, id: index + 2, threadId: `research-${index}`, title: `研究记录 ${index + 1}` })), remote, next)
  await page.reload()
  const history = page.getByRole('region', { name: '最近对话', includeHidden: true })
  await expect(history.getByRole('button', { name: '打开会话：研究记录 24', exact: true })).toHaveCount(1)
  await page.clock.pauseAt(new Date('2030-01-01T00:01:00Z'))
  await history.evaluate(element => { element.scrollTop = 160 })
  const sidebarState = () => history.evaluate(element => ({ scroll: element.scrollTop, text: element.textContent }))
  const beforeSearch = await sidebarState()
  let failSearch = true
  const requests: string[] = []
  await page.route('**/api/conversation/history?**', async route => {
    const params = new URL(route.request().url()).searchParams
    if (!params.has('scope')) return route.fallback()
    requests.push(params.toString())
    if (failSearch) { failSearch = false; return fulfillExpectedHttpError(route, 503, '搜索读取失败') }
    const scope = params.get('scope')
    const items = scope === 'all' ? (params.has('cursor') ? [remote, next] : [record, remote]) : state.threads.slice(0, 2)
    const query = params.get('query') ?? ''
    await route.fulfill({ json: { code: 0, message: 'success', data: {
      items: items.filter(item => item.title.includes(query)), nextCursor: scope === 'all' && !params.has('cursor') && !query ? 'search-next' : null,
    } } })
  })
  await page.getByRole('button', { name: '搜索会话', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '搜索会话', exact: true })
  const input = dialog.getByRole('combobox', { name: '搜索会话' })
  const results = dialog.getByRole('listbox', { name: '会话搜索结果' })
  await expect(input).toBeFocused()
  await expect(dialog.getByText('正在搜索会话', { exact: true })).toBeVisible()
  await page.clock.runFor(300)
  await expect(dialog.getByText('搜索会话失败', { exact: true })).toBeVisible()
  await expect(page.getByRole('list', { name: '系统提示' }).getByRole('status')).toHaveCount(0)
  await dialog.getByRole('button', { name: '重试', exact: true }).click()
  await page.clock.runFor(300)
  await expect(results.getByRole('option')).toHaveCount(2)
  expect(await sidebarState()).toEqual(beforeSearch)
  await input.fill('不存在')
  await page.clock.runFor(300)
  await expect(dialog.getByText('没有匹配的对话', { exact: true })).toBeVisible()
  await input.fill('')
  await page.clock.runFor(300)
  await dialog.getByRole('button', { name: '选择搜索范围' }).click()
  const scope = dialog.getByRole('listbox', { name: '搜索范围', exact: true })
  await scope.press('ArrowDown')
  await scope.press('Enter')
  await page.clock.runFor(300)
  await expect(results.getByRole('option')).toHaveCount(2)
  await expect(results.getByRole('option', { name: /产品交互方案/ })).toContainText('产品开发')
  await dialog.getByRole('button', { name: '加载更多' }).click()
  await expect(results.getByRole('option')).toHaveCount(3)
  expect(requests.some(request => new URLSearchParams(request).get('cursor') === 'search-next')).toBe(true)
  expect(await sidebarState()).toEqual(beforeSearch)
  await input.focus()
  await input.dispatchEvent('compositionstart')
  for (const key of ['ArrowDown', 'Enter']) await input.dispatchEvent('keydown', { key, isComposing: true, bubbles: true })
  await input.dispatchEvent('compositionend')
  await expect(dialog).toBeVisible()
  await expect(results.getByRole('option', { selected: true })).toContainText('本周市场观察')
  await input.press('Control+k')
  await expect(dialog).toBeVisible()
  await input.press('ArrowDown')
  await input.press('Enter')
  await expect(dialog).toHaveCount(0)
  await expect(page.getByRole('button', { name: '切换项目：产品开发' })).toBeVisible()
  expect(new URL(page.url()).searchParams.get('project')).toBe('product')
  expect(new URL(page.url()).searchParams.get('thread')).toBe('product-search')
  await expect(page.getByRole('button', { name: '打开会话：产品交互方案', exact: true })).toBeVisible()
  await expect(page.getByRole('textbox', { name: '消息输入' })).toBeEnabled()
})

test('项目行内取消撤销输入并关闭鼠标提示，键盘恢复提示和焦点', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await prepare(page)
  await page.getByRole('button', { name: '切换项目：市场研究' }).click()
  const menu = page.getByRole('dialog', { name: '项目', exact: true })
  const rename = menu.getByRole('button', { name: '重命名项目', exact: true })
  await rename.hover()
  await expect(page.getByRole('tooltip', { name: '重命名项目', exact: true })).toBeVisible()
  await page.getByRole('tooltip', { name: '重命名项目', exact: true }).hover()
  await expect(page.getByRole('tooltip', { name: '重命名项目', exact: true })).toBeVisible()
  await page.mouse.move(500, 500)
  await expect(page.getByRole('tooltip', { name: '重命名项目', exact: true })).toHaveCount(0)
  await rename.click()
  const input = menu.getByRole('textbox', { name: '项目名称' })
  await expect(input).toBeFocused()
  await input.fill('未保存名称')
  await menu.getByRole('button', { name: '取消', exact: true }).click()
  await expect(input).toHaveCount(0)
  await expect(rename).toBeFocused()
  await expect(page.getByRole('button', { name: '切换项目：市场研究' })).toBeVisible()
  await expect(page.getByRole('tooltip', { name: '重命名项目', exact: true })).toHaveCount(0)
  await page.keyboard.press('Tab')
  await page.keyboard.press('Shift+Tab')
  await expect(rename).toBeFocused()
  await expect(page.getByRole('tooltip', { name: '重命名项目', exact: true })).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(page.getByRole('tooltip', { name: '重命名项目', exact: true })).toHaveCount(0)
  await page.keyboard.press('Enter')
  await input.fill('键盘撤销名称')
  await page.keyboard.press('Escape')
  await expect(input).toHaveCount(0)
  await expect(rename).toBeFocused()
  await expect(page.getByRole('button', { name: '切换项目：市场研究' })).toBeVisible()
  await expect(page.getByRole('tooltip', { name: '重命名项目', exact: true })).toHaveCount(0)
  await page.keyboard.press('Escape')
  await page.keyboard.press('Escape')
  await expect(menu).toHaveCount(0)
  await expect(page.getByRole('button', { name: '切换项目：市场研究' })).toBeFocused()
})

test('项目长列表打开后选中行可见，返回另一项目撤销原行内编辑', async ({ page }) => {
  const state = await prepare(page)
  state.projects.push(...Array.from({ length: 40 }, (_, index) => ({ id: `project-${index}`, name: `项目 ${index}`, createdAt: date, updatedAt: date })))
  await page.reload()
  await page.getByRole('button', { name: '切换项目：市场研究' }).click()
  await page.getByRole('dialog', { name: '项目', exact: true }).getByRole('button', { name: '项目 39', exact: true }).click()
  const trigger = page.getByRole('button', { name: '切换项目：项目 39' })
  await trigger.click()
  const menu = page.getByRole('dialog', { name: '项目', exact: true })
  await expect(menu.getByRole('button', { name: '项目 39', exact: true })).toBeFocused()
  await expect(menu.getByRole('button', { name: '项目 39', exact: true })).toBeInViewport()
  await menu.getByRole('button', { name: '重命名项目', exact: true }).click()
  await menu.getByRole('textbox', { name: '项目名称' }).fill('其他项目草稿')
  await page.goBack()
  const research = page.getByRole('button', { name: '切换项目：市场研究' })
  await research.click()
  await expect(menu.getByRole('textbox', { name: '项目名称' })).toHaveCount(0)
  await menu.getByRole('button', { name: '重命名项目', exact: true }).click()
  await expect(menu.getByRole('textbox', { name: '项目名称' })).toHaveValue('市场研究')
})

test.describe('行内项目操作与中性聚焦', () => {
  test.use({ hasTouch: true })
  for (const theme of ['light', 'dark'] as const) for (const width of [320, 768, 1024, 1440]) {
    test(`${theme} ${width}`, async ({ page }) => {
      await page.setViewportSize({ width: 1440, height: 900 })
      await prepare(page, theme)
      await page.setViewportSize({ width, height: 900 })
      if (width < 768) await page.getByRole('button', { name: '打开导航', exact: true }).click()
      else if (width < 1024) await page.getByRole('button', { name: '打开侧边栏', exact: true }).click()
      const trigger = page.getByRole('button', { name: '切换项目：市场研究' })
      await trigger.click()
      const menu = page.getByRole('dialog', { name: '项目', exact: true })
      const rename = menu.getByRole('button', { name: '重命名项目', exact: true })
      const renameBounds = (await rename.boundingBox())!
      await rename.click()
      const input = menu.getByRole('textbox', { name: '项目名称' })
      const save = menu.getByRole('button', { name: '保存', exact: true })
      const cancel = menu.getByRole('button', { name: '取消', exact: true })
      await page.evaluate(() => document.fonts.ready)
      const inputBounds = (await input.boundingBox())!
      for (const control of [save, cancel]) {
        const bounds = (await control.boundingBox())!
        expect(Math.abs(bounds.y + bounds.height / 2 - inputBounds.y - inputBounds.height / 2)).toBeLessThanOrEqual(0.5)
        expect(bounds.width).toBeGreaterThanOrEqual(44)
        expect(bounds.height).toBeGreaterThanOrEqual(44)
      }
      const cancelBounds = (await cancel.boundingBox())!
      expect(Math.abs(cancelBounds.x + cancelBounds.width - renameBounds.x - renameBounds.width)).toBeLessThanOrEqual(0.5)
      const bounds = (await menu.boundingBox())!
      expect(bounds.x).toBeGreaterThanOrEqual(0)
      expect(bounds.x + bounds.width).toBeLessThanOrEqual(width)
      const captured = resolve(process.cwd(), '../../../.agents/review/studio-tooltip/screenshots')
      await mkdir(captured, { recursive: true })
      await page.screenshot({ path: resolve(captured, `inline-${theme}-${width}.png`), fullPage: true })
      await cancel.click()
      await page.keyboard.press('Escape')
      await page.getByRole('button', { name: '记忆管理', exact: true }).click()
      await page.getByRole('button', { name: '编辑记忆：preferences.md' }).click()
      const content = page.getByRole('textbox', { name: '内容', exact: true })
      await expect(content).toBeFocused()
      const focused = await content.evaluate(element => {
        const probe = document.createElement('span')
        probe.style.color = 'var(--color-focus)'
        document.body.append(probe)
        const expected = getComputedStyle(probe).color
        probe.remove()
        return { expected, border: getComputedStyle(element).borderColor, outline: getComputedStyle(element).outlineWidth }
      })
      expect(focused.border).toBe(focused.expected)
      expect(focused.outline).toBe('0px')
      await page.screenshot({ path: resolve(captured, `memory-focus-${theme}-${width}.png`), fullPage: true })
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width)
    })
  }
})

test('记忆冲突显示最新内容并保留本地输入', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  const state = await prepare(page)
  await page.getByRole('button', { name: '记忆管理', exact: true }).click()
  await page.getByRole('button', { name: '编辑记忆：preferences.md' }).click()
  await page.getByRole('textbox', { name: '内容', exact: true }).fill('人工补充的偏好')
  Object.assign(state.memories.get('research')![0], { content: 'Agent 刚刚更新的偏好', etag: 'b'.repeat(64) })
  await page.getByRole('dialog').getByRole('button', { name: '保存', exact: true }).click()
  await expect(page.getByText('Agent 刚刚更新的偏好', { exact: true })).toBeVisible()
  await expect(page.getByRole('textbox', { name: '内容', exact: true })).toHaveValue('人工补充的偏好')
  await page.getByRole('button', { name: '载入最新内容' }).click()
  await expect(page.getByRole('textbox', { name: '内容', exact: true })).toHaveValue('Agent 刚刚更新的偏好')
})

for (const theme of ['light', 'dark'] as const) for (const width of [320, 768, 1024, 1440]) {
  test(`项目表单校验与确认按钮 ${theme} ${width}`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await prepare(page, theme)
    if (theme === 'light' && width === 1440) await page.emulateMedia({ reducedMotion: 'no-preference' })
    await page.getByRole('button', { name: '切换项目：市场研究' }).click()
    await page.getByRole('dialog', { name: '项目', exact: true }).getByRole('button', { name: '创建项目', exact: true }).click()
    await page.setViewportSize({ width, height: 900 })
    const dialog = page.getByRole('dialog', { name: '创建项目' })
    const name = dialog.getByRole('textbox', { name: '项目名称' })
    const confirm = dialog.getByRole('button', { name: '确认', exact: true })
    await name.fill('   ')
    await confirm.click()
    await expect(name).toBeFocused()
    await expect(name).toHaveAttribute('aria-invalid', 'true')
    await expect(dialog.getByRole('alert')).toHaveText('请输入项目名称')
    await name.fill('研究计划')
    await expect(name).toHaveAttribute('aria-invalid', 'false')
    await expect(dialog.getByRole('alert')).toHaveCount(0)
    const bounds = (await dialog.boundingBox())!
    expect(bounds.x).toBeGreaterThanOrEqual(0)
    expect(bounds.x + bounds.width).toBeLessThanOrEqual(width)
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width)
    await name.hover()
    await expect.poll(() => confirm.evaluate(element => {
      const actual = getComputedStyle(element).backgroundColor
      const probe = document.createElement('span')
      probe.style.backgroundColor = 'var(--color-brand)'
      element.append(probe)
      const expected = getComputedStyle(probe).backgroundColor
      probe.remove()
      return actual === expected
    })).toBe(true)
    await mkdir(reviewCaptured, { recursive: true })
    await page.screenshot({ path: resolve(reviewCaptured, `project-form-${theme}-${width}.png`), fullPage: true })
  })

  test(`项目导航与记忆页面 ${theme} ${width}`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 })
    if (width < 768) {
      await page.setViewportSize({ width: 1440, height: 900 })
      await prepare(page, theme)
      await page.getByRole('button', { name: '记忆管理', exact: true }).click()
      await page.setViewportSize({ width, height: 900 })
    } else {
      await prepare(page, theme)
      await page.getByRole('button', { name: '记忆管理', exact: true }).click()
    }
    await expect(page.getByRole('button', { name: '编辑记忆：preferences.md' })).toBeVisible()
    await page.evaluate(() => document.fonts.ready)
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width)
    await mkdir(captured, { recursive: true })
    await page.screenshot({ path: resolve(captured, `memories-${theme}-${width}.png`), fullPage: true })
    if (width === 320) {
      await page.getByRole('button', { name: '打开导航' }).click()
      await expect(page.getByRole('button', { name: '关闭导航', exact: true })).toBeFocused()
      await page.screenshot({ path: resolve(captured, `navigation-${theme}-${width}.png`), fullPage: true })
    }
  })
}

for (const theme of ['light', 'dark'] as const) for (const width of [320, 1440]) {
  test(`长会话列表独立滚动且账号保持在底部 ${theme} ${width}`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    const state = await prepare(page, theme)
    state.threads.push(...Array.from({ length: 25 }, (_, index) => ({ ...record, id: index + 2, threadId: `history-${index}`, title: `市场研究记录 ${index + 1}` })))
    await page.reload()
    await expect(page.getByRole('button', { name: '管理会话：市场研究记录 25' })).toBeAttached()
    await page.setViewportSize({ width, height: 900 })
    if (width === 320) await page.getByRole('button', { name: '打开导航', exact: true }).click()
    const account = page.getByRole('button', { name: '打开用户菜单', exact: true })
    const history = page.getByRole('region', { name: '最近对话', exact: true })
    await expect(account).toBeInViewport()
    const before = (await account.boundingBox())!
    expect(before.y).toBeGreaterThan(800)
    expect(await history.evaluate(element => element.scrollHeight > element.clientHeight)).toBe(true)
    await history.evaluate(element => { element.scrollTop = element.scrollHeight })
    await expect(page.getByRole('button', { name: '已归档会话', exact: true })).toBeInViewport()
    expect(await account.boundingBox()).toEqual(before)
    await page.evaluate(() => document.fonts.ready)
    await mkdir(captured, { recursive: true })
    await page.screenshot({ path: resolve(captured, `navigation-long-${theme}-${width}.png`), fullPage: true })
  })
}

test.describe('移动目标选择的触控与键盘操作', () => {
  test.use({ hasTouch: true })
  for (const theme of ['light', 'dark'] as const) for (const width of [320, 768, 1024, 1440]) {
    test(`${theme} ${width}`, async ({ page }) => {
      await page.setViewportSize({ width: 1440, height: 900 })
      await prepare(page, theme)
      await page.getByRole('button', { name: '管理会话：本周市场观察' }).click()
      await page.getByRole('button', { name: '移动到项目' }).click()
      await page.setViewportSize({ width, height: 900 })
      const dialog = page.getByRole('dialog', { name: '移动到项目' })
      const trigger = dialog.getByRole('button', { name: '目标项目' })
      await trigger.focus()
      await page.keyboard.press('Enter')
      const list = page.getByRole('listbox', { name: '目标项目' })
      await expect(list).toBeFocused()
      const option = list.getByRole('option', { name: '产品开发' })
      for (const control of [trigger, option]) {
        const bounds = (await control.boundingBox())!
        expect(bounds.width).toBeGreaterThanOrEqual(44)
        expect(bounds.height).toBeGreaterThanOrEqual(44)
      }
      const bounds = (await dialog.boundingBox())!
      expect(bounds.x).toBeGreaterThanOrEqual(0)
      expect(bounds.x + bounds.width).toBeLessThanOrEqual(width)
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width)
      await mkdir(reviewCaptured, { recursive: true })
      await page.screenshot({ path: resolve(reviewCaptured, `move-${theme}-${width}.png`), fullPage: true })
      await page.keyboard.press('Escape')
      await expect(list).toHaveCount(0)
      await expect(trigger).toBeFocused()
      await expect(dialog).toBeVisible()
    })
  }
})
