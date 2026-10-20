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
const user = { user_id: 1, username: 'workspace-browser', avatar_url: null, roles: [], disabled: false }
const file = (path: string, kind: WorkspaceFile['kind'] = 'file', etag = 'first'): WorkspaceFile => ({ path, name: path.split('/').at(-1)!, kind, sizeBytes: kind === 'file' ? 84 : null, modifiedAt: date, etag })

async function prepare(page: Page, theme = 'light', directories?: Record<string, WorkspaceFile[]>) {
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
    if (path === '/api/devices') return reply([])
    if (path === '/api/devices/selection') return reply(null)
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
        entries: workspace[1] === 'product' ? [file('/product.txt')] : directories?.[selectedPath] ?? (selectedPath === '/' ? [file('/scripts', 'directory'), file('/README.md'), file('/report.pdf')] : [file('/scripts/market.py', 'file', etag)]) })
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
        reasoning: [], graph: emptyTraceGraph(1), state: { root: {}, subgraphs: {} }, submissionResult: null, planResults: [], interactionAvailability: [], interactions: [], runFailures: [],
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

async function openWorkspace(page: Page) {
  const entry = page.getByRole('button', { name: '工作区', exact: true })
  if (!await entry.isVisible()) await page.getByRole('button', { name: '打开导航', exact: true }).click()
  await entry.click()
  await expect(page).toHaveURL(/page=workspace/)
}

async function selectScript(page: Page) {
  await openWorkspace(page)
  await page.getByRole('button', { name: 'scripts', exact: true }).click()
  await page.getByRole('button', { name: 'market.py', exact: true }).click()
  await expect(page.getByRole('region', { name: '文件内容' })).toContainText('def summarize(values)')
}

for (const preview of [false, true]) for (const fail of [false, true]) {
  test(`刷新期间保留内容并提供按钮反馈 preview=${preview} fail=${fail}`, async ({ page }, testInfo) => {
    await prepare(page, fail ? 'dark' : 'light')
    if (preview) {
      await selectScript(page)
      await page.emulateMedia({ reducedMotion: 'no-preference' })
    } else {
      await openWorkspace(page)
      await expect(page.getByRole('button', { name: 'README.md', exact: true })).toBeVisible()
    }
    let release!: () => void
    const pending = new Promise<void>(resolve => { release = resolve })
    let received!: () => void
    const requested = new Promise<void>(resolve => { received = resolve })
    let reads = 0
    const endpoint = preview ? '**/workspace/file?**' : '**/workspace/entries?**'
    await page.route(endpoint, async route => {
      reads += 1
      received()
      await pending
      if (fail) await route.fulfill({ status: 503, json: { code: 503, message: 'Unavailable', data: null } })
      else await route.fallback()
    })
    const refresh = page.getByRole('button', { name: '刷新文件', exact: true })
    await refresh.click()
    await requested
    try {
      await expect(refresh).toBeDisabled()
      await expect(refresh).toHaveAttribute('aria-busy', 'true')
      await expect(refresh).toHaveCSS('opacity', '0.48')
      await refresh.evaluate(element => (element as HTMLButtonElement).click())
      expect(reads).toBe(1)
      if (preview) await expect(page.getByRole('region', { name: '文件内容' })).toContainText('def summarize(values)')
      else await expect(page.getByRole('button', { name: 'README.md', exact: true })).toBeVisible()
      await page.screenshot({ path: testInfo.outputPath(`refresh-${preview ? 'preview' : 'list'}-${fail ? 'dark' : 'light'}.png`) })
    } finally {
      release()
    }
    await expect(refresh).toBeEnabled()
    await expect(refresh).not.toHaveAttribute('aria-busy', 'true')
    await page.unroute(endpoint)
    await refresh.click()
    await expect(refresh).toBeEnabled()
    if (preview) await expect(page.getByRole('region', { name: '文件内容' })).toContainText('def summarize(values)')
    else await expect(page.getByRole('button', { name: 'README.md', exact: true })).toBeVisible()
  })
}

test('重新打开长文件在正文加载后恢复阅读位置', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await prepare(page)
  const source = Array.from({ length: 80 }, (_, index) => `print(${index})`).join('\n')
  let release: (() => void) | undefined
  let opened = false
  await page.route('**/workspace/preview?**', async route => {
    if (opened) await new Promise<void>(resolve => { release = resolve })
    opened = true
    await route.fulfill({ json: { code: 0, message: 'success', data: { kind: 'text', file: file('/scripts/market.py'), text: source, truncated: false } } })
  })
  await openWorkspace(page)
  await page.getByRole('button', { name: 'scripts', exact: true }).click()
  await page.getByRole('button', { name: 'market.py', exact: true }).click()
  const content = page.getByRole('region', { name: '文件内容', exact: true })
  await expect(content).toContainText('print(79)')
  await content.evaluate(element => { element.scrollTop = 700 })
  const before = await content.evaluate(element => element.scrollTop)
  expect(before).toBeGreaterThan(0)
  await page.getByRole('button', { name: '返回文件目录', exact: true }).click()
  await page.getByRole('button', { name: 'market.py', exact: true }).click()
  try {
    await expect(content).toContainText('正在读取文件')
    await expect.poll(() => Boolean(release)).toBe(true)
  } finally { release?.() }
  await expect(content).toContainText('print(79)')
  expect(await content.evaluate(element => element.scrollTop)).toBe(before)
})

for (const touch of [false, true]) {
  test.describe(touch ? '触控长路径' : '鼠标长路径', () => {
    test.use({ viewport: { width: touch ? 320 : 1440, height: 900 }, hasTouch: touch })
    test('路径保持一行、拖动不导航、键盘可滚动且没有滚动条或回弹', async ({ page }) => {
      const parts = ['研究资料', '全球市场分析与行业趋势研究资料目录', '科技行业重点公司与产品研究文档', '数据来源与整理以及核验记录归档', '2026年第四季度完整报告与参考资料', '各地区原始数据与分析结果', '审阅和发布归档材料']
      const directories: Record<string, WorkspaceFile[]> = {}
      let path = '/'
      for (const part of parts) {
        const next = `${path === '/' ? '' : path}/${part}`
        directories[path] = [file(next, 'directory')]
        path = next
      }
      directories[path] = [file(path + '/README.md')]
      await prepare(page, 'light', directories)
      await openWorkspace(page)
      for (const part of parts) await page.getByRole('button', { name: part, exact: true }).click()
      const trail = page.getByRole('region', { name: '文件路径', exact: true })
      const box = (await trail.boundingBox())!
      await expect(trail).toHaveCSS('scrollbar-width', 'none')
      await expect(trail).toHaveCSS('overscroll-behavior', 'none')
      await trail.focus()
      expect(await trail.evaluate(element => element.scrollWidth > element.clientWidth)).toBe(true)
      const beforeKeyboard = await trail.evaluate(element => element.scrollLeft)
      expect(beforeKeyboard).toBeGreaterThan(0)
      await page.keyboard.press('ArrowLeft')
      await page.clock.runFor(500)
      await expect.poll(() => trail.evaluate(element => element.scrollLeft)).toBeLessThan(beforeKeyboard)
      await trail.evaluate(element => element.scrollTo({ left: element.scrollWidth, behavior: 'instant' }))
      const initial = await trail.evaluate(element => element.scrollLeft)
      expect(initial).toBeGreaterThan(0)
      if (touch) {
        const client = await page.context().newCDPSession(page)
        await client.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x: box.x + 20, y: box.y + box.height / 2 }] })
        await client.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x: box.x + 100, y: box.y + box.height / 2 }] })
        await client.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] })
        await client.detach()
      } else {
        await page.mouse.move(box.x + 20, box.y + box.height / 2)
        await page.mouse.down()
        await page.mouse.move(box.x + 130, box.y + box.height / 2, { steps: 4 })
        await page.mouse.up()
      }
      await expect.poll(() => trail.evaluate(element => element.scrollLeft)).toBeLessThan(initial)
      await expect(page.getByRole('button', { name: 'README.md', exact: true })).toBeVisible()
      await expect(trail.getByText(parts.at(-1)!, { exact: true })).toHaveAttribute('aria-current', 'location')
      const children = await trail.getByRole('listitem').evaluateAll(elements => elements.map(element => { const box = element.getBoundingClientRect(); return box.y + box.height / 2 }))
      expect(new Set(children).size).toBe(1)
      await trail.evaluate(element => { element.scrollLeft = -100 })
      expect(await trail.evaluate(element => element.scrollLeft)).toBe(0)
      await trail.getByRole('button', { name: '工作区', exact: true }).click()
      await expect(trail).toHaveCount(0)
      await expect(page.getByRole('button', { name: parts[0], exact: true })).toBeVisible()
    })
  })
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
      await openWorkspace(page)
      const drawer = page.getByRole('region', { name: '工作区', exact: true })
      const loading = drawer.getByRole('status').filter({ hasText: /^正在加载工作区$/ })
      await expect(loading).toBeVisible()
      await expect(loading).toHaveAttribute('aria-busy', 'true')
      await expect(loading).toHaveCSS('border-top-width', '0px')
      const [status, title, content] = await measureBounds(loading, drawer.getByRole('heading', { name: '工作区', exact: true }), drawer.getByRole('region', { name: '文件目录' }))
      expect(status.y).toBeGreaterThan(title.y + title.height)
      expect(status.y + status.height).toBeLessThanOrEqual(content.y + content.height)
      expect(status.height).toBeLessThan(content.height)
      await expect(drawer.getByText('只读', { exact: true })).toHaveCount(0)
      expect(status.x).toBeGreaterThanOrEqual(0)
      expect(status.x + status.width).toBeLessThanOrEqual(width)

      await page.screenshot({ path: testInfo.outputPath(`loading-${theme}-${width}.png`) })
    } finally {
      release()
    }
    await expect(page.getByText('此文件夹为空', { exact: true })).toBeVisible()
  })
}

for (const [name, title] of [['任务轨迹 1', '任务轨迹']]) {
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

test('云端文件变化提示与阅读状态跨页面保留，页面不再展示抽屉操作', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  const evidence = await prepare(page)
  expect(evidence.requests).toEqual([])
  await selectScript(page)
  await expect(page.getByRole('button', { name: '关闭工作区' })).toHaveCount(0)
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
  await expect(preview).toBeHidden()
  await page.getByRole('button', { name: '任务轨迹 1', exact: true }).click()
  await expect(page.getByRole('complementary', { name: '任务轨迹', exact: true })).toBeVisible()
  await openWorkspace(page)
  await expect(page.getByRole('complementary', { name: '任务轨迹', exact: true })).toBeHidden()
  await expect(preview).toContainText('print("updated")')
  await page.getByRole('button', { name: '返回文件目录' }).focus()
  await page.keyboard.press('Escape')
  await expect(page.getByRole('button', { name: 'market.py', exact: true })).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(page.getByRole('region', { name: '工作区', exact: true })).toBeVisible()
})

test('侧栏展开时工作区保持独立页面布局', async ({ page }) => {
  await page.setViewportSize({ width: 768, height: 900 })
  await prepare(page)
  await page.getByRole('button', { name: '打开侧边栏', exact: true }).click()
  await openWorkspace(page)
  const drawer = page.getByRole('region', { name: '工作区', exact: true })
  await expect(drawer).toBeVisible()
  await expect(drawer.getByRole('heading', { name: '工作区', exact: true })).toBeVisible()
  await expect(drawer.getByRole('button', { name: '关闭工作区' })).toHaveCount(0)
  expect((await drawer.boundingBox())!.width).toBeGreaterThanOrEqual(300)
})

test('项目切换保持工作区页面并清除原文件选择', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await prepare(page)
  await selectScript(page)
  await page.getByRole('button', { name: '切换项目：市场研究' }).click()
  await page.getByRole('dialog', { name: '项目管理', exact: true }).getByRole('button', { name: '产品开发', exact: true }).click()
  const drawer = page.getByRole('region', { name: '工作区', exact: true })
  await expect(drawer).toBeVisible()
  await expect(drawer.getByRole('button', { name: 'product.txt' })).toBeVisible()
  await expect(drawer.getByRole('tab', { name: '列表视图', exact: true })).toBeVisible()
  await expect(drawer.getByText('market.py', { exact: true })).toHaveCount(0)
})

for (const theme of ['light', 'dark']) {
  for (const width of [320, 768, 1024, 1440]) {
    test.describe(`${theme} ${width}`, () => {
      test.use({ viewport: { width, height: 900 }, hasTouch: width === 320 })
      test('目录与源码适配宽度、保留键盘返回和主题；触控目标满足44px', async ({ page }) => {
        const longName = '市场研究_全球科技行业完整数据与结论_用于检验超长文件名称显示与提示_2026年第四季度'.repeat(3) + '.md'
        await prepare(page, theme, { '/': [file('/scripts', 'directory'), file('/skills', 'directory'), file('/archive', 'directory'), file('/README.md'), file('/report.csv'), file('/' + longName)] })
        await openWorkspace(page)
        const drawer = page.getByRole('region', { name: '工作区', exact: true })
        await expect(drawer).toBeVisible()
        await expect(drawer.getByRole('navigation', { name: '文件路径' })).toHaveCount(0)
        const capture = async (state: string) => {
          if (!process.env.TINKERFIN_VISUAL_QA_DIR) return
          await mkdir(process.env.TINKERFIN_VISUAL_QA_DIR, { recursive: true })
          await page.screenshot({ path: resolve(process.env.TINKERFIN_VISUAL_QA_DIR, `${state}-${theme}-${width}.png`) })
        }
        const list = drawer.getByRole('tab', { name: '列表视图', exact: true })
        const grid = drawer.getByRole('tab', { name: '图标视图', exact: true })
        await expect(list).toHaveAttribute('aria-selected', 'true')
        if (width >= 768) {
          const size = (await drawer.getByText('大小', { exact: true }).boundingBox())!
          const modified = (await drawer.getByText('修改时间', { exact: true }).boundingBox())!
          const value = (await drawer.getByRole('button', { name: 'README.md', exact: true }).getByText('84 B', { exact: true }).boundingBox())!
          expect(size.x + size.width).toBe(value.x + value.width)
          expect(modified.x - size.x - size.width).toBe(32)
        }
        for (const control of [list, grid]) {
          await expect(control).toHaveCSS('box-shadow', 'none')
          await expect(control).toHaveCSS('background-color', 'rgba(0, 0, 0, 0)')
          await control.hover()
          await expect(control).toHaveCSS('box-shadow', 'none')
          await expect(control).toHaveCSS('background-color', 'rgba(0, 0, 0, 0)')
          await control.focus()
          await expect(control).toHaveCSS('box-shadow', 'none')
          await page.mouse.down()
          await expect(control).toHaveCSS('box-shadow', 'none')
          await expect(control).toHaveCSS('background-color', 'rgba(0, 0, 0, 0)')
          await page.mouse.move(1, 450)
          await page.mouse.up()
        }
        await capture('list')
        for (const changeView of [list, grid]) {
          await changeView.click()
          await drawer.getByText('README', { exact: true }).hover()
          await expect(page.getByRole('tooltip')).toHaveCount(0)
          const filename = drawer.getByText(longName.slice(0, -3), { exact: true })
          expect(await filename.evaluate((element, gridView) => { const target = gridView ? element.parentElement! : element; return target.scrollWidth > target.clientWidth || target.scrollHeight > target.clientHeight }, changeView === grid)).toBe(true)
          await filename.hover()
          await expect(page.getByRole('tooltip')).toHaveText(longName)
          await expect(page.getByRole('tooltip')).toHaveCSS('box-shadow', 'none')
          await page.mouse.move(1, 450)
          await expect(page.getByRole('tooltip')).toHaveCount(0)
        }
        await capture('grid')
        await drawer.getByRole('button', { name: 'scripts', exact: true }).click()
        await expect(grid).toHaveAttribute('aria-selected', 'true')
        const [trail] = await measureBounds(drawer.getByRole('navigation', { name: '文件路径' }))
        expect(trail.width).toBeGreaterThan(0)
        await drawer.getByRole('button', { name: 'market.py', exact: true }).click()
        const preview = drawer.getByRole('region', { name: '文件内容' })
        await expect(preview).toContainText('def summarize(values)')
        await expect(preview).toHaveCSS('overscroll-behavior', 'none')
        await expect(preview.getByRole('button', { name: '复制内容', exact: true })).toBeVisible()
        expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width)
        await expect(drawer).toHaveCSS('box-shadow', 'none')
        if (width === 320) {
          const back = drawer.getByRole('button', { name: '返回文件目录' })
          await expect(back).toBeFocused()
          const box = (await back.boundingBox())!
          expect(box.width).toBeGreaterThanOrEqual(44)
          expect(box.height).toBeGreaterThanOrEqual(44)
        }
        await capture('preview')
        const infoButton = drawer.getByRole('button', { name: '文件信息', exact: true })
        const info = page.getByRole('complementary', { name: '文件信息', exact: true })
        if (width !== 320) {
          await infoButton.hover()
          await expect(info).toBeVisible()
          const panel = (await info.boundingBox())!
          await page.mouse.move(panel.x + panel.width / 2, panel.y + 20, { steps: 12 })
          await expect(info).toBeVisible()
          await page.mouse.move(1, 450)
          await expect(info).toBeHidden()
        }
        await infoButton.click()
        await expect(info).toBeVisible()
        await expect(info).toBeFocused()
        await expect(info).toHaveCSS('box-shadow', 'none')
        await expect(info).toHaveCSS('overscroll-behavior', 'none')
        await expect(info).toContainText('/scripts/market.py')
        const panel = (await info.boundingBox())!
        expect(panel.x).toBeGreaterThanOrEqual(0)
        expect(panel.x + panel.width).toBeLessThanOrEqual(width)
        await capture('information')
        await page.keyboard.press('Escape')
        await expect(info).toBeHidden()
        await expect(infoButton).toBeFocused()
        await expect(preview).toBeVisible()
        await infoButton.click()
        await expect(info).toBeVisible()
        await infoButton.click()
        await expect(info).toBeHidden()
        const back = drawer.getByRole('button', { name: '返回文件目录' })
        if (await back.isVisible()) {
          await back.click()
          await expect(page.getByRole('button', { name: 'market.py' })).toBeFocused()
          await expect(grid).toHaveAttribute('aria-selected', 'true')
        }
        await page.keyboard.press('Escape')
        await expect(drawer).toBeVisible()
        await expect(page).toHaveURL(/page=workspace/)
        await expect(page.getByRole('tooltip')).toHaveCount(0)
      })
    })
  }
}

test('工作区直达与历史导航始终读取云端，不依赖本地设备状态', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  const evidence = await prepare(page)
  await page.route('**/api/devices/selection?*', route => route.fulfill({ json: { code: 0, message: 'success', data: { device_id: 'offline-device', workspace_id: 'research' } } }))
  await page.goto('/?project=research&page=workspace')
  const workspace = page.getByRole('region', { name: '工作区', exact: true })
  await expect(workspace.getByRole('button', { name: 'scripts', exact: true })).toBeVisible()
  expect(evidence.requests).toContain('research:entries')
  await expect(page.getByRole('button', { name: '关闭工作区' })).toHaveCount(0)
  await page.getByRole('button', { name: '打开会话：整理市场数据' }).click()
  await expect(workspace).toBeHidden()
  await page.goBack()
  await expect(workspace).toBeVisible()
  await page.reload()
  await expect(workspace.getByRole('button', { name: 'scripts', exact: true })).toBeVisible()
  await expect(page).toHaveURL(/project=research.*page=workspace/)
})

for (const theme of ['light', 'dark']) for (const width of [320, 768, 1024, 1440]) test(`工作区与同布局页面页头对齐 ${theme} ${width}`, async ({ page }, testInfo) => {
  await page.setViewportSize({ width, height: 900 })
  await prepare(page, theme)
  await page.route('**/api/automation/**', route => route.fulfill({ json: { code: 0, message: 'success', data: { items: [], nextCursor: null } } }))
  await openWorkspace(page)
  const files = page.getByRole('region', { name: '工作区', exact: true })
  const tabs = (await files.getByRole('tablist', { name: '文件视图' }).boundingBox())!
  const readTabStyle = (element: HTMLElement | SVGElement) => {
    const style = getComputedStyle(element)
    const underline = getComputedStyle(element, '::after')
    const bounds = element.getBoundingClientRect()
    return {
      width: bounds.width, height: bounds.height,
      color: style.color, shadow: style.boxShadow,
      underline: { width: underline.width, height: underline.height, bottom: underline.bottom, color: underline.backgroundColor },
    }
  }
  const listTab = files.getByRole('tab', { name: '列表视图', exact: true })
  const fileStyle = await listTab.evaluate(readTabStyle)
  const centered = await listTab.evaluate(element => {
    const tab = element.getBoundingClientRect()
    const icon = element.querySelector('svg')!.getBoundingClientRect()
    return { x: icon.x + icon.width / 2 - tab.x - tab.width / 2, y: icon.y + icon.height / 2 - tab.y - tab.height / 2, size: icon.width }
  })
  expect(centered).toEqual({ x: 0, y: 0, size: 16 })
  await page.screenshot({ path: testInfo.outputPath(`files-tabs-${theme}-${width}.png`) })
  const menu = files.getByRole('button', { name: '打开导航', exact: true })
  const menuBox = await menu.isVisible() ? await menu.boundingBox() : null
  if (width < 768) await menu.click()
  await page.getByRole('button', { name: width < 440 ? '技能库' : '定时任务', exact: true }).click()
  const taskTabs = (await page.getByRole('tablist', { name: width < 440 ? '技能库视图' : '定时任务视图' }).boundingBox())!
  expect(tabs.x).toBe(taskTabs.x)
  expect(tabs.y + tabs.height / 2).toBe(taskTabs.y + taskTabs.height / 2)
  const reference = page.getByRole('tablist', { name: width < 440 ? '技能库视图' : '定时任务视图' }).getByRole('tab', { selected: true })
  expect(fileStyle).toEqual(await reference.evaluate(readTabStyle))
  expect(tabs.width).toBe(taskTabs.width)
  await page.screenshot({ path: testInfo.outputPath(`reference-tabs-${theme}-${width}.png`) })
  if (menuBox) {
    const taskMenu = (await page.getByRole('button', { name: '打开导航', exact: true }).boundingBox())!
    expect(taskMenu).toEqual(menuBox)
  }
})

for (const width of [320, 768, 1024, 1440]) test(`工作区列表网格与路径共用页面边距 ${width}`, async ({ page }) => {
  await page.setViewportSize({ width, height: 900 })
  await prepare(page)
  await openWorkspace(page)
  const workspace = page.getByRole('region', { name: '工作区', exact: true })
  const pageBox = (await workspace.boundingBox())!
  const tabs = (await workspace.getByRole('tablist', { name: '文件视图' }).boundingBox())!
  const gutter = await workspace.evaluate(element => parseFloat(getComputedStyle(element).getPropertyValue('--layout-page-gutter')))
  for (const view of ['列表视图', '图标视图']) {
    await workspace.getByRole('tab', { name: view, exact: true }).click()
    const entries = (await workspace.getByRole('list', { name: '当前目录文件' }).boundingBox())!
    expect(entries.x).toBe(pageBox.x + gutter)
    expect(entries.x + entries.width).toBe(pageBox.x + pageBox.width - gutter)
    if (width >= 768) expect(entries.x).toBe(tabs.x)
  }
  await workspace.getByRole('button', { name: 'scripts', exact: true }).click()
  const trail = (await workspace.getByRole('region', { name: '文件路径', exact: true }).boundingBox())!
  expect(trail.x).toBe(pageBox.x + gutter)
})

test('直接进入工作区时内容区不出现自动聚焦外框', async ({ page }) => {
  await prepare(page)
  await page.goto('/?project=research&page=workspace')
  const directory = page.getByRole('region', { name: '文件目录', exact: true })
  await expect(directory).toBeFocused()
  await expect(directory).toHaveCSS('outline-width', '0px')
  for (const key of ['Escape', 'Escape', 'Shift', 'Control', 'PageDown']) {
    await directory.press(key)
    await expect(directory).toBeFocused()
    await expect(directory).toHaveCSS('outline-width', '0px')
  }
  await directory.click({ position: { x: 20, y: 350 } })
  await directory.press('Escape')
  await expect(directory).toHaveCSS('outline-width', '0px')
  await page.getByRole('button', { name: 'scripts', exact: true }).click()
  await directory.press('Escape')
  await expect(directory).toHaveCSS('outline-width', '0px')
  await page.getByRole('button', { name: 'market.py', exact: true }).click()
  await page.getByRole('button', { name: '返回文件目录', exact: true }).press('Escape')
  const entry = page.getByRole('button', { name: 'market.py', exact: true })
  await expect(entry).toBeFocused()
  await entry.press('Escape')
  await expect(entry).toHaveCSS('outline-width', '0px')
  await entry.press('Tab')
  await page.keyboard.press('Shift+Tab')
  await expect(entry).toBeFocused()
  await expect(entry).toHaveCSS('outline-width', '2px')
})
