import { expect, test, type Page } from '@playwright/test'

type TextHarness = { emit: (event: Record<string, unknown>) => void; finish: () => void }

async function setup(page: Page, theme: string) {
  await page.addInitScript(theme => {
    const user = { user_id: 1, username: 'typewriter', display_name: '逐字验收', avatar_url: null, roles: [], disabled: false }
    localStorage.setItem('tinkerfin:theme', theme)
    localStorage.setItem('tinkerfin.auth.session', JSON.stringify({ token: 'test-token', tokenType: 'Bearer', expiresAt: '2099-01-01T00:00:00Z', user }))
    const original = window.fetch
    let controller: ReadableStreamDefaultController<Uint8Array>
    let runId = ''
    let seq = 0
    let content = ''
    let cancelled = false
    const emit = (event: Record<string, unknown>) => {
      if (event.type === 'TEXT_MESSAGE_CONTENT') content += event.delta
      controller.enqueue(new TextEncoder().encode(`id: ${++seq}\ndata: ${JSON.stringify(event)}\n\n`))
    }
    Object.assign(window, { textHarness: { emit, finish: () => {
      emit({ type: 'RUN_FINISHED', threadId: 'thread-typewriter', runId })
      controller.close()
    } } })
    window.fetch = async (input, init) => {
      const request = new Request(input, init)
      const path = new URL(request.url).pathname
      if (!path.startsWith('/api/')) return original(input, init)
      const json = (data: unknown) => new Response(JSON.stringify({ code: 0, message: 'success', data }), { headers: { 'Content-Type': 'application/json' } })
      if (path === '/api/auth/me') return json({ expires_at: '2099-01-01T00:00:00Z', user })
      if (path === '/api/models') return json({ items: [{ modelId: 'main', displayName: 'Main', imageSupport: 'supported', reasoningEnabled: false, isDefault: true }], defaultModelId: 'main' })
      if (path === '/api/conversation/config') return json({ dayRanges: [7, 30] })
      if (path === '/api/conversation/history') return json({ items: [], nextCursor: null })
      if (path === '/api/conversation/chat') {
        const payload = await request.json()
        runId = payload.runId
        return new Response(new ReadableStream<Uint8Array>({ start(value) {
          controller = value
          emit({ type: 'RUN_STARTED', threadId: 'thread-typewriter', runId })
        } }), { headers: { 'Content-Type': 'text/event-stream' } })
      }
      if (path.endsWith('/cancel')) {
        cancelled = true
        emit({ type: 'TEXT_MESSAGE_END', messageId: 'answer' })
        emit({ type: 'RUN_ERROR', code: 'cancelled', message: '任务已停止' })
        controller.close()
        return json({ cancelled: true })
      }
      if (path.endsWith('/history')) return json({
        accessMode: 'full', id: 1, threadId: 'thread-typewriter', title: '逐字验收', titleSource: 'default', titleGenerationStatus: 'idle', titleSeq: 0,
        lastModel: 'main', pinned: false, asOfSeq: 10, generation: 'test', observedAt: '2026-09-14T00:00:00.000000Z',
        headRunId: runId, availableHeads: [runId], historyCursor: null, messageCount: 1, toolCallCount: 0,
        messages: [{ agui: { kind: 'message', messageId: 'answer' }, id: 'trace-answer', sourceId: 'answer', traceSeq: 8, graphNamespace: [], runId, role: 'assistant', content, contentOmitted: false, status: 'completed', createdAt: '2026-09-14T00:00:00Z', completedAt: '2026-09-14T00:00:00Z' }],
        reasoning: [], runFailures: [], state: { root: {}, subgraphs: {} }, interactions: [], status: { execution: cancelled ? 'cancelled' : 'succeeded', headRunId: runId },
        completeness: { missingPrefix: false, missingTail: false, payloadOmitted: false }, createdAt: '2026-09-14T00:00:00Z', updatedAt: '2026-09-14T00:00:00Z',
        graph: { asOfSeq: 10, turns: [], nodes: [], orderedNodeIds: [], matchedNodeIds: [], completeness: { callTrackingMissing: false, relationshipEvidenceMissing: false, detailsOmitted: false } },
        taskTrace: { status: 'ready', todoGroups: [] },
      })
      return json({})
    }
  }, theme)
  await page.goto('/')
  await page.getByRole('textbox', { name: '消息输入' }).fill('请逐字回复')
  await page.getByRole('button', { name: '发送消息', exact: true }).click()
  await expect(page.getByRole('button', { name: '停止任务', exact: true })).toBeVisible()
}

for (const displayed of [4, 12]) {
  test(`已显示 ${displayed} 字后历史同步不倒退或重播`, async ({ page }) => {
    await setup(page, 'light')
    await page.clock.install()
    await page.clock.pauseAt(new Date())
    const content = '这段回复已经显示完成了呀'
    await page.evaluate(content => {
      const harness = (window as typeof window & { textHarness: TextHarness }).textHarness
      harness.emit({ type: 'TEXT_MESSAGE_START', messageId: 'answer', role: 'assistant' })
      harness.emit({ type: 'TEXT_MESSAGE_CONTENT', messageId: 'answer', delta: content })
      harness.emit({ type: 'TEXT_MESSAGE_END', messageId: 'answer' })
    }, content)
    const answer = page.locator('#answer .message-markdown')
    await expect(answer).toHaveCount(1)
    await page.clock.runFor(displayed * 16)
    await expect(answer).toHaveText(content.slice(0, displayed))
    await page.evaluate(() => (window as typeof window & { textHarness: TextHarness }).textHarness.finish())
    await expect(page).toHaveTitle(/逐字验收/)
    await expect(answer).toHaveText(content.slice(0, displayed))
    await page.clock.runFor(16)
    await expect(answer).toHaveText(content.slice(0, displayed + 1))
    await page.clock.runFor(content.length * 16)
    await expect(answer).toHaveText(content)
    await expect(page.locator('#answer')).toHaveCount(1)
  })
}

for (const displayed of [4, 12]) {
  test(`点击停止时已显示 ${displayed} 字，历史同步保持正文和位置`, async ({ page }) => {
    await setup(page, 'light')
    await page.clock.install()
    await page.clock.pauseAt(new Date())
    const content = '停止后不会清空或重播呀呀'
    await page.evaluate(({ content, displayed }) => {
      const harness = (window as typeof window & { textHarness: TextHarness }).textHarness
      harness.emit({ type: 'TEXT_MESSAGE_START', messageId: 'answer', role: 'assistant' })
      harness.emit({ type: 'TEXT_MESSAGE_CONTENT', messageId: 'answer', delta: content })
      // 模型正文结束与整个任务结束可以分开发生
      if (displayed === content.length) harness.emit({ type: 'TEXT_MESSAGE_END', messageId: 'answer' })
    }, { content, displayed })
    await page.clock.runFor(64)
    const answer = page.locator('#answer .message-markdown')
    await expect(answer).toHaveCount(1)
    await page.clock.runFor(displayed * 16)
    const shown = await answer.textContent()
    expect(shown!.length).toBeGreaterThan(0)
    const pane = page.getByRole('region', { name: '对话内容', exact: true })
    const top = await pane.evaluate(element => element.scrollTop)
    await page.getByRole('button', { name: '停止任务', exact: true }).click()
    await expect(page).toHaveTitle(/逐字验收/)
    await expect(answer).toHaveText(shown!)
    expect(await pane.evaluate(element => element.scrollTop)).toBe(top)
    await page.clock.runFor(content.length * 16)
    await expect(answer).toHaveText(content)
    await expect(page.locator('#answer')).toHaveCount(1)
    await expect(page.getByRole('button', { name: '停止任务', exact: true })).toHaveCount(0)
  })
}

for (const theme of ['light', 'dark']) {
  for (const width of [320, 768, 1024, 1440]) {
    test(`整块回复和结束后逐字显示 ${theme} ${width}`, async ({ page }, testInfo) => {
      await page.setViewportSize({ width, height: 800 })
      await page.emulateMedia({ reducedMotion: width === 320 ? 'reduce' : 'no-preference' })
      await setup(page, theme)
      await page.clock.install()
      await page.clock.pauseAt(new Date())
      const content = '逐字显示不会整段跳出。\n\n' + '这一行用于核验逐字展开后的滚动跟随。\n\n'.repeat(12)
        + '**重点内容**\n\n```python\nprint("你好")\n```\n\n|模型|结果|\n|---|---|\n|千问|正常|'
      await page.evaluate(content => {
        const harness = (window as typeof window & { textHarness: TextHarness }).textHarness
        harness.emit({ type: 'TEXT_MESSAGE_START', messageId: 'answer', role: 'assistant' })
        harness.emit({ type: 'TEXT_MESSAGE_CONTENT', messageId: 'answer', delta: content })
        harness.emit({ type: 'TEXT_MESSAGE_END', messageId: 'answer' })
        harness.finish()
      }, content)
      const answer = page.locator('#answer .message-markdown')
      await expect(answer).toHaveCount(1)
      await expect(answer).toHaveText('')
      for (let i = 1; i <= 6; i++) {
        await page.clock.runFor(16)
        await expect(answer).toHaveText(content.slice(0, i))
      }
      await expect(page.getByRole('button', { name: '复制回答', exact: true })).toHaveCount(0)
      await page.screenshot({ path: testInfo.outputPath(`partial-${theme}-${width}.png`) })
      await page.clock.runFor(content.length * 17)
      await expect(page.getByRole('button', { name: '复制回答', exact: true })).toBeVisible()
      await expect(answer.getByRole('table')).toContainText('千问正常')
      await expect(answer.locator('code')).toHaveText('print("你好")\n')
      const pane = page.getByRole('region', { name: '对话内容', exact: true })
      expect(await pane.evaluate(element => element.scrollHeight - element.scrollTop - element.clientHeight)).toBeLessThanOrEqual(2)
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
      await page.screenshot({ path: testInfo.outputPath(`complete-${theme}-${width}.png`) })
    })
  }
}
