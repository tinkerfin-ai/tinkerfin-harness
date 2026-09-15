import { expect, test, type Page } from '@playwright/test'

type TitleHarness = {
  emit: (index: number, event: Record<string, unknown>) => void
  aborted: number[]
  cancelled: string[]
  count: () => number
}

async function setup(page: Page, theme: string) {
  await page.addInitScript(({ theme }) => {
    const user = { user_id: 1, username: 'titles', display_name: '标题验收', avatar_url: null, roles: [], disabled: false }
    localStorage.setItem('tinkerfin:theme', theme)
    localStorage.setItem('tinkerfin.auth.session', JSON.stringify({ token: 'test-token', tokenType: 'Bearer', expiresAt: '2099-01-01T00:00:00Z', user }))
    const original = window.fetch
    const streams: { controller: ReadableStreamDefaultController<Uint8Array>; threadId: string; runId: string; seq: number }[] = []
    const titles = new Map<string, { threadId: string; title: string; titleSource: string; titleGenerationStatus: string; titleSeq: number }>()
    const aborted: number[] = []
    const cancelled: string[] = []
    const emit = (index: number, event: Record<string, unknown>) => {
      const stream = streams[index]
      stream.controller.enqueue(new TextEncoder().encode(`id: ${++stream.seq}\ndata: ${JSON.stringify(event)}\n\n`))
    }
    Object.assign(window, { titleHarness: { emit, aborted, cancelled, count: () => streams.length } })
    window.fetch = async (input, init) => {
      const request = new Request(input, init)
      const url = new URL(request.url)
      if (!url.pathname.startsWith('/api/')) return original(input, init)
      const json = (data: unknown) => new Response(JSON.stringify({ code: 0, message: 'success', data }), { headers: { 'Content-Type': 'application/json' } })
      if (url.pathname === '/api/auth/me') return json({ expires_at: '2099-01-01T00:00:00Z', user })
      if (url.pathname === '/api/models') return json({ items: [{ modelId: 'main', displayName: 'Main', imageSupport: 'supported', reasoningEnabled: false, isDefault: true }], defaultModelId: 'main' })
      if (url.pathname === '/api/conversation/config') return json({ dayRanges: [7, 30] })
      if (url.pathname === '/api/conversation/history') return json({ items: [], nextCursor: null })
      if (url.pathname === '/api/conversation/chat') {
        const payload = await request.json()
        const index = streams.length
        const threadId = `title-thread-${index}`
        const title = { threadId, title: Array.from(String(payload.messages[0].content)).slice(0, 16).join(''), titleSource: 'default', titleGenerationStatus: 'idle', titleSeq: 0 }
        titles.set(threadId, title)
        return new Response(new ReadableStream<Uint8Array>({ start(controller) {
          streams.push({ controller, threadId, runId: payload.runId, seq: 0 })
          request.signal.addEventListener('abort', () => { aborted.push(index); controller.error(new DOMException('Aborted', 'AbortError')) }, { once: true })
          emit(index, { type: 'RUN_STARTED', runId: payload.runId, ...title })
        } }), { headers: { 'Content-Type': 'text/event-stream' } })
      }
      const threadId = url.pathname.split('/')[3]
      if (request.method === 'PATCH') {
        const current = titles.get(threadId)!
        const patch = await request.json()
        const title = { ...current, title: patch.title, titleSource: 'user', titleGenerationStatus: 'skipped', titleSeq: 3 }
        titles.set(threadId, title)
        return json(title)
      }
      if (url.pathname.endsWith('/cancel')) {
        cancelled.push(threadId)
        const index = streams.findIndex(stream => stream.threadId === threadId)
        emit(index, { type: 'RUN_FINISHED', threadId, runId: streams[index].runId })
        streams[index].controller.close()
        return json({ cancelled: true })
      }
      if (url.pathname.endsWith('/history')) {
        const stream = streams.find(item => item.threadId === threadId)!
        return json({ accessMode: 'full', ...titles.get(threadId), id: 1, lastModel: 'main', pinned: false, asOfSeq: 0, generation: 'test', observedAt: '2026-09-08T00:00:00.000000Z', headRunId: stream.runId, availableHeads: [stream.runId], historyCursor: null, messageCount: 0, toolCallCount: 0, messages: [], reasoning: [], runFailures: [], state: { root: {}, subgraphs: {} }, interactions: [], status: { execution: 'succeeded', headRunId: stream.runId }, completeness: { missingPrefix: false, missingTail: false, payloadOmitted: false }, createdAt: '2026-09-08T00:00:00Z', updatedAt: '2026-09-08T00:00:00Z', graph: { asOfSeq: 0, turns: [], nodes: [], orderedNodeIds: [], matchedNodeIds: [], completeness: { callTrackingMissing: false, relationshipEvidenceMissing: false, detailsOmitted: false } }, taskTrace: url.searchParams.get('includeTaskTrace') === 'true' ? { status: 'ready', todoGroups: [] } : null })
      }
      return json({})
    }
  }, { theme })
  await page.goto('/')
  await expect(page.getByRole('textbox', { name: '消息输入' })).toBeEnabled()
}

async function send(page: Page, text: string) {
  await page.getByRole('textbox', { name: '消息输入' }).fill(text)
  await page.getByRole('button', { name: '发送消息', exact: true }).click()
}

async function navigation(page: Page) {
  for (const name of ['打开导航', '打开侧边栏']) {
    const control = page.getByRole('button', { name, exact: true })
    if (await control.isVisible()) await control.click()
  }
}

async function titleEvent(page: Page, index: number, title: string, titleSeq = 2) {
  await page.evaluate(({ index, title, titleSeq }) => {
    (window as typeof window & { titleHarness: TitleHarness }).titleHarness.emit(index, { type: 'CUSTOM', name: 'studio.conversation.title.updated', value: { threadId: `title-thread-${index}`, title, titleSource: 'generated', titleGenerationStatus: 'succeeded', titleSeq } })
  }, { index, title, titleSeq })
}

for (const theme of ['light', 'dark']) {
  for (const width of [320, 768, 1024, 1440]) {
    test(`原流标题显示与手动固定 ${theme} ${width}`, async ({ page }, testInfo) => {
      await page.setViewportSize({ width, height: 1000 })
      await page.emulateMedia({ reducedMotion: 'reduce' })
      await setup(page, theme)
      await send(page, '默认标题应当截取前十六个字符用于立即展示')
      await expect.poll(() => page.evaluate(() => (window as typeof window & { titleHarness: TitleHarness }).titleHarness.count())).toBe(1)
      await titleEvent(page, 0, '自动标题')
      await expect(page).toHaveTitle(/自动标题/)
      await navigation(page)
      await expect(page.getByRole('button', { name: '打开会话：自动标题', exact: true })).toBeVisible()
      await page.getByRole('button', { name: '管理会话：自动标题', exact: true }).click()
      await page.getByRole('button', { name: '重命名', exact: true }).click()
      await page.getByRole('textbox', { name: '会话名称', exact: true }).fill('用户固定标题')
      await page.getByRole('button', { name: '保存', exact: true }).click()
      await expect(page.getByRole('dialog')).toHaveCount(0)
      await titleEvent(page, 0, '迟到自动标题')
      await expect(page).toHaveTitle(/用户固定标题/)
      await expect(page.getByRole('button', { name: '打开会话：用户固定标题', exact: true })).toBeVisible()
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
      await page.screenshot({ path: testInfo.outputPath(`title-${theme}-${width}.png`) })
    })
  }
}

test('A和B同时接收，停止B后A标题仍更新且不抢导航', async ({ page }) => {
  await setup(page, 'light')
  await send(page, '会话A')
  await expect(page).toHaveTitle(/会话A/)
  await page.locator('.new-chat').click()
  await send(page, '会话B')
  await expect(page).toHaveTitle(/会话B/)
  await titleEvent(page, 0, '后台会话A')
  await expect(page).toHaveTitle(/会话B/)
  await page.getByRole('button', { name: '停止任务', exact: true }).click()
  await expect.poll(() => page.evaluate(() => (window as typeof window & { titleHarness: TitleHarness }).titleHarness.cancelled)).toEqual(['title-thread-1'])
  await titleEvent(page, 0, 'A仍然接收', 4)
  await expect(page.getByRole('button', { name: '打开会话：A仍然接收', exact: true })).toBeVisible()
  expect(await page.evaluate(() => (window as typeof window & { titleHarness: TitleHarness }).titleHarness.aborted)).not.toContain(0)
})
