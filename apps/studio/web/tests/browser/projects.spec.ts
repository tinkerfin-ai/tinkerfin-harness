import { emptyTraceGraph } from '../../src/test/traceFixtures'
import { expect, test, type Page } from '@playwright/test'
import fixture from './fixtures/multimodal-history.json' with { type: 'json' }
import { installNotificationStream } from './fixtures/notifications'
import { fulfillExpectedHttpError } from './support/diagnostics'
import { measureBounds } from './support/geometry'

const date = '2030-01-01T00:00:00Z'
const user = { user_id: 1, username: 'project-review', avatar_url: null, roles: [], disabled: false }
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

for (const theme of ['light', 'dark'] as const) for (const width of [320, 768, 1024, 1440]) {
  test(`项目快捷面板居中且新建不叠加弹窗 ${theme} ${width}`, async ({ page }, info) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await prepare(page, theme)
    await page.setViewportSize({ width, height: 900 })
    if (width < 768) await page.getByRole('button', { name: '打开导航', exact: true }).click()
    else if (width < 1024) await page.getByRole('button', { name: '打开侧边栏', exact: true }).click()
    await page.getByRole('button', { name: '切换项目：市场研究' }).click()
    const menu = page.getByRole('dialog')
    await expect(menu).toHaveAccessibleName('项目管理')
    const bounds = (await menu.boundingBox())!
    expect(bounds.x + bounds.width / 2).toBeCloseTo(width / 2, 1)
    expect(bounds.width).toBeLessThanOrEqual(580)
    expect(bounds.x).toBeGreaterThanOrEqual(0)
    await expect(menu.getByRole('searchbox', { name: '搜索项目' })).toBeFocused()
    await expect(page.getByRole('textbox', { name: '消息输入' })).toHaveCount(0)
    const choice = menu.getByRole('button', { name: '市场研究', exact: true })
    await expect(choice).toHaveAttribute('aria-current', 'true')
    await expect(choice).toHaveCSS('box-shadow', 'none')
    await page.screenshot({ path: info.outputPath(`picker-${theme}-${width}.png`) })
    await menu.getByRole('button', { name: '新建项目', exact: true }).click()
    await expect(menu).toHaveAccessibleName('创建项目')
    await expect(menu).toHaveCount(1)
    await expect(menu.getByRole('textbox', { name: '项目名称' })).toBeFocused()
    await page.screenshot({ path: info.outputPath(`create-${theme}-${width}.png`) })
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width)
  })
}

test('项目面板搜索、键盘切换、无匹配时创建与取消', async ({ page }) => {
  await prepare(page)
  const trigger = page.getByRole('button', { name: '切换项目：市场研究' })
  await trigger.press('ArrowDown')
  const menu = page.getByRole('dialog')
  const search = menu.getByRole('searchbox', { name: '搜索项目' })
  await search.fill('产品')
  await search.press('Enter')
  await expect(page.getByRole('button', { name: '切换项目：产品开发' })).toBeVisible()
  await page.getByRole('button', { name: '切换项目：产品开发' }).click()
  await search.fill('新研究空间')
  await expect(menu.getByText('没有匹配的项目')).toBeVisible()
  await search.press('Enter')
  await expect(menu.getByRole('textbox', { name: '项目名称' })).toHaveValue('新研究空间')
  await page.keyboard.press('Escape')
  await expect(search).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(menu).toHaveCount(0)
  await expect(page.getByRole('button', { name: '切换项目：产品开发' })).toBeFocused()
})

test('创建、重命名与切换项目保留独立草稿，长名称不撑开侧栏', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await prepare(page)
  await page.getByRole('button', { name: '切换项目：市场研究' }).click()
  await page.getByRole('dialog', { name: '项目管理', exact: true }).getByRole('button', { name: '新建项目', exact: true }).click()
  await page.getByRole('textbox', { name: '项目名称' }).fill('新研究')
  await page.getByRole('dialog').getByRole('button', { name: '创建', exact: true }).click()
  await expect(page.getByRole('button', { name: '切换项目：新研究' })).toBeVisible()
  await page.getByRole('textbox', { name: '消息输入' }).fill('待研究的问题')
  await page.getByRole('button', { name: '切换项目：新研究' }).click()
  await page.getByRole('dialog', { name: '项目管理', exact: true }).getByRole('button', { name: '重命名项目：新研究', exact: true }).click()
  const name = '市场研究'.repeat(16)
  await page.getByRole('textbox', { name: '项目名称' }).fill(name)
  await page.getByRole('textbox', { name: '项目名称' }).press('Enter')
  const renamed = page.getByRole('button', { name: `切换项目：${name}`, includeHidden: true })
  await expect(page.getByRole('button', { name: `切换项目：${name}`, includeHidden: true })).toBeVisible()
  await expect(page.getByRole('textbox', { name: '消息输入', includeHidden: true })).toHaveValue('待研究的问题')
  expect((await renamed.boundingBox())!.width).toBeLessThan(261)
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(1440)
  await page.getByRole('dialog', { name: '项目管理', exact: true }).getByRole('button', { name: '产品开发', exact: true }).click()
  await expect(page.getByRole('textbox', { name: '消息输入' })).toHaveValue('')
  await page.getByRole('button', { name: '切换项目：产品开发' }).click()
  await page.getByRole('dialog', { name: '项目管理', exact: true }).getByRole('button', { name, exact: true }).click()
  await expect(page.getByRole('textbox', { name: '消息输入' })).toHaveValue('待研究的问题')
})

for (const theme of ['light', 'dark'] as const) {
  test(`普通与归档列表同步当前会话、空列表和深链接 ${theme}`, async ({ page }, info) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    const api = await prepare(page, theme)
    api.threads.push(
      { ...record, id: 2, threadId: 'archived-thread', title: '归档会话选择', archived: true },
      { ...record, id: 3, threadId: 'other-archived-thread', title: '另一条归档会话', archived: true },
      { ...record, id: 4, threadId: 'other-normal-thread', title: '另一条普通会话' },
    )
    const history = page.getByRole('region', { name: '最近对话', exact: true })
    for (const width of [320, 768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 900 })
      if (width < 768) await page.getByRole('button', { name: '打开导航', exact: true }).click()
      else if (width < 1024) await page.getByRole('button', { name: '打开侧边栏', exact: true }).click()
      const normal = history.getByRole('button', { name: '打开会话：本周市场观察', exact: true })
      await expect(normal).toHaveAttribute('aria-current', 'page')
      await page.getByRole('button', { name: '已归档会话', exact: true }).click()
      const current = history.getByRole('button', { name: '打开会话：归档会话选择', exact: true })
      const other = history.getByRole('button', { name: '打开会话：另一条归档会话', exact: true })
      await expect(current).toHaveAttribute('aria-current', 'page')
      await expect(other).not.toHaveAttribute('aria-current')
      await expect(page).toHaveTitle('归档会话选择')
      await expect(page).toHaveURL(/thread=archived-thread/)
      await page.mouse.move(width - 1, 899)
      const surface = (button: typeof current) => button.evaluate(element => {
        let layer: Element = element
        while (getComputedStyle(layer).backgroundColor === 'rgba(0, 0, 0, 0)' && layer.parentElement) layer = layer.parentElement
        return getComputedStyle(layer).backgroundColor
      })
      expect(await surface(current)).not.toBe(await surface(other))
      await page.screenshot({ path: info.outputPath(`archived-selected-${theme}-${width}.png`), animations: 'disabled' })
      await other.click()
      if (width < 768) await page.getByRole('button', { name: '打开导航', exact: true }).click()
      await expect(other).toHaveAttribute('aria-current', 'page')
      await expect(current).not.toHaveAttribute('aria-current')
      await expect(page).toHaveTitle('另一条归档会话')
      await current.click()
      if (width < 768) await page.getByRole('button', { name: '打开导航', exact: true }).click()
      await expect(current).toHaveAttribute('aria-current', 'page')
      await page.getByRole('button', { name: '返回会话记录', exact: true }).click()
      await expect(normal).toHaveAttribute('aria-current', 'page')
      await expect(page).toHaveTitle('本周市场观察')
      await page.screenshot({ path: info.outputPath(`normal-selected-${theme}-${width}.png`), animations: 'disabled' })
      const otherNormal = history.getByRole('button', { name: '打开会话：另一条普通会话', exact: true })
      await otherNormal.click()
      if (width < 768) await page.getByRole('button', { name: '打开导航', exact: true }).click()
      await expect(otherNormal).toHaveAttribute('aria-current', 'page')
      await expect(normal).not.toHaveAttribute('aria-current')
      await expect(page).toHaveTitle('另一条普通会话')
      await normal.click()
      if (width < 768) await page.getByRole('button', { name: '打开导航', exact: true }).click()
      await expect(normal).toHaveAttribute('aria-current', 'page')
    }
    await page.goto('/?project=research&thread=archived-thread')
    await expect(page.getByRole('button', { name: '返回会话记录', exact: true })).toBeVisible()
    await expect(history.getByRole('button', { name: '打开会话：归档会话选择', exact: true })).toHaveAttribute('aria-current', 'page')
    await expect(page).toHaveTitle('归档会话选择')
    await page.goBack()
    await expect(history.getByRole('button', { name: '打开会话：本周市场观察', exact: true })).toHaveAttribute('aria-current', 'page')
    api.threads.splice(1)
    await page.getByRole('button', { name: '已归档会话', exact: true }).click()
    await expect(history.getByText('暂无最近对话', { exact: true })).toBeVisible()
    await expect(history.locator('[aria-current="page"]')).toHaveCount(0)
    await expect(page).not.toHaveURL(/thread=/)
  })
}

for (const theme of ['light', 'dark'] as const) {
  test(`普通与归档列表分别记住非首条会话 ${theme}`, async ({ page }, info) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    const api = await prepare(page, theme)
    api.threads.push(
      { ...record, id: 2, threadId: 'archive-first', title: '首条归档会话', archived: true },
      { ...record, id: 3, threadId: 'archive-selected', title: '上次归档会话', archived: true, updatedAt: '2029-12-26T00:00:00Z' },
      { ...record, id: 4, threadId: 'normal-selected', title: '上次普通会话', updatedAt: '2029-12-26T00:00:00Z' },
    )
    const history = page.getByRole('region', { name: '最近对话', exact: true })
    await page.getByRole('button', { name: '已归档会话', exact: true }).click()
    await page.getByRole('button', { name: '返回会话记录', exact: true }).click()
    const normal = history.getByRole('button', { name: '打开会话：上次普通会话', exact: true })
    const archived = history.getByRole('button', { name: '打开会话：上次归档会话', exact: true })
    await normal.click()
    await page.getByRole('button', { name: '已归档会话', exact: true }).click()
    await archived.click()
    await page.getByRole('button', { name: '返回会话记录', exact: true }).click()
    for (const width of [320, 768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 900 })
      if (width < 768) await page.getByRole('button', { name: '打开导航', exact: true }).click()
      else if (width < 1024) await page.getByRole('button', { name: '打开侧边栏', exact: true }).click()
      await expect(normal).toHaveAttribute('aria-current', 'page')
      await expect(history.getByRole('button', { name: '打开会话：本周市场观察', exact: true })).not.toHaveAttribute('aria-current')
      await expect(page).toHaveTitle('上次普通会话')
      await expect(page).toHaveURL(/thread=normal-selected/)
      await page.screenshot({ path: info.outputPath(`remembered-normal-${theme}-${width}.png`), animations: 'disabled' })
      await page.getByRole('button', { name: '已归档会话', exact: true }).click()
      await expect(archived).toHaveAttribute('aria-current', 'page')
      await expect(history.getByRole('button', { name: '打开会话：首条归档会话', exact: true })).not.toHaveAttribute('aria-current')
      await expect(page).toHaveTitle('上次归档会话')
      await expect(page).toHaveURL(/thread=archive-selected/)
      await page.screenshot({ path: info.outputPath(`remembered-archived-${theme}-${width}.png`), animations: 'disabled' })
      await page.getByRole('button', { name: '返回会话记录', exact: true }).click()
      await expect(normal).toHaveAttribute('aria-current', 'page')
      await expect(page).toHaveTitle('上次普通会话')
    }
    for (const targetArchived of [true, false]) {
      let release: () => void = () => undefined
      const held = new Promise<void>(resolve => { release = resolve })
      const listPattern = '**/api/conversation/history*'
      await page.route(listPattern, async route => {
        await held
        await route.fallback()
      })
      const response = page.waitForResponse(url => new URL(url.url()).pathname === '/api/conversation/history')
      await page.getByRole('button', { name: targetArchived ? '已归档会话' : '返回会话记录', exact: true }).click()
      await expect(targetArchived ? archived : normal).toHaveAttribute('aria-current', 'page')
      await expect(page).toHaveTitle(targetArchived ? '上次归档会话' : '上次普通会话')
      await expect(page.getByRole('region', { name: '对话内容', exact: true }).getByText('整理本周需要关注的市场变化', { exact: true })).toBeVisible()
      await expect(page.getByText('正在加载历史会话', { exact: true })).toHaveCount(0)
      release()
      await response
      await page.unroute(listPattern)
    }
    await page.getByRole('button', { name: '已归档会话', exact: true }).click()
    let detailUnavailable = true
    await page.route('**/api/conversation/normal-selected/history*', async route => {
      if (detailUnavailable) return fulfillExpectedHttpError(route, 503, '记忆会话详情临时不可用')
      await route.fallback()
    })
    await page.route('**/api/conversation/history*', async route => {
      const url = new URL(route.request().url())
      if (url.searchParams.get('archived') === 'true') return route.fallback()
      const older = url.searchParams.has('cursor')
      await route.fulfill({ json: { code: 0, message: 'success', data: {
        items: older ? api.threads.filter(item => item.threadId === 'normal-selected') : [record],
        nextCursor: older ? null : 'next-page',
      } } })
    })
    await page.getByRole('button', { name: '返回会话记录', exact: true }).click()
    await expect(page.getByText('历史会话加载失败', { exact: true })).toBeVisible()
    await expect(page).toHaveTitle('上次普通会话')
    await expect(normal).toHaveAttribute('aria-current', 'page')
    await expect(page.getByRole('region', { name: '对话内容', exact: true }).getByText('整理本周需要关注的市场变化', { exact: true })).toBeVisible()
    await expect(history.getByRole('button', { name: '打开会话：本周市场观察', exact: true })).not.toHaveAttribute('aria-current')
    detailUnavailable = false
    await page.getByRole('button', { name: '重新加载', exact: true }).click()
    await expect(normal).toHaveAttribute('aria-current', 'page')
    await expect(page).toHaveTitle('上次普通会话')
    await expect(page).toHaveURL(/thread=normal-selected/)
  })
}

test('会话移动、归档与恢复使用目标项目', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await prepare(page)
  await page.getByRole('button', { name: '管理会话：本周市场观察' }).click()
  await page.getByRole('button', { name: '移动到项目' }).click()
  await page.getByRole('dialog').getByRole('button', { name: '移动', exact: true }).click()
  await expect(page.getByRole('button', { name: '管理会话：本周市场观察' })).toHaveCount(0)
  await page.getByRole('button', { name: '切换项目：市场研究' }).click()
  await page.getByRole('dialog', { name: '项目管理', exact: true }).getByRole('button', { name: '产品开发', exact: true }).click()
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
  await expect(nav.getByRole('button', { name: '更多', exact: true })).toHaveCount(0)
  await expect(nav.getByRole('button', { name: '工作区', exact: true })).toBeEnabled()
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

for (const theme of ['light', 'dark'] as const) for (const width of [320, 768, 1024, 1440]) {
  test(`会话搜索仅在输入关键词后显示模糊匹配 ${theme} ${width}`, async ({ page }, testInfo) => {
    await page.clock.install({ time: new Date(date) })
    await page.setViewportSize({ width: 1440, height: 900 })
    await prepare(page, theme)
    await page.locator('[aria-keyshortcuts~="Meta+K"]').click()
    await page.setViewportSize({ width, height: 900 })
    if (width < 768) await page.getByRole('button', { name: '打开导航', exact: true }).click()
    else if (width < 1024) await page.getByRole('button', { name: '打开侧边栏', exact: true }).click()
    await page.clock.pauseAt(new Date('2030-01-01T00:01:00Z'))
    const queries: string[] = []
    await page.route('**/api/conversation/history?**', route => {
      const params = new URL(route.request().url()).searchParams
      if (params.has('scope')) queries.push(params.get('query') ?? '')
      return route.fallback()
    })
    await page.getByRole('button', { name: '搜索会话', exact: true }).click()
    const dialog = page.getByRole('dialog', { name: '搜索会话', exact: true })
    const input = dialog.getByRole('combobox', { name: '搜索会话' })
    await expect(input).toBeFocused()
    await page.clock.runFor(300)
    await expect(input).toHaveAttribute('placeholder', '输入关键词')
    await expect(dialog.getByRole('status')).toHaveCount(0)
    await expect(input).toHaveAttribute('aria-expanded', 'false')
    await expect(dialog.getByText('方向键选择，Enter 打开，Esc 关闭')).toHaveCount(0)
    const [emptyPanel] = await measureBounds(dialog)
    const [emptyInput] = await measureBounds(input)
    expect(emptyPanel.height).toBeLessThan(480)
    expect(emptyPanel.y).toBe((900 - 480) / 2)
    expect(queries).toEqual([])

    await page.screenshot({ path: testInfo.outputPath(`empty-${theme}-${width}.png`) })
    await dialog.getByRole('button', { name: '选择搜索范围' }).click()
    const scope = dialog.getByRole('listbox', { name: '搜索范围', exact: true })
    const all = scope.getByRole('option', { name: '全部项目', exact: true })
    await expect(all).toBeInViewport()
    await all.click()
    await expect(scope).toHaveCount(0)
    expect(queries).toEqual([])
    await input.fill('周')
    await page.clock.runFor(300)
    await expect(dialog.getByRole('option')).toHaveCount(1)
    await expect(dialog.getByRole('option')).toContainText('本周市场观察')
    await expect(dialog.getByRole('option', { selected: true })).toHaveCount(0)
    await expect(input).not.toHaveAttribute('aria-activedescendant')
    expect(queries).toEqual(['周'])
    const bounds = (await dialog.boundingBox())!
    expect(bounds.height).toBe(480)
    expect(bounds.y).toBe(emptyPanel.y)
    expect((await measureBounds(input))[0]).toEqual(emptyInput)
    expect(bounds.y + bounds.height / 2).toBeCloseTo(450, 4)
    expect(bounds.x).toBeGreaterThanOrEqual(0)
    expect(bounds.x + bounds.width).toBeLessThanOrEqual(width)
    await page.screenshot({ path: testInfo.outputPath(`results-${theme}-${width}.png`) })
    await input.press('ArrowDown')
    await expect(dialog.getByRole('option', { selected: true })).toContainText('本周市场观察')
    await page.screenshot({ path: testInfo.outputPath(`selected-${theme}-${width}.png`) })
    await input.fill('不存在的标题')
    await page.clock.runFor(300)
    const noResults = dialog.getByText('没有匹配的对话', { exact: true })
    await expect(noResults).toBeVisible()
    const [noMatchPanel] = await measureBounds(dialog)
    expect(noMatchPanel).toEqual(bounds)
    await page.screenshot({ path: testInfo.outputPath(`no-match-${theme}-${width}.png`) })
    await input.clear()
    await page.clock.runFor(300)
    await expect(input).toHaveAttribute('placeholder', '输入关键词')
    await expect(dialog.getByRole('status')).toHaveCount(0)
    await expect(dialog.getByRole('option')).toHaveCount(0)
    const [clearedPanel] = await measureBounds(dialog)
    expect(clearedPanel).toEqual(emptyPanel)
    expect(queries).toEqual(['周', '不存在的标题'])
  })
}

for (const theme of ['light', 'dark'] as const) {
  test(`搜索结果在短视口内滚动且输入和底栏保持可见 ${theme}`, async ({ page }) => {
    await page.clock.install({ time: new Date(date) })
    await page.setViewportSize({ width: 1440, height: 900 })
    const state = await prepare(page, theme)
    state.threads.push(...Array.from({ length: 9 }, (_, index) => ({ ...record, id: index + 2, threadId: `search-${index}`, title: `市场观察 ${index + 1}` })))
    await page.setViewportSize({ width: 768, height: 420 })
    await page.clock.pauseAt(new Date('2030-01-01T00:01:00Z'))
    await page.getByRole('button', { name: '搜索会话', exact: true }).click()
    const dialog = page.getByRole('dialog', { name: '搜索会话', exact: true })
    const input = dialog.getByRole('combobox', { name: '搜索会话' })
    await input.fill('观察')
    await page.clock.runFor(300)
    const options = dialog.getByRole('option')
    await expect(options).toHaveCount(10)
    const last = options.last()
    await last.scrollIntoViewIfNeeded()
    await expect(last).toBeInViewport()
    await expect(input).toBeInViewport()
    const footer = dialog.getByText('方向键选择，Enter 打开，Esc 关闭')
    await expect(footer).toBeInViewport()
    const [rowBox, footerBox] = await measureBounds(last, footer)
    expect(rowBox.y + rowBox.height).toBeLessThanOrEqual(footerBox.y)
    const [panel] = await measureBounds(dialog)
    expect(panel.y + panel.height / 2).toBeCloseTo(210, 4)
    expect(panel.y + panel.height).toBeLessThanOrEqual(420)
  })
}

test('会话搜索独立保留侧栏，支持范围、分页、重试、输入法和跨项目打开', async ({ page }) => {
  await page.clock.install({ time: new Date(date) })
  await page.setViewportSize({ width: 1440, height: 900 })
  const state = await prepare(page)
  const remote = { ...record, id: 50, projectId: 'product', threadId: 'product-search', title: '产品市场交互方案' }
  const next = { ...remote, id: 51, threadId: 'product-next', title: '接口市场交互记录' }
  state.threads.push(...Array.from({ length: 24 }, (_, index) => ({ ...record, id: index + 2, threadId: `research-${index}`, title: `市场研究记录 ${index + 1}` })), remote, next)
  await page.reload()
  const history = page.getByRole('region', { name: '最近对话', includeHidden: true })
  await expect(history.getByRole('button', { name: '打开会话：市场研究记录 24', exact: true })).toHaveCount(1)
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
      items: items.filter(item => item.title.includes(query)), nextCursor: scope === 'all' && !params.has('cursor') && query ? 'search-next' : null,
    } } })
  })
  await page.getByRole('button', { name: '搜索会话', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '搜索会话', exact: true })
  const input = dialog.getByRole('combobox', { name: '搜索会话' })
  const results = dialog.getByRole('listbox', { name: '会话搜索结果' })
  await expect(input).toBeFocused()
  await expect(input).toHaveAttribute('placeholder', '输入关键词')
  await expect(dialog.getByRole('status')).toHaveCount(0)
  await page.clock.runFor(300)
  expect(requests).toEqual([])
  await expect(results.getByRole('option')).toHaveCount(0)
  await input.fill('市场')
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
  const requestsBeforeClear = requests.length
  await input.fill('   ')
  await expect(input).toHaveAttribute('placeholder', '输入关键词')
  await expect(dialog.getByRole('status')).toHaveCount(0)
  await expect(results.getByRole('option')).toHaveCount(0)
  await page.clock.runFor(300)
  await dialog.getByRole('button', { name: '选择搜索范围' }).click()
  const scope = dialog.getByRole('listbox', { name: '搜索范围', exact: true })
  await scope.press('ArrowDown')
  await scope.press('Enter')
  await page.clock.runFor(300)
  expect(requests).toHaveLength(requestsBeforeClear)
  await input.fill('市场')
  await page.clock.runFor(300)
  await expect(results.getByRole('option')).toHaveCount(2)
  await expect(results.getByRole('option', { name: /产品市场交互方案/ })).toContainText('产品开发')
  await dialog.getByRole('button', { name: '加载更多' }).click()
  await expect(results.getByRole('option')).toHaveCount(3)
  expect(requests.some(request => new URLSearchParams(request).get('cursor') === 'search-next')).toBe(true)
  expect(await sidebarState()).toEqual(beforeSearch)
  await input.focus()
  await input.dispatchEvent('compositionstart')
  for (const key of ['ArrowDown', 'Enter']) await input.dispatchEvent('keydown', { key, isComposing: true, bubbles: true })
  await input.dispatchEvent('compositionend')
  await expect(dialog).toBeVisible()
  await expect(results.getByRole('option', { selected: true })).toHaveCount(0)
  await input.press('Control+k')
  await expect(dialog).toBeVisible()
  await input.press('ArrowDown')
  await expect(results.getByRole('option', { selected: true })).toContainText('本周市场观察')
  await input.press('ArrowDown')
  await expect(results.getByRole('option', { selected: true })).toContainText('产品市场交互方案')
  await input.press('Enter')
  await expect(dialog).toHaveCount(0)
  await expect(page.getByRole('button', { name: '切换项目：产品开发' })).toBeVisible()
  expect(new URL(page.url()).searchParams.get('project')).toBe('product')
  expect(new URL(page.url()).searchParams.get('thread')).toBe('product-search')
  await expect(page.getByRole('button', { name: '打开会话：产品市场交互方案', exact: true })).toBeVisible()
  await expect(page.getByRole('textbox', { name: '消息输入' })).toBeEnabled()
})

test('项目重命名取消和 Escape 保留原名称，返回列表后搜索获得焦点', async ({ page }) => {
  await prepare(page)
  await page.getByRole('button', { name: '切换项目：市场研究' }).click()
  const menu = page.getByRole('dialog')
  for (const cancel of ['button', 'escape']) {
    await menu.getByRole('button', { name: '重命名项目：市场研究' }).click()
    await menu.getByRole('textbox', { name: '项目名称' }).fill('未保存名称')
    if (cancel === 'button') await menu.getByRole('button', { name: '取消', exact: true }).click()
    else await page.keyboard.press('Escape')
    await expect(menu.getByRole('searchbox', { name: '搜索项目' })).toBeFocused()
    await expect(menu.getByRole('button', { name: '市场研究', exact: true })).toHaveAttribute('aria-current', 'true')
  }
})

test('项目长列表支持搜索，浏览器返回时撤销原项目编辑', async ({ page }) => {
  const state = await prepare(page)
  state.projects.push(...Array.from({ length: 40 }, (_, index) => ({ id: `project-${index}`, name: `项目 ${index}`, createdAt: date, updatedAt: date })))
  await page.reload()
  await page.getByRole('button', { name: '切换项目：市场研究' }).click()
  const menu = page.getByRole('dialog')
  await menu.getByRole('searchbox', { name: '搜索项目' }).fill('项目 39')
  await menu.getByRole('button', { name: '项目 39', exact: true }).click()
  await page.getByRole('button', { name: '切换项目：项目 39' }).click()
  await menu.getByRole('searchbox', { name: '搜索项目' }).fill('项目 39')
  await menu.getByRole('button', { name: '重命名项目：项目 39' }).click()
  await menu.getByRole('textbox', { name: '项目名称' }).fill('其他项目草稿')
  await page.goBack()
  await expect(menu.getByRole('textbox', { name: '项目名称' })).toHaveCount(0)
  await expect(menu.getByRole('searchbox', { name: '搜索项目' })).toBeVisible()
})

test.describe('项目面板触控操作', () => {
  test.use({ hasTouch: true })
  for (const width of [320, 768, 1024, 1440]) test(`操作目标尺寸 ${width}`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await prepare(page)
    await page.getByRole('button', { name: '切换项目：市场研究' }).click()
    await page.setViewportSize({ width, height: 900 })
    const menu = page.getByRole('dialog')
    await expect(menu.getByRole('button', { name: '重命名项目：产品开发' })).toHaveCSS('opacity', '1')
    for (const button of await menu.getByRole('button').all()) {
      if (!await button.isVisible()) continue
      const box = (await button.boundingBox())!
      expect(box.width).toBeGreaterThanOrEqual(44)
      expect(box.height).toBeGreaterThanOrEqual(44)
    }
    await menu.getByRole('button', { name: '重命名项目：产品开发' }).click()
    for (const name of ['保存', '取消']) {
      const box = (await menu.getByRole('button', { name, exact: true }).boundingBox())!
      expect(box.width).toBeGreaterThanOrEqual(44)
      expect(box.height).toBeGreaterThanOrEqual(44)
    }
  })
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
  test(`项目表单校验与确认按钮 ${theme} ${width}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await prepare(page, theme)
    if (theme === 'light' && width === 1440) await page.emulateMedia({ reducedMotion: 'no-preference' })
    await page.getByRole('button', { name: '切换项目：市场研究' }).click()
    await page.getByRole('dialog', { name: '项目管理', exact: true }).getByRole('button', { name: '新建项目', exact: true }).click()
    await page.setViewportSize({ width, height: 900 })
    const dialog = page.getByRole('dialog', { name: '创建项目' })
    const name = dialog.getByRole('textbox', { name: '项目名称' })
    const confirm = dialog.getByRole('button', { name: '创建', exact: true })
    await name.fill('   ')
    await expect(confirm).toBeDisabled()
    await expect(name).toBeFocused()
    const surface = await name.evaluate(input => {
      const control = input.parentElement!
      const style = getComputedStyle(control)
      return { border: style.borderTopWidth, outline: style.outlineWidth, shadow: style.boxShadow, padding: style.paddingInlineStart, height: parseFloat(style.height) }
    })
    expect(surface).toEqual({ border: '0px', outline: '0px', shadow: 'none', padding: '16px', height: 44 })
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

    await page.screenshot({ path: testInfo.outputPath(`project-form-${theme}-${width}.png`), fullPage: true })
  })

  test(`项目导航与记忆页面 ${theme} ${width}`, async ({ page }, testInfo) => {
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
    const add = page.getByRole('button', { name: '新增记忆', exact: true })
    await expect(add).toBeVisible()
    await expect(add).toHaveText('新增')
    await expect(page.getByText('这里的记忆会用于此项目的后续会话')).toHaveCount(0)
    const [addBox] = await measureBounds(add)
    expect(addBox.y + addBox.height / 2).toBe(32)
    const search = page.getByRole('button', { name: '搜索记忆', exact: true })
    await expect(page.getByRole('searchbox', { name: '搜索记忆', exact: true })).toHaveCount(0)
    const [searchIcon] = await measureBounds(search.locator('svg'))
    expect(addBox.x - searchIcon.x - searchIcon.width).toBe(15)
    await search.click()
    const searchInput = page.getByRole('searchbox', { name: '搜索记忆', exact: true })
    await expect(searchInput).toBeFocused()
    await expect(searchInput).toHaveCSS('font-size', '13px')
    await searchInput.fill('没有这条记忆')
    await expect(page.getByRole('status')).toHaveText('没有匹配的记忆')
    await page.getByRole('button', { name: '关闭搜索', exact: true }).click()
    await expect(search).toBeFocused()
    await expect(page.getByRole('button', { name: '编辑记忆：preferences.md' })).toBeVisible()
    await add.click()
    const createMemory = page.getByRole('dialog', { name: '新增记忆', exact: true })
    await expect(createMemory.getByRole('textbox', { name: '记忆名称' })).toBeFocused()
    await createMemory.getByRole('button', { name: '取消', exact: true }).click()
    await expect(add).toBeFocused()
    await page.evaluate(() => document.fonts.ready)
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width)

    await page.screenshot({ path: testInfo.outputPath(`memories-${theme}-${width}.png`), fullPage: true })

    await page.screenshot({ path: testInfo.outputPath(`header-memories-${theme}-${width}.png`) })
    await search.click()
    await expect(searchInput).toHaveValue('')
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width)
    await page.screenshot({ path: testInfo.outputPath(`header-search-${theme}-${width}.png`) })
    await searchInput.press('Escape')
    await expect(search).toBeFocused()
    if (width === 320) {
      await page.getByRole('button', { name: '打开导航' }).click()
      await expect(page.getByRole('button', { name: '关闭导航', exact: true })).toBeFocused()
      await page.screenshot({ path: testInfo.outputPath(`navigation-${theme}-${width}.png`), fullPage: true })
    }
  })
}

for (const theme of ['light', 'dark'] as const) for (const width of [320, 1440]) {
  test(`长会话列表独立滚动且账号保持在底部 ${theme} ${width}`, async ({ page }, testInfo) => {
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

    await page.screenshot({ path: testInfo.outputPath(`navigation-long-${theme}-${width}.png`), fullPage: true })
  })
}

test.describe('移动目标选择的触控与键盘操作', () => {
  test.use({ hasTouch: true })
  for (const theme of ['light', 'dark'] as const) for (const width of [320, 768, 1024, 1440]) {
    test(`${theme} ${width}`, async ({ page }, testInfo) => {
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

      await page.screenshot({ path: testInfo.outputPath(`move-${theme}-${width}.png`), fullPage: true })
      await page.keyboard.press('Escape')
      await expect(list).toHaveCount(0)
      await expect(trigger).toBeFocused()
      await expect(dialog).toBeVisible()
    })
  }
})

for (const theme of ['light', 'dark'] as const) for (const width of [320, 768, 1024, 1440]) {
  test(`历史会话与链路的加载反馈采用同一外观 ${theme} ${width}`, async ({ page }, info) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await prepare(page, theme)
    await page.setViewportSize({ width, height: 900 })
    if (width < 768) await page.getByRole('button', { name: '打开导航', exact: true }).click()
    else if (width < 1024) await page.getByRole('button', { name: '打开侧边栏', exact: true }).click()
    let releaseList: () => void = () => undefined
    const heldList = new Promise<void>(resolve => { releaseList = resolve })
    const listPattern = '**/api/conversation/history*'
    await page.route(listPattern, async route => { await heldList; await route.fallback() })
    await page.getByRole('button', { name: '已归档会话', exact: true }).click()
    if (width < 768) await page.getByRole('button', { name: '关闭导航', exact: true }).click()
    const history = page.getByRole('status').filter({ hasText: '正在加载历史会话' })
    await expect(history).toBeVisible()
    const appearance = (status: typeof history) => status.evaluate(element => {
      const style = getComputedStyle(element)
      const icon = element.querySelector('svg')!
      const title = [...element.children].find(child => child.textContent)!
      return { border: style.borderTopWidth, background: style.backgroundColor, shadow: style.boxShadow,
        gap: style.columnGap, fontSize: getComputedStyle(title).fontSize, weight: getComputedStyle(title).fontWeight,
        iconWidth: icon.getBoundingClientRect().width, color: getComputedStyle(icon).color,
        animation: getComputedStyle(icon).animationName }
    })
    const historyAppearance = await appearance(history)
    expect(historyAppearance).toMatchObject({ border: '0px', background: 'rgba(0, 0, 0, 0)', shadow: 'none', gap: '8px', fontSize: '13px', iconWidth: 18, animation: 'none' })
    await page.screenshot({ path: info.outputPath(`history-loading-${theme}-${width}.png`), animations: 'disabled' })
    releaseList()
    await expect(history).toHaveCount(0)
    await page.unroute(listPattern)
    if (width < 768) await page.getByRole('button', { name: '打开导航', exact: true }).click()
    await page.getByRole('button', { name: '返回会话记录', exact: true }).click()
    if (width < 768) await page.getByRole('button', { name: '关闭导航', exact: true }).click()
    await expect(page).toHaveTitle('本周市场观察')
    let releaseGraph: () => void = () => undefined
    const heldGraph = new Promise<void>(resolve => { releaseGraph = resolve })
    await page.route('**/api/conversation/research-thread/trace/graph?*', async route => {
      await heldGraph
      await route.fulfill({ json: { code: 0, message: 'success', data: { ...emptyTraceGraph(3), nextCursor: null, generation: 'project-preview', headRunId: 'run' } } })
    })
    await page.getByRole('button', { name: '链路', exact: true }).click()
    const chain = page.getByRole('status').filter({ hasText: '正在加载链路…' })
    await expect(chain).toBeVisible()
    expect(await appearance(chain)).toEqual(historyAppearance)
    await page.screenshot({ path: info.outputPath(`chain-loading-${theme}-${width}.png`), animations: 'disabled' })
    releaseGraph()
    await expect(chain).toHaveCount(0)
  })
}

test('重命名其他项目保留当前项目和草稿，当前项目失焦保存同步标题', async ({ page }) => {
  await prepare(page)
  const composer = page.getByRole('textbox', { name: '消息输入' })
  await composer.fill('保留当前项目草稿')
  await page.getByRole('button', { name: '切换项目：市场研究' }).click()
  const menu = page.getByRole('dialog')
  const other = menu.getByRole('button', { name: '重命名项目：产品开发', exact: true })
  await menu.getByRole('button', { name: '产品开发', exact: true }).hover()
  await expect(other).toHaveCSS('opacity', '1')
  await other.click()
  const input = menu.getByRole('textbox', { name: '项目名称' })
  await expect(input).toHaveValue('产品开发')
  expect(await input.evaluate(element => (element as HTMLInputElement).selectionStart)).toBe('产品开发'.length)
  await input.fill('产品设计')
  await input.press('Enter')
  await expect(menu.getByRole('button', { name: '产品设计', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: '切换项目：市场研究', includeHidden: true })).toBeVisible()
  await menu.getByRole('button', { name: '重命名项目：市场研究', exact: true }).click()
  await input.fill('市场分析')
  await page.mouse.click(8, 8)
  await expect(page.getByRole('button', { name: '切换项目：市场分析' })).toBeVisible()
  await expect(composer).toHaveValue('保留当前项目草稿')
  await expect(page.getByRole('button', { name: '切换项目：市场分析' })).toBeFocused()
  await expect(page).toHaveURL(/project=research/)
})

test('非当前项目在外部保存失败后保留编辑目标和输入，重试不会切换项目', async ({ page }) => {
  await prepare(page)
  let fail = true
  await page.route('**/api/projects/product', route => {
    if (route.request().method() === 'PATCH' && fail) { fail = false; return fulfillExpectedHttpError(route, 503, '项目未能保存，请重试') }
    return route.fallback()
  })
  await page.getByRole('button', { name: '切换项目：市场研究' }).click()
  const menu = page.getByRole('dialog')
  await menu.getByRole('button', { name: '产品开发', exact: true }).hover()
  await menu.getByRole('button', { name: '重命名项目：产品开发', exact: true }).click()
  const input = menu.getByRole('textbox', { name: '项目名称' })
  await input.fill('重试后的产品')
  await page.mouse.click(8, 8)
  await expect(menu).toBeVisible()
  await expect(input).toHaveValue('重试后的产品')
  await expect(menu.getByRole('alert')).toHaveText('服务暂不可用，请稍后重试')
  await menu.getByRole('button', { name: '保存', exact: true }).click()
  await expect(menu.getByRole('button', { name: '重试后的产品', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: '切换项目：市场研究', includeHidden: true })).toBeVisible()
})

test('项目导航采用紧凑比例，面板新建入口固定右上角', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await prepare(page)
  const nav = page.getByRole('navigation', { name: '工作区功能' })
  for (const name of ['技能库', '记忆管理', '定时任务', '工作区']) {
    const item = nav.getByRole('button', { name, exact: true })
    await expect(item).toHaveCSS('min-height', '40px')
    await expect(item).toHaveCSS('font-size', '12px')
    await expect(item.locator('svg')).toHaveCSS('width', '16px')
  }
  await page.getByRole('button', { name: '切换项目：市场研究' }).click()
  const menu = page.getByRole('dialog')
  const create = menu.getByRole('button', { name: '新建项目', exact: true })
  const [createBox, closeBox] = await measureBounds(create, menu.getByRole('button', { name: '关闭对话框' }))
  expect(createBox.y + createBox.height / 2).toBe(closeBox.y + closeBox.height / 2)
  expect(createBox.x + createBox.width).toBeLessThanOrEqual(closeBox.x)
  await expect(menu.getByRole('button', { name: '市场研究', exact: true })).toHaveCSS('font-size', '13px')
})

test('项目搜索输入法确认不触发切换，方向键从搜索框进入首尾项目', async ({ page }) => {
  await prepare(page)
  await page.getByRole('button', { name: '切换项目：市场研究' }).click()
  const menu = page.getByRole('dialog')
  const search = menu.getByRole('searchbox', { name: '搜索项目' })
  await search.evaluate(input => input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', isComposing: true, bubbles: true })))
  await expect(menu).toHaveAccessibleName('项目管理')
  await expect(search).toBeFocused()
  await search.press('ArrowUp')
  await expect(menu.getByRole('button', { name: '产品开发', exact: true })).toBeFocused()
  await search.focus()
  await search.press('ArrowDown')
  await expect(menu.getByRole('button', { name: '市场研究', exact: true })).toBeFocused()
})

for (const width of [320, 768, 1440]) test(`项目切换和关闭后恢复可见入口焦点 ${width}`, async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await prepare(page)
  await page.setViewportSize({ width, height: 900 })
  if (width < 768) await page.getByRole('button', { name: '打开导航', exact: true }).click()
  const trigger = page.getByRole('button', { name: width === 768 ? '选择项目' : '切换项目：市场研究', exact: true })
  await trigger.click()
  await page.keyboard.press('Escape')
  await expect(trigger).toBeFocused()
  await trigger.click()
  await page.getByRole('dialog').getByRole('button', { name: '产品开发', exact: true }).click()
  if (width >= 1024) await expect(page.getByRole('button', { name: '切换项目：产品开发' })).toBeFocused()
  else await expect(page.getByRole('button', { name: width === 768 ? '选择项目' : '打开导航', exact: true })).toBeFocused()
  if (width < 768) await page.getByRole('button', { name: '打开导航', exact: true }).click()
  await page.getByRole('button', { name: width === 768 ? '选择项目' : '切换项目：产品开发', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: '新建项目', exact: true }).click()
  await page.getByRole('textbox', { name: '项目名称' }).fill('焦点验收')
  await page.getByRole('dialog').getByRole('button', { name: '创建', exact: true }).click()
  await expect(page.getByRole('button', { name: width >= 1024 ? '切换项目：焦点验收' : width === 768 ? '选择项目' : '打开导航', exact: true })).toBeFocused()
})

for (const width of [320, 768, 1024, 1440]) test(`项目面板右侧操作图标共用对齐线 ${width}`, async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await prepare(page)
  await page.getByRole('button', { name: '切换项目：市场研究' }).click()
  await page.setViewportSize({ width, height: 900 })
  const panel = page.getByRole('dialog')
  const close = panel.getByRole('button', { name: '关闭对话框' })
  const rename = panel.getByRole('button', { name: '重命名项目：市场研究' })
  const create = panel.getByRole('button', { name: '新建项目', exact: true })
  const icons = await measureBounds(close.locator('svg'), rename.locator('svg'))
  expect(icons[1].x + icons[1].width / 2).toBe(icons[0].x + icons[0].width / 2)
  const [createBox, closeBox] = await measureBounds(create, close)
  expect(createBox.y + createBox.height / 2).toBe(closeBox.y + closeBox.height / 2)
  await rename.hover()
  await expect(rename).toHaveCSS('background-color', 'rgba(0, 0, 0, 0)')
  await panel.getByRole('searchbox', { name: '搜索项目' }).fill('市场')
  await expect(panel.getByRole('button', { name: '清空搜索' })).toHaveCount(0)

})

test('项目入口恢复焦点无残留外框，键盘仍可打开面板', async ({ page }) => {
  await prepare(page)
  const trigger = page.getByRole('button', { name: '切换项目：市场研究' })
  await trigger.click()
  await page.getByRole('searchbox', { name: '搜索项目' }).fill('市场')
  await page.getByRole('dialog').getByRole('button', { name: '市场研究', exact: true }).click()
  await expect(trigger).toBeFocused()
  await expect(trigger).toHaveCSS('outline-width', '0px')
  await trigger.press('Enter')
  await expect(page.getByRole('dialog')).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(trigger).toBeFocused()
  await expect(trigger).toHaveCSS('outline-width', '0px')
  await trigger.press('Tab')
  await page.keyboard.press('Shift+Tab')
  await expect(trigger).toBeFocused()
  await expect(trigger).toHaveCSS('outline-width', '2px')
})

for (const width of [320, 768, 1024, 1440]) test(`项目与会话搜索标题及定位一致 ${width}`, async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await prepare(page)
  await page.getByRole('button', { name: '搜索会话', exact: true }).click()
  await page.setViewportSize({ width, height: 900 })
  const dialog = page.getByRole('dialog')
  const [searchBox, searchTitle, searchClose] = await measureBounds(dialog, dialog.getByRole('heading'), dialog.getByRole('button', { name: '关闭对话框' }))
  const titleStyle = await dialog.getByRole('heading').evaluate(el => ({ size: getComputedStyle(el).fontSize, weight: getComputedStyle(el).fontWeight }))
  await page.keyboard.press('Escape')
  if (width < 768) await page.getByRole('button', { name: '打开导航', exact: true }).click()
  await page.getByRole('button', { name: width >= 768 && width < 1024 ? '选择项目' : '切换项目：市场研究', exact: true }).click()
  const [box, title, close] = await measureBounds(dialog, dialog.getByRole('heading'), dialog.getByRole('button', { name: '关闭对话框' }))
  expect({ x: box.x, y: box.y, width: box.width }).toEqual({ x: searchBox.x, y: searchBox.y, width: searchBox.width })
  expect(title).toEqual(searchTitle)
  expect(close).toEqual(searchClose)
  expect(await dialog.getByRole('heading').evaluate(el => ({ size: getComputedStyle(el).fontSize, weight: getComputedStyle(el).fontWeight }))).toEqual(titleStyle)
  await dialog.getByRole('searchbox').fill('没有这个项目')
  expect((await measureBounds(dialog))[0]).toEqual(box)
  await dialog.getByRole('button', { name: '新建项目', exact: true }).click()
  expect((await measureBounds(dialog))[0]).toEqual(box)
  expect((await measureBounds(dialog.getByRole('heading')))[0]).toEqual(title)
})

test('侧栏右侧操作同列且品牌与页头垂直居中', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  const state = await prepare(page)
  state.threads[0].pinned = true
  await page.reload()
  const trigger = page.getByRole('button', { name: '切换项目：市场研究' })
  const more = page.getByRole('button', { name: '管理会话：本周市场观察' })
  const [chevron, dots] = await measureBounds(trigger.locator('svg').last(), more.locator('svg'))
  expect(chevron.x + chevron.width / 2).toBe(dots.x + dots.width / 2)
  const [collapse, cloud] = await measureBounds(page.getByRole('button', { name: '收起侧边栏', exact: true }), page.getByRole('button', { name: '云端', exact: true }))
  expect(collapse.y + collapse.height / 2).toBe(cloud.y + cloud.height / 2)
})

test('侧栏品牌图形、项目图标和置顶文字共用竖向中心线', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  const state = await prepare(page)
  state.threads[0].pinned = true
  await page.reload()
  const heading = page.getByRole('heading', { name: '置顶', exact: true })
  const center = await heading.evaluate(element => { const range = document.createRange(); range.selectNodeContents(element); const box = range.getBoundingClientRect(); return box.x + box.width / 2 })
  const [mark, project] = await measureBounds(page.locator('.sidebar-head .brand-logo__mark'), page.getByRole('button', { name: '切换项目：市场研究' }).locator('svg').first())
  expect(mark.x + mark.width / 2).toBeCloseTo(center, 1)
  expect(project.x + project.width / 2).toBeCloseTo(center, 1)
})
