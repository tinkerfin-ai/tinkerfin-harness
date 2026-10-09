import { expect, test, type Page } from '@playwright/test'
import { mkdir } from 'node:fs/promises'
import { resolve } from 'node:path'
import type { ConversationHistoryDetail } from '../../src/api/conversation/history'
import type { WorkspaceFile } from '../../src/features/workspaceFiles/api'
import { emptyTraceGraph } from '../../src/test/traceFixtures'
import { installNotificationStream } from './fixtures/notifications'
import { measureBounds } from './support/geometry'

declare global {
  interface Window { emitWorkspaceChange: (project: string) => void }
}
const date = '2030-01-01T00:00:00Z'
const user = { user_id: 1, username: 'workspace-browser', display_name: '工作区用户', avatar_url: null, roles: [], disabled: false }
const file = (path: string, kind: WorkspaceFile['kind'] = 'file', etag = 'first'): WorkspaceFile => ({ path, name: path.split('/').at(-1)!, kind, sizeBytes: kind === 'file' ? 84 : null, modifiedAt: date, etag })

async function prepare(page: Page, theme = 'light') {
  await page.clock.install({ time: new Date(date) })
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await installNotificationStream(page)
  await page.addInitScript(({ user, theme }) => {
    localStorage.setItem('tinkerfin.auth.session', JSON.stringify({ token: 'files-preview', tokenType: 'Bearer', serverAddress: 'http://127.0.0.1:8090', expiresAt: '2099-01-01T00:00:00Z', user }))
    localStorage.setItem('tinkerfin:theme', theme)
    const original = window.fetch
    const streams = new Map<string, Set<ReadableStreamDefaultController<Uint8Array>>>()
    window.emitWorkspaceChange = project => { for (const stream of streams.get(project) ?? []) stream.enqueue(new TextEncoder().encode('event: change\ndata: {"kind":"files_changed"}\n\n')) }
    window.fetch = async (input, init) => {
      const path = new URL(input instanceof Request ? input.url : String(input), location.href).pathname
      const match = /^\/api\/projects\/([^/]+)\/workspace\/events$/.exec(path)
      if (!match) return original(input, init)
      const request = new Request(input, init)
      const readers = streams.get(match[1]) ?? new Set<ReadableStreamDefaultController<Uint8Array>>()
      streams.set(match[1], readers)
      let release: () => void
      return new Response(new ReadableStream<Uint8Array>({
        start(reader) {
          readers.add(reader)
          const abort = () => { reader.close(); release() }
          release = () => { readers.delete(reader); request.signal.removeEventListener('abort', abort) }
          request.signal.addEventListener('abort', abort, { once: true })
          reader.enqueue(new TextEncoder().encode('event: ready\ndata: {}\n\n'))
        }, cancel() { release() },
      }), { headers: { 'Content-Type': 'text/event-stream' } })
    }
  }, { user, theme })
  const projects = [{ id: 'research', name: '市场研究', createdAt: date, updatedAt: date }, { id: 'product', name: '产品开发', createdAt: date, updatedAt: date }]
  const requests: string[] = []
  let etag = 'first'
  const source = () => etag === 'first' ? '# 市场研究\n\ndef summarize(values):\n    return sum(values) / len(values)\n\nprint(summarize([12, 18, 24]))\n' : 'print("updated")\n'
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url()), path = url.pathname
    const reply = (data: unknown) => route.fulfill({ json: { code: 0, message: 'success', data } })
    if (path === '/api/auth/me') return reply({ expires_at: '2099-01-01T00:00:00Z', user })
    if (path === '/api/projects') return reply(projects)
    if (path === '/api/models') return reply({ items: [{ modelId: 'main', displayName: 'DeepSeek', connectionId: 'test', connectionDisplayName: '测试', reasoningEnabled: false, isDefault: true }], defaultModelId: 'main' })
    if (path === '/api/skills/installations') return reply([])
    if (path === '/api/conversation/config') return reply({ dayRanges: [7, 30] })
    const workspace = /^\/api\/projects\/([^/]+)\/workspace\/(entries|file|preview)$/.exec(path)
    if (workspace) {
      requests.push(`${workspace[1]}:${workspace[2]}`)
      const selectedPath = url.searchParams.get('path') ?? '/'
      const selected = file(selectedPath, 'file', etag)
      if (workspace[2] === 'entries') return reply({ state: 'ready', path: selectedPath, nextCursor: null,
        entries: workspace[1] === 'product' ? [file('/product.txt')] : selectedPath === '/' ? [file('/scripts', 'directory'), file('/README.md'), file('/report.pdf')] : [file('/scripts/market.py', 'file', etag)] })
      if (workspace[2] === 'file') return reply(selected)
      return reply(selectedPath.endsWith('.pdf') ? { kind: 'unsupported', file: selected } : { kind: 'text', file: selected, text: source(), truncated: false })
    }
    if (path === '/api/conversation/history') return reply({ items: url.searchParams.get('projectId') === 'product' ? [] : ['one', 'two'].map((id, index) => ({
      id: index + 1, threadId: id, projectId: 'research', archived: false, title: id === 'one' ? '整理市场数据' : '补充数据来源',
      titleSource: 'user', titleGenerationStatus: 'skipped', titleSeq: 1, status: 'idle', lastRunId: 'run', lastModel: 'main', accessMode: 'full',
      messageCount: 1, toolCallCount: 1, hasPendingInterrupt: false, pendingInteractionKind: null, pinned: false, createdAt: date, updatedAt: date,
    })), nextCursor: null })
    if (/\/api\/conversation\/(one|two)\/history$/.test(path)) {
      const threadId = path.split('/')[3]
      const detail: ConversationHistoryDetail = {
        id: threadId === 'one' ? 1 : 2, threadId, projectId: 'research', archived: false, accessMode: 'full', title: threadId === 'one' ? '整理市场数据' : '补充数据来源',
        titleSource: 'user', titleGenerationStatus: 'skipped', titleSeq: 1, lastModel: 'main', pinned: false,
        asOfSeq: 1, generation: 'workspace-browser', observedAt: date, headRunId: 'run', availableHeads: ['run'], historyCursor: null,
        messageCount: 1, toolCallCount: 1, messages: [{ id: 'question', agui: { kind: 'message', messageId: 'question' }, sourceId: 'question', traceSeq: 1, graphNamespace: [], runId: 'run', role: 'user', content: '请整理市场数据，并将分析脚本保存在工作区', contentOmitted: false, status: 'completed', createdAt: date, completedAt: date }],
        reasoning: [], graph: emptyTraceGraph(1), state: { root: {}, subgraphs: {} }, interactionAvailability: [], interactions: [], runFailures: [],
        status: { execution: 'succeeded', headRunId: 'run' }, completeness: { missingPrefix: false, missingTail: false, payloadOmitted: false },
        taskTrace: { status: 'ready', todoGroups: [{ id: 'todo-group:run', userMessageId: 'question', userMessagePreview: '整理市场数据', groupToolCallId: 'tool', createdAt: date, status: 'completed', todos: [{ id: 'todo', content: '核对数据来源', status: 'completed' }] }] },
        createdAt: date, updatedAt: date,
      }
      return reply(detail)
    }
    return route.fulfill({ status: 404, json: { code: 404, message: 'Unknown test endpoint', data: null } })
  })
  await page.goto('/?project=research&thread=one')
  await expect(page.getByRole('button', { name: '任务轨迹 1', exact: true })).toBeVisible()
  return { requests, update: () => { etag = 'second' } }
}

async function selectScript(page: Page) {
  await page.getByRole('button', { name: '工作区', exact: true }).click()
  await page.getByRole('treeitem', { name: 'scripts', exact: true }).click()
  await page.getByRole('treeitem', { name: 'market.py', exact: true }).click()
  await expect(page.getByRole('region', { name: '文件内容' })).toContainText('def summarize(values)')
}

for (const theme of ['light', 'dark']) for (const width of [320, 768, 1024, 1440]) {
  test(`工作区加载提示居中且没有卡片边框 ${theme} ${width}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 })
    await prepare(page, theme)
    let release!: () => void
    const pending = new Promise<void>(resolve => { release = resolve })
    await page.route('**/workspace/entries?**', async route => {
      await pending
      await route.fulfill({ json: { code: 0, message: 'success', data: { state: 'ready', path: '/', entries: [], nextCursor: null } } })
    })
    try {
      await page.getByRole('button', { name: '工作区', exact: true }).click()
      const drawer = page.getByRole('complementary', { name: '工作区', exact: true })
      const loading = drawer.getByRole('status').filter({ hasText: /^正在加载工作区$/ })
      await expect(loading).toBeVisible()
      await expect(loading).toHaveAttribute('aria-busy', 'true')
      await expect(loading).toHaveCSS('border-top-width', '0px')
      const [status, title, footer] = await measureBounds(loading, drawer.getByRole('heading', { name: '工作区', exact: true }), drawer.getByText('只读', { exact: true }))
      expect(status.y).toBeGreaterThan(title.y + title.height)
      expect(status.y + status.height).toBeLessThan(footer.y)
      expect(status.height).toBeLessThan(footer.y - title.y - title.height - status.height)
      expect(status.x).toBeGreaterThanOrEqual(0)
      expect(status.x + status.width).toBeLessThanOrEqual(width)

      await page.screenshot({ path: testInfo.outputPath(`loading-${theme}-${width}.png`) })
    } finally {
      release()
    }
    await expect(page.getByText('工作区尚无文件', { exact: true })).toBeVisible()
  })
}

for (const [name, title] of [['工作区', '工作区'], ['任务轨迹 1', '任务轨迹']]) {
  test(`键盘打开${title}后进入抽屉，Tab 可继续操作，Escape 返回入口`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await prepare(page)
    const launcher = page.getByRole('button', { name, exact: true })
    await launcher.focus()
    await page.keyboard.press('Enter')
    const drawer = page.getByRole('complementary', { name: title, exact: true })
    await expect(drawer.getByRole('button', { name: `关闭${title}` })).toBeFocused()
    await page.keyboard.press('Tab')
    await expect(drawer.locator(':focus')).toHaveCount(1)
    await page.keyboard.press('Escape')
    await expect(launcher).toBeFocused()
    await expect(page.getByRole('tooltip')).toHaveCount(0)
  })
}

test('项目文件实时提示、同项目会话保留阅读，两个顶部入口互斥且间距与会话工具栏一致', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  const evidence = await prepare(page)
  expect(evidence.requests).toEqual([])
  const task = page.getByRole('button', { name: '任务轨迹 1', exact: true })
  const files = page.getByRole('button', { name: '工作区', exact: true })
  const [taskBox, filesBox, archive, search] = await measureBounds(task, files, page.getByRole('button', { name: '已归档会话', exact: true }), page.getByRole('button', { name: '搜索会话', exact: true }))
  expect(filesBox.x - taskBox.x - taskBox.width).toBe(search.x - archive.x - archive.width)
  expect(filesBox.width).toBe(search.width)
  await expect(task).toHaveText('1')
  const collapse = page.getByRole('button', { name: '收起侧边栏' })
  const icons = await measureBounds(task.locator('svg'), files.locator('svg'), collapse.locator('svg'))
  for (const icon of icons) {
    expect(icon.width).toBe(17)
    expect(icon.height).toBe(17)
    expect(icon.y + icon.height / 2).toBe(icons[2].y + icons[2].height / 2)
  }
  for (const action of [task, files]) {
    await expect(action).toHaveCSS('box-shadow', 'none')
    await expect(action).toHaveCSS('background-color', 'rgba(0, 0, 0, 0)')
    await action.hover()
    await expect(action).toHaveCSS('box-shadow', 'none')
    await expect(action).toHaveCSS('background-color', 'rgba(0, 0, 0, 0)')
  }
  if (process.env.TINKERFIN_VISUAL_QA_DIR) {
    await mkdir(process.env.TINKERFIN_VISUAL_QA_DIR, { recursive: true })
    await page.mouse.move(600, 400)
    await page.screenshot({ path: resolve(process.env.TINKERFIN_VISUAL_QA_DIR, 'workspace-header-controls.png') })
  }
  await selectScript(page)
  await expect(files).toBeHidden()
  await expect(page.getByText('项目文件', { exact: true })).toHaveCount(0)
  const [refreshBox, closeBox] = await measureBounds(page.getByRole('button', { name: '刷新文件' }), page.getByRole('button', { name: '关闭工作区' }))
  expect(closeBox.x - refreshBox.x - refreshBox.width).toBe(search.x - archive.x - archive.width)
  expect(refreshBox.y + refreshBox.height / 2).toBe(closeBox.y + closeBox.height / 2)
  expect(closeBox.x + closeBox.width / 2).toBe(filesBox.x + filesBox.width / 2)
  expect(closeBox.y + closeBox.height / 2).toBe(filesBox.y + filesBox.height / 2)
  const preview = page.getByRole('region', { name: '文件内容' })
  await page.evaluate(() => window.emitWorkspaceChange('research'))
  await page.clock.runFor(150)
  await expect(page.getByText('文件已变化', { exact: true })).toHaveCount(0)
  evidence.update()
  await page.evaluate(() => window.emitWorkspaceChange('research'))
  await page.clock.runFor(150)
  await expect(page.getByText('文件已变化', { exact: true })).toBeVisible()
  await expect(preview).toContainText('def summarize(values)')
  await page.getByRole('button', { name: '刷新预览' }).click()
  await expect(preview).toContainText('print("updated")')
  await page.getByRole('button', { name: '打开会话：补充数据来源' }).click()
  await expect(preview).toContainText('print("updated")')
  await task.click()
  await expect(task).toBeHidden()
  await expect(page.getByRole('complementary', { name: '工作区', exact: true })).toBeHidden()
  await expect(page.getByRole('complementary', { name: '任务轨迹', exact: true })).toBeVisible()
  const [taskClose] = await measureBounds(page.getByRole('button', { name: '关闭任务轨迹' }))
  expect(taskClose.x + taskClose.width / 2).toBe(filesBox.x + filesBox.width / 2)
  expect(taskClose.y + taskClose.height / 2).toBe(filesBox.y + filesBox.height / 2)
  await files.click()
  await expect(page.getByRole('complementary', { name: '任务轨迹', exact: true })).toBeHidden()
  await page.keyboard.press('Escape')
  await expect(files).toBeFocused()
  await expect(page.getByRole('tooltip')).toHaveCount(0)
})

test('侧栏展开挤压内容时，工作区仍以完整详情显示', async ({ page }) => {
  await page.setViewportSize({ width: 768, height: 900 })
  await prepare(page)
  await page.getByRole('button', { name: '打开侧边栏', exact: true }).click()
  await page.getByRole('button', { name: '工作区', exact: true }).click()
  const drawer = page.getByRole('complementary', { name: '工作区', exact: true })
  await expect(drawer).toBeVisible()
  await expect(drawer.getByRole('button', { name: '返回对话' })).toBeVisible()
  expect((await drawer.boundingBox())!.width).toBeGreaterThanOrEqual(300)
})

test('项目切换保持抽屉打开并清除原文件选择', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await prepare(page)
  await selectScript(page)
  await page.getByRole('button', { name: '切换项目：市场研究' }).click()
  await page.getByRole('dialog', { name: '项目', exact: true }).getByRole('button', { name: '产品开发', exact: true }).click()
  const drawer = page.getByRole('complementary', { name: '工作区', exact: true })
  await expect(drawer).toBeVisible()
  await expect(drawer.getByRole('treeitem', { name: 'product.txt' })).toBeVisible()
  await expect(drawer.getByText('选择文件查看源码')).toBeVisible()
  await expect(drawer.getByText('market.py', { exact: true })).toHaveCount(0)
})

for (const theme of ['light', 'dark']) {
  for (const width of [320, 768, 1024, 1440]) {
    test.describe(`${theme} ${width}`, () => {
      test.use({ viewport: { width, height: 900 }, hasTouch: width === 320 })
      test('目录与源码适配宽度、保留键盘返回和主题；触控目标满足44px', async ({ page }) => {
        await prepare(page, theme)
        await selectScript(page)
        const drawer = page.getByRole('complementary', { name: '工作区', exact: true })
        await expect(drawer).toBeVisible()
        expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width)
        await expect(drawer).toHaveCSS('box-shadow', 'none')
        if (width === 320) {
          const back = drawer.getByRole('button', { name: '返回文件目录' })
          await expect(back).toBeFocused()
          const box = (await back.boundingBox())!
          expect(box.width).toBeGreaterThanOrEqual(44)
          expect(box.height).toBeGreaterThanOrEqual(44)
        }
        if (process.env.TINKERFIN_VISUAL_QA_DIR) {
          await mkdir(process.env.TINKERFIN_VISUAL_QA_DIR, { recursive: true })
          await page.screenshot({ path: resolve(process.env.TINKERFIN_VISUAL_QA_DIR, `workspace-files-${theme}-${width}.png`) })
        }
        const back = drawer.getByRole('button', { name: '返回文件目录' })
        if (await back.isVisible()) {
          await back.click()
          await expect(page.getByRole('treeitem', { name: 'market.py' })).toBeFocused()
        }
        await page.keyboard.press('Escape')
        await expect(page.getByRole('button', { name: '工作区', exact: true })).toBeFocused()
        await expect(page.getByRole('tooltip')).toHaveCount(0)
      })
    })
  }
}
