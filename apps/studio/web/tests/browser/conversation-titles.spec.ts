import { expect, test, type Page } from '@playwright/test'

type TitleHarness = {
  emit: (index: number, event: Record<string, unknown>) => void
  title: (index: number, title: string, titleSeq: number) => void
  finish: (index: number) => void
  aborted: number[]
  cancelled: string[]
  count: () => number
  requests: string[]
}

async function setup(page: Page, theme: string, pauseClock = false) {
  await page.clock.install({ time: new Date('2026-09-20T00:00:00Z') })
  await page.addInitScript(({ theme }) => {
    const user = { user_id: 1, username: 'titles', display_name: '标题验收', avatar_url: null, roles: [], disabled: false }
    localStorage.setItem('tinkerfin:theme', theme)
    localStorage.setItem('tinkerfin.auth.session', JSON.stringify({ token: 'test-token', serverAddress: 'http://127.0.0.1:8090', tokenType: 'Bearer', expiresAt: '2099-01-01T00:00:00Z', user }))
    const original = window.fetch
    const streams: { controller: ReadableStreamDefaultController<Uint8Array>; threadId: string; runId: string; seq: number; execution: 'running' | 'succeeded' | 'cancelled'; messages: Record<string, unknown>[] }[] = []
    const titles = new Map<string, { threadId: string; title: string; titleSource: string; titleGenerationStatus: string; titleSeq: number }>()
    const requests: string[] = []
    const aborted: number[] = []
    const cancelled: string[] = []
    const emit = (index: number, event: Record<string, unknown>) => {
      const stream = streams[index]
      if (event.type === 'TEXT_MESSAGE_START') stream.messages.push({ agui: null, id: event.messageId, sourceId: event.messageId, traceSeq: stream.seq + 1, graphNamespace: [], runId: stream.runId, role: 'assistant', content: '', contentOmitted: false, status: 'streaming', createdAt: '2026-09-20T00:00:00Z', completedAt: null })
      const message = stream.messages.find(item => item.id === event.messageId)
      if (message && event.type === 'TEXT_MESSAGE_CONTENT') message.content = String(message.content) + String(event.delta)
      if (message && event.type === 'TEXT_MESSAGE_END') { message.status = 'completed'; message.completedAt = '2026-09-20T00:00:01Z' }
      if (event.type === 'RUN_FINISHED') stream.execution = 'succeeded'
      if (event.type === 'RUN_ERROR' && event.code === 'cancelled') stream.execution = 'cancelled'
      stream.controller.enqueue(new TextEncoder().encode(`id: ${++stream.seq}\ndata: ${JSON.stringify(event)}\n\n`))
    }
    Object.assign(window, { titleHarness: {
      emit, aborted, cancelled, requests, count: () => streams.length,
      title: (index: number, title: string, titleSeq: number) => {
        const threadId = `title-thread-${index}`
        titles.set(threadId, { threadId, title, titleSeq, titleSource: 'generated', titleGenerationStatus: 'succeeded' })
      },
      finish: (index: number) => {
        const stream = streams[index]
        emit(index, { type: 'RUN_FINISHED', threadId: stream.threadId, runId: stream.runId })
        stream.controller.close()
      },
    } })
    window.fetch = async (input, init) => {
      const request = new Request(input, init)
      const url = new URL(request.url)
      requests.push(url.pathname)
      if (!url.pathname.startsWith('/api/')) return original(input, init)
      const json = (data: unknown) => new Response(JSON.stringify({ code: 0, message: 'success', data }), { headers: { 'Content-Type': 'application/json' } })
      if (url.pathname === '/api/auth/me') return json({ expires_at: '2099-01-01T00:00:00Z', user })
      if (url.pathname === '/api/models') return json({ items: [{ modelId: 'main', displayName: 'Main', imageSupport: 'supported', connectionId: 'test-provider', connectionDisplayName: '测试提供方', reasoningEnabled: false, isDefault: true }], defaultModelId: 'main' })
      if (url.pathname === '/api/conversation/config') return json({ dayRanges: [7, 30] })
      if (url.pathname === '/api/conversation/history') return json({ items: [], nextCursor: null })
      if (url.pathname === '/api/conversation/chat') {
        const payload = await request.json()
        const index = streams.length
        const previous = streams.filter(stream => stream.threadId === payload.threadId).at(-1)
        const threadId = previous?.threadId ?? `title-thread-${index}`
        const title = titles.get(threadId) ?? { threadId, title: Array.from(String(payload.messages[0].content)).slice(0, 16).join(''), titleSource: 'default', titleGenerationStatus: 'running', titleSeq: 1 }
        titles.set(threadId, title)
        return new Response(new ReadableStream<Uint8Array>({ start(controller) {
          streams.push({ controller, threadId, runId: payload.runId, seq: previous?.seq ?? 0, execution: 'running', messages: [...(previous?.messages ?? []), { agui: null, id: payload.messages[0].id, sourceId: payload.messages[0].id, traceSeq: (previous?.seq ?? 0) + 1, graphNamespace: [], runId: payload.runId, role: 'user', content: payload.messages[0].content, contentOmitted: false, status: 'completed', createdAt: '2026-09-20T00:00:00Z', completedAt: '2026-09-20T00:00:00Z' }] })
          request.signal.addEventListener('abort', () => { aborted.push(index); controller.error(new DOMException('Aborted', 'AbortError')) }, { once: true })
          emit(index, { type: 'RUN_STARTED', runId: payload.runId, ...title })
        } }), { headers: { 'Content-Type': 'text/event-stream' } })
      }
      const threadId = url.pathname.split('/')[3]
      if (url.pathname.endsWith('/title')) return json(titles.get(threadId))
      if (request.method === 'PATCH') {
        const current = titles.get(threadId)!
        const patch = await request.json()
        const title = { ...current, title: patch.title, titleSource: 'user', titleGenerationStatus: 'skipped', titleSeq: 3 }
        titles.set(threadId, title)
        return json(title)
      }
      if (url.pathname.endsWith('/cancel')) {
        cancelled.push(threadId)
        const index = streams.findIndex(stream => stream.threadId === threadId && stream.runId === url.pathname.split('/')[5])
        for (const message of streams[index].messages.filter(message => message.status === 'streaming')) {
          emit(index, { type: 'TEXT_MESSAGE_END', messageId: message.id })
        }
        emit(index, { type: 'RUN_ERROR', code: 'cancelled', message: '聊天生成已取消', rawEvent: { runId: streams[index].runId } })
        streams[index].controller.close()
        return json({ cancelled: true })
      }
      if (url.pathname.endsWith('/history')) {
        const stream = streams.filter(item => item.threadId === threadId).at(-1)!
        return json({ accessMode: 'full', ...titles.get(threadId), id: 1, lastModel: 'main', pinned: false, asOfSeq: stream.seq, generation: 'test', observedAt: '2026-09-08T00:00:00.000000Z', headRunId: stream.runId, availableHeads: [stream.runId], historyCursor: null, messageCount: stream.messages.length, toolCallCount: 0, messages: stream.messages, reasoning: [], runFailures: [], state: { root: {}, subgraphs: {} }, interactions: [], status: { execution: stream.execution, headRunId: stream.runId }, completeness: { missingPrefix: false, missingTail: false, payloadOmitted: false }, createdAt: '2026-09-08T00:00:00Z', updatedAt: '2026-09-08T00:00:00Z', graph: { asOfSeq: stream.seq, turns: [], nodes: [], orderedNodeIds: [], matchedNodeIds: [], completeness: { callTrackingMissing: false, relationshipEvidenceMissing: false, detailsOmitted: false } }, taskTrace: url.searchParams.get('includeTaskTrace') === 'true' ? { status: 'ready', todoGroups: [] } : null })
      }
      return json({})
    }
  }, { theme })
  await page.goto('/')
  await expect(page.getByRole('textbox', { name: '消息输入' })).toBeEnabled()
  if (pauseClock) await page.clock.pauseAt(new Date('2026-09-20T01:00:00Z'))
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
    (window as typeof window & { titleHarness: TitleHarness }).titleHarness.title(index, title, titleSeq)
  }, { index, title, titleSeq })
  await page.clock.runFor(1000)
}

for (const theme of ['light', 'dark']) {
  for (const width of [320, 768, 1024, 1440]) {
    test(`停止后正文立即定稿，切回及下一轮不重播 ${theme} ${width}`, async ({ page }, testInfo) => {
      await page.setViewportSize({ width, height: 1000 })
      await page.emulateMedia({ reducedMotion: 'no-preference' })
      await setup(page, theme, true)
      await send(page, '停止验收')
      await expect(page).toHaveTitle(/停止验收/)
      const content = '已经收到但尚未逐字显示的完整正文，停止后应立即保留全部内容'
      await page.evaluate(content => {
        const harness = (window as typeof window & { titleHarness: TitleHarness }).titleHarness
        harness.emit(0, { type: 'TEXT_MESSAGE_START', messageId: 'stop-answer', role: 'assistant' })
        harness.emit(0, { type: 'TEXT_MESSAGE_CONTENT', messageId: 'stop-answer', delta: content })
      }, content)
      // 推进正文发布定时器，等待已接收正文挂载后再停止
      await page.clock.runFor(100)
      await expect(page.locator('#stop-answer')).toBeAttached()
      await expect(page.getByRole('status', { name: '任务仍在继续' })).toBeVisible()
      await expect(page.getByRole('button', { name: '停止任务', exact: true })).toBeEnabled()

      await page.getByRole('button', { name: '停止任务', exact: true }).click()
      await expect(page.getByText(content, { exact: true })).toBeVisible()
      await expect(page.getByRole('status', { name: '任务仍在继续' })).toHaveCount(0)
      await expect(page.getByRole('group', { name: '回答操作' })).toBeVisible()
      await expect(page.getByRole('button', { name: '停止任务', exact: true })).toHaveCount(0)
      await page.screenshot({ path: testInfo.outputPath(`stopped-${theme}-${width}.png`) })

      await page.emulateMedia({ reducedMotion: 'reduce' })
      await page.getByRole('button', { name: '关闭提示：任务已停止', exact: true }).click()
      await navigation(page)
      await page.getByRole('button', { name: '新会话', exact: true }).first().click()
      const closeNavigation = page.getByRole('button', { name: '关闭导航', exact: true })
      if (await closeNavigation.isVisible()) await closeNavigation.click()
      await expect(page.getByRole('heading', { name: '暂无消息' })).toBeVisible()
      await navigation(page)
      await page.getByRole('button', { name: '打开会话：停止验收', exact: true }).click()
      await expect(page.getByText(content, { exact: true })).toBeVisible()
      await expect(page.getByRole('button', { name: '恢复连接', exact: true })).toHaveCount(0)

      await send(page, '继续下一轮')
      await expect.poll(() => page.evaluate(() => (window as typeof window & { titleHarness: TitleHarness }).titleHarness.count())).toBe(2)
      const nextContent = '下一轮仍可正常回复，停止时结束本轮展示'
      await page.evaluate(content => {
        const harness = (window as typeof window & { titleHarness: TitleHarness }).titleHarness
        harness.emit(1, { type: 'TEXT_MESSAGE_START', messageId: 'next-answer', role: 'assistant' })
        harness.emit(1, { type: 'TEXT_MESSAGE_CONTENT', messageId: 'next-answer', delta: content })
      }, nextContent)
      await page.clock.runFor(100)
      await expect(page.locator('#next-answer')).toBeAttached()
      await expect(page.getByRole('status', { name: '任务仍在继续' })).toBeVisible()
      await expect(page.getByRole('button', { name: '停止任务', exact: true })).toBeEnabled()
      await page.getByRole('button', { name: '停止任务', exact: true }).click()
      await expect(page.getByText(nextContent, { exact: true })).toBeVisible()
      await expect(page.getByText(content, { exact: true })).toBeVisible()
      await expect(page.getByRole('status', { name: '任务仍在继续' })).toHaveCount(0)
      expect(await page.evaluate(() => (window as typeof window & { titleHarness: TitleHarness }).titleHarness.cancelled)).toEqual(['title-thread-0', 'title-thread-0'])
    })

    test(`独立标题查询与四点加载 ${theme} ${width}`, async ({ page }, testInfo) => {
      await page.setViewportSize({ width, height: 1000 })
      await page.emulateMedia({ reducedMotion: 'reduce' })
      await setup(page, theme)
      await send(page, '默认标题应当截取前十六个字符用于立即展示')
      await expect.poll(() => page.evaluate(() => (window as typeof window & { titleHarness: TitleHarness }).titleHarness.count())).toBe(1)
      await titleEvent(page, 0, '自动标题')
      await expect(page).toHaveTitle(/自动标题/)
      await navigation(page)
      await expect(page.getByRole('button', { name: '打开会话：自动标题，正在生成', exact: true })).toBeVisible()
      await page.getByRole('button', { name: '管理会话：自动标题', exact: true }).click()
      await page.getByRole('button', { name: '重命名', exact: true }).click()
      await page.getByRole('textbox', { name: '会话名称', exact: true }).fill('用户固定标题')
      await page.getByRole('button', { name: '保存', exact: true }).click()
      await expect(page.getByRole('dialog')).toHaveCount(0)
      await titleEvent(page, 0, '迟到自动标题')
      await expect(page).toHaveTitle(/用户固定标题/)
      const runningConversation = page.getByRole('button', { name: '打开会话：用户固定标题，正在生成', exact: true })
      await expect(runningConversation).toBeVisible()
      const alignment = await runningConversation.evaluate(element => {
        const indicator = element.querySelector('.conversation-loading')!.getBoundingClientRect()
        const title = element.querySelector('.conversation-title-marquee')!.getBoundingClientRect()
        return { center: indicator.x + indicator.width / 2, titleStart: title.x }
      })
      expect(alignment.center).toBeCloseTo(alignment.titleStart / 2, 1)
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
      await page.screenshot({ path: testInfo.outputPath(`title-${theme}-${width}.png`) })
    })
  }
}

test('A和B同时接收、往返切换后后台完成，停止B不影响A标题', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'no-preference' })
  await setup(page, 'light')
  await send(page, '会话A')
  await expect(page).toHaveTitle(/会话A/)
  await page.locator('.new-chat').click()
  await send(page, '会话B')
  await expect(page).toHaveTitle(/会话B/)
  await expect(page.getByRole('button', { name: '打开会话：会话A，正在生成', exact: true })).toBeVisible()
  await page.evaluate(() => {
    const harness = (window as typeof window & { titleHarness: TitleHarness }).titleHarness
    harness.emit(0, { type: 'TEXT_MESSAGE_START', messageId: 'answer-a', role: 'assistant' })
    harness.emit(0, { type: 'TEXT_MESSAGE_CONTENT', messageId: 'answer-a', delta: '后台持续接收的正文' })
  })
  await page.getByRole('button', { name: '打开会话：会话A，正在生成', exact: true }).click()
  await expect(page).toHaveTitle(/会话A/)
  const loading = page.getByRole('status', { name: '任务仍在继续' })
  await expect(loading).toBeVisible()
  await expect(page.getByRole('button', { name: '恢复连接', exact: true })).toHaveCount(0)
  const runningConversation = page.getByRole('button', { name: '打开会话：会话A，正在生成', exact: true })
  expect(await runningConversation.evaluate(element => element.getAnimations({ subtree: true }).map(animation => animation.effect?.getTiming().duration))).toEqual([1200, 1200, 1200, 1200])
  await page.getByRole('button', { name: '打开会话：会话B，正在生成', exact: true }).click()
  await page.getByRole('button', { name: '停止任务', exact: true }).click()
  await expect.poll(() => page.evaluate(() => (window as typeof window & { titleHarness: TitleHarness }).titleHarness.cancelled)).toEqual(['title-thread-1'])
  await page.evaluate(() => {
    const harness = (window as typeof window & { titleHarness: TitleHarness }).titleHarness
    harness.emit(0, { type: 'TEXT_MESSAGE_END', messageId: 'answer-a' })
    harness.finish(0)
  })
  await expect(page.getByRole('button', { name: '打开会话：会话A', exact: true })).not.toHaveAttribute('aria-busy', 'true')
  await titleEvent(page, 0, 'A完成后的标题')
  await expect(page.getByRole('button', { name: '打开会话：A完成后的标题', exact: true })).toBeVisible()
  expect(await page.evaluate(() => (window as typeof window & { titleHarness: TitleHarness }).titleHarness.aborted)).not.toContain(0)
  await expect(page).toHaveTitle(/会话B/)
  await page.getByRole('button', { name: '打开会话：A完成后的标题', exact: true }).click()
  await expect(page.getByText('后台持续接收的正文', { exact: true })).toBeVisible()
  await expect(loading).toHaveCount(0)
  await expect(page.getByRole('button', { name: '恢复连接', exact: true })).toHaveCount(0)
})
