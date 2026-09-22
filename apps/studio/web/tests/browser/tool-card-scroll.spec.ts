import { expect, test, type Locator, type Page } from '@playwright/test'

import type { ConversationHistoryDetail } from '../../src/api/conversation/history'
import type { ConversationAgUiEvent, RawEventContext } from '../../src/api/conversation/types'
import { emptyTraceGraph, traceGraphNode, traceGraphWithNodes } from '../../src/test/traceFixtures'
import { installLiveRun } from './fixtures/liveRun'

const THREAD = 'tool-scroll-thread'
const RUN = 'tool-scroll-run'
const SUB_RUN = 'subagent-11111111-1111-5111-8111-111111111111'
const time = '2026-09-20T00:00:00.000Z'
const user = { user_id: 1, username: 'scroll-reader', display_name: '滚动验收', avatar_url: null, roles: [], disabled: false }
const snapshot: ConversationHistoryDetail = {
  id: 1, threadId: THREAD, title: '工具流式阅读', accessMode: 'full', lastModel: 'test-model',
  titleSource: 'default', titleGenerationStatus: 'idle', titleSeq: 0, pinned: false,
  asOfSeq: 1, generation: 'scroll-generation', observedAt: time, headRunId: RUN,
  availableHeads: [RUN], runFailures: [], historyCursor: null, messageCount: 1, toolCallCount: 0,
  messages: [{
    id: 'scroll-user', agui: { kind: 'message', messageId: 'scroll-user' }, traceSeq: 1,
    graphNamespace: [], runId: RUN, role: 'user', content: '检查工具输入与研究结果',
    contentOmitted: false, status: 'completed', createdAt: time, completedAt: time,
  }],
  reasoning: [], graph: emptyTraceGraph(1), state: { root: {}, subgraphs: {} }, interactions: [],
  status: { execution: 'running', headRunId: RUN },
  completeness: { missingPrefix: false, missingTail: false, payloadOmitted: false },
  taskTrace: { status: 'ready', todoGroups: [] }, createdAt: time, updatedAt: time,
}

const params = (label: string) => Array.from({ length: 18 }, (_, i) => `  "${label}${i}": "逐项核对流式内容和滚动位置",`).join('\n')
const prose = (label: string) => Array.from({ length: 12 }, (_, i) => `${label} ${i + 1}：检查输入、工具执行与输出，保留阅读位置并展示新增内容。`).join('\n\n')
const subSource: RawEventContext = {
  streamMode: 'messages', runId: RUN,
  source: {
    kind: 'deep_agent_subagent', agentType: 'subagent', agentName: 'researcher',
    graphNamespace: ['tools:research'], parentGraphNamespace: [], graphTaskId: 'research',
    parentToolCallId: 'task-call', subagentInvocationId: SUB_RUN, subagentInput: '检查资料并总结',
  },
}
const text = (delta: string): ConversationAgUiEvent => ({ type: 'TEXT_MESSAGE_CONTENT', messageId: 'research-text', delta, rawEvent: subSource })
const startSubagent: ConversationAgUiEvent[] = [
  { type: 'TOOL_CALL_START', toolCallId: 'task-call', toolCallName: 'task' },
  { type: 'TOOL_CALL_ARGS', toolCallId: 'task-call', delta: JSON.stringify({ description: '检查资料并总结', subagent_type: 'researcher' }) },
  {
    type: 'RAW', source: 'langgraph.tasks', rawEvent: { type: 'tasks', phase: 'start', ns: [] },
    event: {
      data: { id: 'research', name: 'tools' },
      provenance: {
        kind: 'root', graphNamespace: [], agentType: 'main', agentName: 'main',
        subagents: [{
          schema: 'tinkerfin.subagent-provenance', subagentInvocationId: SUB_RUN,
          graphNamespace: ['tools:research'], parentGraphNamespace: [], graphTaskId: 'research',
          agentName: 'researcher', parentToolCallId: 'task-call', description: '检查资料并总结', requestRunId: RUN,
        }],
      },
    },
  },
  { type: 'TEXT_MESSAGE_START', messageId: 'research-text', role: 'assistant', rawEvent: subSource },
]

async function openStudio(page: Page, theme = 'light', reducedMotion: 'reduce' | 'no-preference' = 'reduce', initial = snapshot) {
  let history = initial
  await page.emulateMedia({ reducedMotion })
  await page.addInitScript(({ user, theme }) => {
    localStorage.setItem('tinkerfin.auth.session', JSON.stringify({ token: 'scroll-token', serverAddress: 'http://127.0.0.1:8090', tokenType: 'Bearer', expiresAt: '2099-01-01T00:00:00Z', user }))
    localStorage.setItem('tinkerfin:theme', theme)
    localStorage.setItem('tinkerfin:language', 'zh-CN')
  }, { user, theme })
  await page.route('**/api/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    let data: unknown
    if (path === '/api/auth/me') data = { expires_at: '2099-01-01T00:00:00Z', user }
    else if (path === '/api/models') data = { defaultModelId: 'test-model', items: [{ modelId: 'test-model', displayName: 'Test model', connectionId: 'test', connectionDisplayName: '测试', isDefault: true, reasoningEnabled: false }] }
    else if (path === '/api/conversation/config') data = { dayRanges: [7, 30] }
    else if (path === '/api/conversation/history') data = { items: [{ ...history, status: history.status.execution === 'running' ? 'running' : 'idle', lastRunId: RUN, hasPendingInterrupt: false, pendingInteractionKind: null }], nextCursor: null }
    else if (path === `/api/conversation/${THREAD}/history`) data = history
    else if (path === `/api/conversation/${THREAD}/trace`) {
      await route.fulfill({ contentType: 'text/event-stream', body: `event: trace\ndata: ${JSON.stringify({ type: 'snapshot', snapshot: history })}\n\n` })
      return
    } else {
      await route.fulfill({ status: 404, json: {} })
      return
    }
    await route.fulfill({ json: { code: 0, message: 'success', data } })
  })
  const run = await installLiveRun(page, initial)
  await page.goto(`/?thread=${THREAD}`)
  await expect(page.getByText('检查工具输入与研究结果', { exact: true })).toBeVisible()
  return { ...run, setHistory: (value: ConversationHistoryDetail) => { history = value } }
}

const completedHistory = (nodes: Parameters<typeof traceGraphWithNodes>[0], execution: 'succeeded' | 'failed' | 'cancelled' = 'succeeded'): ConversationHistoryDetail => ({
  ...snapshot, asOfSeq: 100, observedAt: '2026-09-20T00:01:00.000Z',
  graph: traceGraphWithNodes(nodes, 100), status: { execution, headRunId: RUN }, toolCallCount: nodes.length,
})

const historicalTool = (id: string, name: string, request: string, result: string) => traceGraphNode({
  id: `trace-${id}`, agui: { kind: 'tool', toolCallId: id }, name, runId: RUN,
  startedSeq: 2, updatedSeq: 100, request, result,
})

const historicalSubagent = (result: string, status: 'succeeded' | 'failed' | 'cancelled' = 'succeeded') => traceGraphNode({
  id: 'trace-subagent', agui: { kind: 'subagent', parentToolCallId: 'task-call', subagentInvocationId: SUB_RUN },
  kind: 'subagent', name: 'researcher', agentName: 'researcher', runId: RUN, sourceId: SUB_RUN,
  graphNamespace: ['tools:research'], startedSeq: 3, updatedSeq: 100,
  request: '检查资料并总结', result, status,
})

const position = (region: Locator) => region.evaluate((element) => element.scrollTop)
async function atBottom(region: Locator) {
  await expect(region).toBeVisible()
  await expect.poll(() => region.evaluate((element) => element.scrollHeight - element.clientHeight)).toBeGreaterThan(0)
  await expect.poll(() => region.evaluate((element) => Math.abs(element.scrollHeight - element.clientHeight - element.scrollTop))).toBeLessThanOrEqual(2)
}

async function wheelUp(page: Page, region: Locator) {
  await region.hover()
  const scroll = await region.evaluateHandle((element) => ({
    ended: new Promise<void>((resolve) => element.addEventListener('scrollend', () => resolve(), { once: true })),
  }))
  try {
    await page.mouse.wheel(0, -100)
    await scroll.evaluate(async ({ ended }) => { await ended })
  } finally { await scroll.dispose() }
}

test('工具参数跟随、暂停、折叠和批次增长；完整结果从顶部阅读', async ({ page }) => {
  const run = await openStudio(page)
  await run.emit(
    { type: 'TOOL_CALL_START', toolCallId: 'write-call', toolCallName: 'write_file', parentMessageId: 'batch-message' },
    { type: 'TOOL_CALL_ARGS', toolCallId: 'write-call', delta: `{\n${params('初始参数')}` },
  )
  await page.getByText('Write', { exact: true }).click()
  const input = page.getByRole('region', { name: '输入', exact: true }).first()
  await atBottom(input)
  await run.emit({ type: 'TOOL_CALL_ARGS', toolCallId: 'write-call', delta: `\n${params('追加参数')}` })
  await expect(input).toContainText('追加参数17')
  await atBottom(input)
  await wheelUp(page, input)
  const paused = await position(input)
  await run.emit({ type: 'TOOL_CALL_ARGS', toolCallId: 'write-call', delta: `\n${params('暂停期间')}` })
  await expect(input).toContainText('暂停期间17')
  expect(await position(input)).toBe(paused)

  await run.emit({ type: 'TOOL_CALL_START', toolCallId: 'list-call', toolCallName: 'ls', parentMessageId: 'batch-message' })
  await expect(page.getByText('List', { exact: true })).toBeVisible()
  await expect(input).toBeVisible()
  expect(await position(input)).toBe(paused)
  await page.getByText('Write', { exact: true }).click()
  await run.emit({ type: 'TOOL_CALL_ARGS', toolCallId: 'write-call', delta: `\n${params('折叠期间')}` })
  await page.getByText('Write', { exact: true }).click()
  await expect(input).toContainText('折叠期间17')
  expect(await position(input)).toBe(paused)
  await input.press('End')
  await atBottom(input)
  run.setHistory(completedHistory([
    historicalTool('write-call', 'write_file', `{\n${params('初始参数')}\n${params('追加参数')}\n${params('暂停期间')}\n${params('折叠期间')}\n"结束参数": "已收到"\n}`, prose('完整结果')),
    historicalTool('list-call', 'ls', '', '完成'),
  ]))
  await run.emit(
    { type: 'TOOL_CALL_ARGS', toolCallId: 'write-call', delta: '\n"结束参数": "已收到"\n}' },
    { type: 'TOOL_CALL_RESULT', toolCallId: 'write-call', messageId: 'write-result', role: 'tool', content: prose('完整结果') },
    { type: 'RUN_FINISHED', threadId: THREAD, runId: RUN },
  )
  await expect(input).toContainText('结束参数')
  await atBottom(input)
  const result = page.getByRole('region', { name: '输出', exact: true }).first()
  await expect(result).toContainText('完整结果 12')
  expect(await position(result)).toBe(0)
  await run.finish()
})

test('子 Agent 正文与嵌套参数各自跟随，Markdown、窄屏和图片加载后保持最新', async ({ page }) => {
  const run = await openStudio(page)
  let releaseImage!: () => void
  const imageReady = new Promise<void>((resolve) => { releaseImage = resolve })
  await page.route('**/scroll-image.svg', async (route) => {
    await imageReady
    await route.fulfill({ contentType: 'image/svg+xml', body: '<svg xmlns="http://www.w3.org/2000/svg" width="160" height="640"><rect width="160" height="640" fill="#777"/></svg>' })
  })
  await run.emit(...startSubagent,
    { type: 'TOOL_CALL_START', toolCallId: 'read-call', toolCallName: 'read_file', rawEvent: subSource },
    { type: 'TOOL_CALL_ARGS', toolCallId: 'read-call', delta: `{\n${params('子工具参数')}`, rawEvent: subSource },
    text(prose('初始正文')),
  )
  await page.getByText('SubAgent', { exact: true }).click()
  await page.getByText('Read', { exact: true }).click()
  const input = page.getByRole('region', { name: '输入', exact: true })
  const output = page.getByRole('region', { name: '输出', exact: true }).last()
  await atBottom(input)
  await atBottom(output)
  await run.emit(text('\n\n```text\n代码行一\n代码行二\n```\n\n| 列一 | 列二 |\n| --- | --- |\n| 表格内容 | 追加内容 |\n\n正文标记'))
  await expect(output).toContainText('正文标记')
  await atBottom(output)
  await output.press('Home')
  await expect.poll(() => position(output)).toBe(0)
  await page.getByText('SubAgent', { exact: true }).click()
  await run.emit(text(`\n\n${prose('折叠正文')}`), { type: 'TOOL_CALL_ARGS', toolCallId: 'read-call', delta: `\n${params('折叠子参数')}`, rawEvent: subSource })
  await page.getByText('SubAgent', { exact: true }).click()
  await expect(output).toContainText('折叠正文 12')
  expect(await position(output)).toBe(0)
  await atBottom(input)
  await output.press('End')
  await atBottom(output)
  await page.setViewportSize({ width: 320, height: 900 })
  await atBottom(input)
  await atBottom(output)
  await run.emit(text('\n\n![延迟图示](/scroll-image.svg)\n\n图片后正文'))
  await expect(output).toContainText('图片后正文')
  await atBottom(output)
  const height = await output.evaluate((element) => element.scrollHeight)
  releaseImage()
  await expect.poll(() => output.getByRole('img').evaluate((image: HTMLImageElement) => image.naturalHeight)).toBe(640)
  await expect.poll(() => output.evaluate((element) => element.scrollHeight)).toBeGreaterThan(height)
  await atBottom(output)
})

for (const ending of ['failed', 'cancelled'] as const) test(`${ending} 终止不改变用户暂停的阅读位置`, async ({ page }) => {
  const run = await openStudio(page)
  await run.emit(...startSubagent, text(prose('运行正文')))
  await page.getByText('SubAgent', { exact: true }).click()
  const output = page.getByRole('region', { name: '输出', exact: true })
  await atBottom(output)
  await output.press('Home')
  await expect.poll(() => position(output)).toBe(0)
  await run.emit(text(`\n\n${prose('最终正文')}`))
  await expect(output).toContainText('最终正文 12')
  run.setHistory(completedHistory([historicalSubagent(`${prose('运行正文')}\n\n${prose('最终正文')}`, ending)], ending))
  await run.emit({ type: 'RUN_ERROR', code: ending, message: '运行已终止' })
  await expect(page.getByText(ending === 'cancelled' ? '已取消' : '执行失败', { exact: true }).first()).toBeAttached()
  expect(await position(output)).toBe(0)
  await run.finish()
})

test('首次打开已完成工具和子 Agent 时从顶部阅读', async ({ page }) => {
  await openStudio(page, 'light', 'reduce', completedHistory([
    historicalTool('write-call', 'write_file', `{\n${params('历史参数')}\n}`, prose('历史结果')),
    historicalSubagent(prose('历史正文')),
  ]))
  await page.getByText('Write', { exact: true }).click()
  await page.getByText('SubAgent', { exact: true }).click()
  for (const region of await page.getByRole('region', { name: /^(输入|输出)$/ }).all()) {
    await expect(region).toBeVisible()
    expect(await position(region)).toBe(0)
  }
})

for (const theme of ['light', 'dark']) for (const width of [320, 768, 1024, 1440]) {
  test(`流式工具布局 ${theme} ${width}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 1000 })
    const run = await openStudio(page, theme, width === 1440 ? 'no-preference' : 'reduce')
    await run.emit(
      { type: 'TOOL_CALL_START', toolCallId: 'write-call', toolCallName: 'write_file' },
      { type: 'TOOL_CALL_ARGS', toolCallId: 'write-call', delta: `{\n${params('工具参数')}` },
      ...startSubagent, text(prose('研究正文')),
    )
    await page.getByText('Write', { exact: true }).click()
    await page.getByText('SubAgent', { exact: true }).click()
    const input = page.getByRole('region', { name: '输入', exact: true })
    const output = page.getByRole('region', { name: '输出', exact: true }).last()
    await run.emit({ type: 'TOOL_CALL_ARGS', toolCallId: 'write-call', delta: '\n"末尾参数": "可见"' }, text('\n\n末尾正文'))
    await expect(input).toContainText('末尾参数')
    await expect(output).toContainText('末尾正文')
    await atBottom(input)
    await atBottom(output)
    await page.keyboard.press('Tab')
    await input.focus()
    await expect(input).toBeFocused()
    expect(await input.evaluate((element) => getComputedStyle(element).outlineStyle)).toBe('solid')
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
    await page.screenshot({ path: testInfo.outputPath(`tool-scroll-${theme}-${width}.png`), fullPage: true })
  })
}
