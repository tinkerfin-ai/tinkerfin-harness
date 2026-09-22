import { expect, test, type Page } from '@playwright/test'
import { mkdir } from 'node:fs/promises'
import { resolve } from 'node:path'
import type { ConversationHistoryDetail } from '../../src/api/conversation/history'
import { traceGraphNode, traceGraphWithNodes } from '../../src/test/traceFixtures'

const threadId = 'compact-browser'
const time = '2026-09-21T00:00:00.000Z'
const timestamp = (offset: number) => new Date(Date.parse(time) + offset).toISOString()
const user = { user_id: 1, username: 'compact-test', display_name: '压缩验收', avatar_url: null, roles: [], disabled: false }
const summary = [
  '## 项目目标与约束',
  '用户希望为当前会话增加手动压缩，保留原聊天记录与报告附件，继续讨论实现细节。',
  '',
  '## 已确认的交互',
  '- 选择 `/compact` 后立即整理上下文',
  '- 草稿、附件和任务清单保留',
  '- 生成摘要与保存结果分别显示状态',
  '',
  '## 调用方式',
  '```python',
  'await runtime.compact(',
  '    thread_id=thread_id,',
  '    run_id=run_id,',
  ')',
  '```',
  '',
  '## 下一步',
  '核验异常恢复与键盘操作，然后继续处理剩余任务。',
].join('\n')
const result = (runId: string, status: 'compacted' | 'nothing_to_compact' | 'not_reduced' = 'compacted') => ({
  run_id: runId, status, summary: status === 'compacted' ? summary : null, compacted_messages: status === 'compacted' ? 2 : 0,
})
type Phase = 'done' | 'generating' | 'saving' | 'failed' | 'unconfirmed' | 'not_reduced' | 'nothing_to_compact' | 'cancelled'

function history(runId?: string, phase: Phase = 'done', messageCount = 2): ConversationHistoryDetail {
  const busy = phase === 'generating' || phase === 'saving'
  const failed = phase === 'failed' || phase === 'unconfirmed'
  const nodeStatus = busy ? 'running' : failed ? 'failed' : phase === 'cancelled' ? 'cancelled' : 'succeeded'
  const chatSeq = messageCount + 1
  const asOfSeq = runId ? chatSeq + 5 : chatSeq
  const nodes = [traceGraphNode({ id: 'chat-node', kind: 'model', runId: 'chat', startedSeq: 1 })]
  if (runId) {
    nodes.push(traceGraphNode({ id: 'compact-context', kind: 'context', contextKind: 'compaction', compactionOrigin: 'manual', name: 'Context', runId, turnId: 'compact-turn', startedSeq: chatSeq + 1, updatedSeq: asOfSeq, status: nodeStatus, completedAt: busy ? null : time }))
    nodes.push(traceGraphNode({ id: 'compact-node', kind: 'custom', contextKind: 'compaction', compactionOrigin: 'manual', parentNodeId: 'compact-context', name: 'context_compaction', runId,
      turnId: 'compact-turn', startedSeq: chatSeq + 1, updatedSeq: asOfSeq, startedAt: time,
      status: nodeStatus,
      result: phase === 'done' ? result(runId) : phase === 'not_reduced' || phase === 'nothing_to_compact' ? result(runId, phase) : phase === 'saving' || phase === 'unconfirmed' ? { status: 'saving' } : null,
      completedAt: busy ? null : time }))

  }
  return {
    accessMode: 'write_approval', id: 1, threadId, title: '项目讨论', titleSource: 'user', titleGenerationStatus: 'idle', titleSeq: 1,
    lastModel: 'main', pinned: false, asOfSeq, generation: 'compact-generation', observedAt: time,
    headRunId: runId ?? 'chat', availableHeads: [runId ?? 'chat'], historyCursor: null, messageCount, toolCallCount: 0,
    messages: Array.from({ length: messageCount }, (_, index): ConversationHistoryDetail['messages'][number] => ({
      id: `message-${index}`, agui: null, traceSeq: index + 1, runId: 'chat', graphNamespace: [], role: index % 2 ? 'assistant' : 'user',
      content: index === messageCount - 1 ? '项目目标和附件已确认，可以继续讨论实现细节'
        : index === messageCount - 2 ? '请整理项目需求，保留报告附件'
          : index % 2 ? `历史回答 ${index}` : `历史提问 ${index}`,
      contentOmitted: false, status: 'completed', createdAt: time, completedAt: time,
    })),
    reasoning: [], graph: traceGraphWithNodes(nodes, asOfSeq), state: { root: {}, subgraphs: {} }, interactions: [],
    status: { execution: nodeStatus, headRunId: runId ?? 'chat' },
    completeness: { missingPrefix: false, missingTail: false, payloadOmitted: false },
    taskTrace: { status: 'ready', todoGroups: [] }, createdAt: time, updatedAt: time, runFailures: [],
  }
}

async function openConversation(page: Page, options: { theme?: string; detail?: ConversationHistoryDetail } = {}) {
  let detail = options.detail ?? history()
  let release = () => {}
  const finish = new Promise<void>(resolve => { release = resolve })
  const requests: { path: string; payload: { runId: string; model: string } }[] = []
  let historyReads = 0
  await page.addInitScript(({ user, theme }) => {
    localStorage.setItem('tinkerfin:theme', theme)
    localStorage.setItem('tinkerfin:language', 'zh-CN')
    localStorage.setItem('tinkerfin.auth.session', JSON.stringify({ token: 'test-only', serverAddress: 'http://127.0.0.1:8090', tokenType: 'Bearer', expiresAt: '2099-01-01T00:00:00Z', user }))
  }, { user, theme: options.theme ?? 'light' })
  await page.route('**/{api,objects}/**', async route => {
    const url = new URL(route.request().url())
    const path = url.pathname
    let data: unknown = {}
    if (path.endsWith('/compact') || path === '/api/conversation/chat') {
      const payload = route.request().postDataJSON()
      requests.push({ path, payload })
      await finish
      // 聊天只核验提交行为，测试结束后中止挂起的请求
      if (path === '/api/conversation/chat') { await route.abort(); return }
      detail = history(payload.runId, 'done', detail.messageCount)
      const events = [
        { type: 'RUN_STARTED', threadId, runId: payload.runId },
        { type: 'STATE_SNAPSHOT', snapshot: { context_compaction: result(payload.runId) } },
        { type: 'RUN_FINISHED', threadId, runId: payload.runId },
      ]
      await route.fulfill({ contentType: 'text/event-stream', body: events.map((event, index) => `id: ${index + 1}\ndata: ${JSON.stringify(event)}\n\n`).join('') })
      return
    }
    if (path === '/api/auth/me') data = { user, expires_at: '2099-01-01T00:00:00Z' }
    else if (path === '/api/models') data = { items: [{ modelId: 'main', displayName: 'Main', imageSupport: 'supported', connectionId: 'test-provider', connectionDisplayName: '测试提供方', reasoningEnabled: false, isDefault: true }], defaultModelId: 'main' }
    else if (path === '/api/conversation/config') data = { dayRanges: [7, 30] }
    else if (path === '/api/conversation/history') data = { items: [{ ...detail, status: detail.status.execution === 'running' ? 'running' : 'idle', lastRunId: detail.headRunId, hasPendingInterrupt: false, pendingInteractionKind: null }], nextCursor: null }
    else if (path.endsWith('/history')) {
      historyReads += 1
      data = { ...detail, taskTrace: url.searchParams.get('includeTaskTrace') === 'false' ? null : detail.taskTrace }
    } else if (path.endsWith('/trace/graph')) {
      data = { ...(url.searchParams.has('modelCallId') ? traceGraphWithNodes([], detail.asOfSeq) : detail.graph), nextCursor: null }
    } else if (path.endsWith('/follow')) {
      await finish
      await route.fulfill({ contentType: 'text/event-stream', body: '' })
      return
    } else if (path === '/api/attachments/uploads') data = { attachment_id: 'draft-document', url: new URL('/objects/upload', url).href, fields: {}, expires_in: 600 }
    else if (path === '/objects/upload') { await route.fulfill({ status: 204 }); return }
    else if (path.endsWith('/complete')) data = { id: 'draft-document', name: '草稿.pdf', mime_type: 'application/pdf', size_bytes: 3 }
    await route.fulfill({ json: { code: 0, message: 'success', data } })
  })
  await page.goto('/?thread=' + threadId)
  await expect(page.getByText('项目目标和附件已确认，可以继续讨论实现细节', { exact: true })).toBeVisible()
  return { requests, release, setDetail: (value: ConversationHistoryDetail) => { detail = value }, historyReads: () => historyReads }
}

const conversationPane = (page: Page) => page.getByRole('region', { name: '对话内容', exact: true })
const latestReply = (page: Page) => conversationPane(page).getByText('项目目标和附件已确认，可以继续讨论实现细节', { exact: true })

async function readEarlierHistory(page: Page, position: '上翻' | '目录') {
  await page.emulateMedia({ reducedMotion: 'reduce' })
  if (position === '目录') {
    await page.getByRole('button', { name: '对话目录', exact: true }).click()
    await page.getByRole('dialog', { name: '对话目录', exact: true })
      .getByRole('button', { name: '历史提问 0 历史回答 1', exact: true }).click()
    await expect(conversationPane(page).getByText('历史提问 0', { exact: true })).toBeInViewport()
    await expect(latestReply(page)).not.toBeAttached()
  } else {
    await expect(latestReply(page)).toBeInViewport()
    await conversationPane(page).dispatchEvent('wheel', { deltaY: -600 })
    await conversationPane(page).evaluate(element => {
      element.scrollTop = Math.floor((element.scrollHeight - element.clientHeight) / 3)
    })
    await expect(latestReply(page)).not.toBeInViewport()
  }
  await expect(page.getByRole('button', { name: '回到底部', exact: true })).toBeVisible()
}

for (const [gesture, position] of [['click', '目录'], ['Enter', '上翻'], ['Tab', '上翻'], ['send', '目录']] as const) {
  test(`压缩 ${gesture} 从${position}历史回到最新，只执行一次并保留新草稿与附件`, async ({ page }) => {
    const setup = await openConversation(page, { detail: history(undefined, 'done', 120) })
    try {
      await readEarlierHistory(page, position)
      const input = page.getByRole('textbox', { name: '消息输入' })
      await page.locator('input[type=file]').setInputFiles({ name: '草稿.pdf', mimeType: 'application/pdf', buffer: Buffer.from('pdf') })
      const attachment = page.getByRole('group', { name: '草稿.pdf', exact: true })
      await expect(attachment).not.toHaveAttribute('aria-busy')
      if (gesture === 'click') {
        await input.fill('保留原草稿')
        await page.getByRole('button', { name: '打开命令和技能', exact: true }).click()
        await page.getByRole('option', { name: /compact/ }).click()
        await expect(input).toHaveValue('保留原草稿')
      } else if (gesture === 'send') {
        await input.fill('/compact ')
        await page.getByRole('button', { name: '发送消息', exact: true }).click()
        await expect(input).toHaveValue('')
      } else {
        const draft = '/compact 后续草稿'
        await input.fill(draft)
        for (let position = draft.length; position > 5; position--) await input.press('ArrowLeft')
        await expect(page.getByRole('option', { name: /compact/ })).toBeVisible()
        await input.press(gesture)
        await expect(input).toHaveValue(' 后续草稿')
      }
      await expect(page.getByRole('region', { name: '正在整理上下文' })).toBeInViewport()
      if (gesture !== 'send') await expect(input).toBeFocused()
      await expect(page.getByText('正在整理上下文', { exact: true })).toBeVisible()
      await expect(page.getByText('compact_conversation', { exact: true })).toBeVisible()
      await expect(page.getByRole('button', { name: '停止任务', exact: true })).toBeEnabled()
      await input.fill('压缩期间编辑的新草稿')
      await expect.poll(() => setup.requests.length).toBe(1)
      expect(setup.requests[0].path).toBe(`/api/conversation/${threadId}/compact`)
      expect(Object.keys(setup.requests[0].payload).sort()).toEqual(['model', 'runId'])
      setup.release()
      const disclosure = page.locator('summary').filter({ hasText: '已压缩 2 条历史消息' })
      await expect(disclosure).toBeInViewport()
      await expect(page.getByRole('heading', { name: '项目目标与约束' })).toBeHidden()
      await expect(input).toHaveValue('压缩期间编辑的新草稿')
      await expect(attachment).toBeVisible()
      await disclosure.click()
      await expect(page.getByRole('heading', { name: '项目目标与约束' })).toBeVisible()
      expect(setup.requests).toHaveLength(1)
      await page.reload()
      await expect(disclosure).toBeVisible()
      await expect(page.getByText('请整理项目需求，保留报告附件', { exact: true })).toBeVisible()
      expect(setup.requests).toHaveLength(1)
    } finally { setup.release() }
  })
}

test('压缩期间主动上翻后，完成时保留阅读位置与草稿', async ({ page }) => {
  const setup = await openConversation(page, { detail: history(undefined, 'done', 120) })
  try {
    const input = page.getByRole('textbox', { name: '消息输入' })
    await input.fill('/compact')
    await input.press('Enter')
    await expect(page.getByRole('region', { name: '正在整理上下文' })).toBeInViewport()
    await expect.poll(() => setup.requests.length).toBe(1)
    await readEarlierHistory(page, '上翻')
    const scrollTop = await conversationPane(page).evaluate(element => element.scrollTop)
    await input.fill('继续编辑的草稿')
    setup.release()
    const disclosure = page.locator('summary').filter({ hasText: '已压缩 2 条历史消息' })
    await expect(disclosure).toBeAttached()
    await expect(disclosure).not.toBeInViewport()
    expect(await conversationPane(page).evaluate(element => element.scrollTop)).toBe(scrollTop)
    await expect(input).toHaveValue('继续编辑的草稿')
    await expect(input).toBeFocused()
  } finally { setup.release() }
})

for (const position of ['上翻', '目录'] as const) {
  test(`Plan 从${position}历史选中命令时保留位置，提交正文后回到最新`, async ({ page }) => {
    const setup = await openConversation(page, { detail: history(undefined, 'done', 120) })
    try {
      await readEarlierHistory(page, position)
      const scrollTop = await conversationPane(page).evaluate(element => element.scrollTop)
      const input = page.getByRole('textbox', { name: '消息输入' })
      if (position === '上翻') {
        await input.fill('/plan')
        await input.press('Enter')
        await expect(input).toHaveValue('/plan ')
        await expect(page.getByRole('button', { name: '发送消息', exact: true })).toBeDisabled()
      } else {
        await input.fill('制定执行方案')
        await page.getByRole('button', { name: '打开命令和技能', exact: true }).click()
        await page.getByRole('option', { name: /plan 进入 Plan 模式/ }).click()
        await expect(input).toHaveValue('/plan 制定执行方案')
      }
      expect(setup.requests).toHaveLength(0)
      expect(await conversationPane(page).evaluate(element => element.scrollTop)).toBe(scrollTop)
      await expect(latestReply(page)).not.toBeInViewport()
      await expect(input).toBeFocused()
      await input.fill('/plan 制定执行方案')
      await input.press('Enter')
      await expect.poll(() => setup.requests.length).toBe(1)
      expect(setup.requests[0]).toMatchObject({
        path: '/api/conversation/chat',
        payload: { messages: [{ role: 'user', content: '制定执行方案' }], forwardedProps: { command: { plan: 'on' } } },
      })
      await expect(conversationPane(page).getByText('制定执行方案', { exact: true })).toBeInViewport()
      await expect(page.getByRole('button', { name: 'Plan 已开启，点击关闭' })).toBeDisabled()
      await expect(input).toHaveValue('')
      await expect(input).toBeFocused()
    } finally { setup.release() }
  })
}

test('键盘关闭 Plan 后返回输入框并保留草稿', async ({ page }) => {
  const detail = history()
  detail.state.root = { tinkerfin_plan: { effectiveMode: 'plan' } }
  const setup = await openConversation(page, { detail })
  try {
    const input = page.getByRole('textbox', { name: '消息输入' })
    await input.fill('保留待发送的正文')
    const plan = page.getByRole('button', { name: 'Plan 已开启，点击关闭' })
    await plan.focus()
    await plan.press('Enter')
    await expect(plan).not.toBeAttached()
    await expect(input).toHaveValue('保留待发送的正文')
    await expect(input).toBeFocused()
    await input.press('Enter')
    await expect.poll(() => setup.requests.length).toBe(1)
    expect(setup.requests[0]).toMatchObject({
      path: '/api/conversation/chat',
      payload: { messages: [{ role: 'user', content: '保留待发送的正文' }], forwardedProps: { command: { plan: 'off' } } },
    })
  } finally { setup.release() }
})

test('刷新后保存阶段禁用停止，未确认的结果可重新加载', async ({ page }) => {
  const setup = await openConversation(page, { detail: history('compact', 'saving') })
  try {
    await expect(page.getByRole('button', { name: '正在保存压缩结果', exact: true })).toBeDisabled()
    setup.setDetail(history('compact', 'unconfirmed'))
    await page.reload()
    await expect(page.getByRole('region', { name: '压缩结果尚未确认' })).toBeVisible()
    const reads = setup.historyReads()
    setup.setDetail({ ...history('compact'), asOfSeq: 9, graph: { ...history('compact').graph, asOfSeq: 9 } })
    await page.getByRole('button', { name: '重新加载', exact: true }).click()
    await expect(page.locator('summary').filter({ hasText: '已压缩 2 条历史消息' })).toBeVisible()
    expect(setup.historyReads()).toBeGreaterThan(reads)
    expect(setup.requests).toHaveLength(0)
  } finally { setup.release() }
})

for (const theme of ['light', 'dark']) for (const width of [320, 768, 1024, 1440]) {
  test(`压缩操作各状态视觉 ${theme} ${width}`, async ({ page }) => {
    await page.setViewportSize({ width, height: 960 })
    await page.emulateMedia({ reducedMotion: 'reduce' })
    const setup = await openConversation(page, { theme, detail: history('compact') })
    try {
      const disclosure = page.locator('summary').filter({ hasText: '已压缩 2 条历史消息' })
      await expect(disclosure).toBeVisible()
      await disclosure.focus()
      await expect(disclosure).toBeFocused()
      await disclosure.press('Enter')
      await expect(page.getByRole('heading', { name: '项目目标与约束' })).toBeVisible()
      const bounds = (await disclosure.boundingBox())!
      expect(bounds.height).toBeGreaterThanOrEqual(44)
      expect(bounds.x).toBeGreaterThanOrEqual(0)
      expect(bounds.x + bounds.width).toBeLessThanOrEqual(width)
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
      const directory = resolve(process.cwd(), '../../../.impeccable/review/context-details')
      await mkdir(directory, { recursive: true })
      const operation = page.locator('[id="compaction:compact"]')
      if (width === 320) await operation.screenshot({ path: resolve(directory, `focus-${theme}-${width}.png`) })
      await disclosure.evaluate(element => element.blur())
      await operation.screenshot({ path: resolve(directory, `summary-${theme}-${width}.png`) })
      if (width === 320 || width === 1440) await page.screenshot({ path: resolve(directory, `page-${theme}-${width}.png`) })
      const phases: [Phase, string][] = [
        ['generating', '正在整理上下文'],
        ['saving', '正在保存压缩结果'],
        ['not_reduced', '上下文未缩短，已保留原内容'],
        ['nothing_to_compact', '暂无可压缩的历史'],
        ['cancelled', '压缩已停止'],
        ['failed', '压缩未完成'],
        ['unconfirmed', '压缩结果尚未确认'],
      ]
      for (const [phase, label] of phases) {
        setup.setDetail(history('compact', phase))
        await page.reload()
        const region = page.getByRole('region', { name: label, exact: true })
        await expect(region).toBeVisible()
        await expect(region.getByText('compact_conversation', { exact: true })).toBeVisible()
        await expect(region.getByText(label, { exact: true })).toBeVisible()
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
        await region.screenshot({ path: resolve(directory, `${phase}-${theme}-${width}.png`) })
      }
    } finally { setup.release() }
  })

  test(`压缩链路说明 ${theme} ${width}`, async ({ page }) => {
    await page.setViewportSize({ width, height: 960 })
    await page.emulateMedia({ reducedMotion: 'reduce' })
    const detail = history('compact', 'not_reduced')
    detail.graph = traceGraphWithNodes([
      ...detail.messages.map(message => traceGraphNode({ id: message.id, runId: 'chat', turnId: 'chat-turn',
        kind: message.role === 'user' ? 'human_message' : 'assistant_message', content: message.content, startedSeq: message.traceSeq,
        startedAt: time, completedAt: time })),
      ...detail.graph.nodes.filter(node => node.runId === 'compact').map(node => ({ ...node, startedAt: timestamp(1000), completedAt: timestamp(17200), ...(node.kind === 'custom' ? { request: { origin: 'manual', messages: [{ content: '项目目标与附件已确认，下一步实现压缩入口' }] }, result: { ...result('compact', 'not_reduced'), generated_summary: '## 项目目标与约束\n保留聊天历史及附件，使用当前模型生成摘要。手动压缩和工具共用保留规则。' } } : {}) })),
      traceGraphNode({ id: 'summary-model', kind: 'model', parentNodeId: 'compact-node', name: 'qwen3.8-max', runId: 'compact', turnId: 'compact-turn', startedSeq: 6,
        startedAt: timestamp(1216), completedAt: timestamp(17167),
        request: { messages: [{ messageType: 'human', content: '请将早期对话整理为便于后续使用的摘要' }] } }),
    ], detail.asOfSeq)
    const setup = await openConversation(page, { theme, detail })
    try {
      await page.getByRole('tab', { name: '链路', exact: true }).click()
      await expect(page.getByRole('button', { name: '压缩，压缩上下文，已完成，查看详情', exact: true })).not.toBeVisible()
      await expect(page.getByRole('button', { name: '上下文，压缩，已完成，查看详情', exact: true })).toBeVisible()
      const typography = await page.evaluate(() => {
        const compact = document.querySelector('[data-trace-node-id="compact-context"] .chain-trace-node-content')
        const ordinary = document.querySelector('[data-trace-node-id="message-0"] .chain-trace-node-content')
        const textStyle = (element: Element | null) => {
          if (!element) return null
          const style = getComputedStyle(element)
          return { color: style.color, fontSize: style.fontSize, fontWeight: style.fontWeight }
        }
        return { compact: textStyle(compact), ordinary: textStyle(ordinary), text: compact?.textContent }
      })
      expect(typography.text).toBe('压缩')
      expect(typography.compact).not.toBeNull()
      expect(typography.compact).toEqual(typography.ordinary)
      await expect(page.getByRole('button', { name: '模型，qwen3.8-max，已完成，查看详情', exact: true })).not.toBeVisible()
      if (width === 1024) await page.getByRole('button', { name: '收起侧边栏', exact: true }).click()
      await page.getByRole('button', { name: '上下文，压缩，已完成，查看详情', exact: true }).click()
      if (width === 768) {
        await expect(page.getByText('展开窗口后可查看', { exact: true })).toBeVisible()
      } else {
        await expect(page.getByText('生成的摘要', { exact: true })).not.toBeVisible()
        await expect(page.getByText('摘要模型与用量', { exact: true })).not.toBeVisible()
        await expect(page.getByRole('tab', { name: '压缩内容', exact: true })).toBeVisible()
        await expect(page.getByRole('tab', { name: '请求', exact: true })).not.toBeVisible()
        await expect(page.getByRole('tab', { name: '结果', exact: true })).not.toBeVisible()
        await page.getByRole('tab', { name: '摘要', exact: true }).click()
        await expect(page.getByRole('heading', { name: '项目目标与约束', exact: true })).toBeVisible()
      }
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
      const directory = resolve(process.cwd(), '../../../.impeccable/review/context-details')
      await mkdir(directory, { recursive: true })
      await page.screenshot({ path: resolve(directory, `trace-${theme}-${width}.png`) })
    } finally { setup.release() }
  })
}
