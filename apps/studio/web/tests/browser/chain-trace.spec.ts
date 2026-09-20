import { expect, test, type Page, type Route } from '@playwright/test'
import { resolve } from 'node:path'

import type { ConversationHistoryDetail } from '../../src/api/conversation/history'
import type {
  TraceGraph,
  TraceGraphNode,
  TraceGraphPage,
} from '../../src/api/conversation/traceGraph'

const THREAD_ID = 'chain-trace-browser-thread'
const OTHER_THREAD_ID = 'chain-trace-other-thread'
const RUN_ID = 'chain-trace-browser-run'
const BASE_TIME = '2026-09-01T00:00:00.000Z'
const SECOND_TURN_OFFSET = 60 * 60 * 1_000

const user = {
  user_id: 27,
  username: 'chain-browser-user',
  display_name: '链路用户',
  avatar_url: null,
  roles: [],
  disabled: false,
}

const success = (data: unknown) => ({ code: 0, message: 'success', data })
const fulfillJson = (route: Route, data: unknown) => route.fulfill({
  status: 200,
  contentType: 'application/json',
  body: JSON.stringify(success(data)),
})

const timestamp = (offset: number) => new Date(
  Date.parse(BASE_TIME) + offset,
).toISOString()

const graphNode = (
  id: string,
  kind: TraceGraphNode['kind'],
  startedSeq: number,
  values: Partial<TraceGraphNode> = {},
): TraceGraphNode => ({
  agui: null,
  id,
  turnId: 'turn-browser-2',
  parentSubagentId: null,
  modelCallId: null,
  kind,
  status: 'succeeded',
  name: kind === 'human_message'
    ? 'HumanMessage'
    : kind === 'assistant_message' ? 'AssistantMessage' : id,
  runId: RUN_ID,
  graphNamespace: [],
  startedAt: timestamp(
    (startedSeq < 10 ? 0 : SECOND_TURN_OFFSET) + startedSeq * 100,
  ),
  completedAt: timestamp(
    (startedSeq < 10 ? 0 : SECOND_TURN_OFFSET) + startedSeq * 100 + 80,
  ),
  startedSeq,
  updatedSeq: startedSeq,
  contentOmitted: false,
  toolCallOnly: false,
  requestOmitted: false,
  resultOmitted: false,
  linkIssues: [],
  ...values,
})

const nodes: TraceGraphNode[] = [
  graphNode('old-human', 'human_message', 1, {
    turnId: 'turn-browser-1',
    runId: 'run-browser-1',
    content: '第一轮浏览器任务',
  }),
  graphNode('old-assistant', 'assistant_message', 2, {
    turnId: 'turn-browser-1',
    runId: 'run-browser-1',
    content: '第一轮已完成',
  }),
  graphNode('human-current', 'human_message', 10, {
    content: '继续核验真实链路',
  }),
  graphNode('context-current', 'context', 11, {
    name: 'Context',
    content: '# 浏览器系统提示词',
  }),
  graphNode('model-current', 'model', 12, {
    name: 'deepseek-v4-pro',
    provider: 'deepseek',
    model: 'deepseek-v4-pro',
    firstOutputAt: timestamp(SECOND_TURN_OFFSET + 1_260),
    request: {
      messages: [{ messageType: 'system', content: '# 浏览器系统提示词' }],
    },
    usage: {
      input_token_details: { cache_read: 4736 },
      input_tokens: 4872,
      output_token_details: { reasoning: 123 },
      output_tokens: 185,
      total_tokens: 5057,
    },
    responseMetadata: { finish_reason: 'tool_calls' },
  }),
  graphNode('assistant-stage', 'assistant_message', 13, {
    modelCallId: 'model-current',
    content: '准备委派任务',
  }),
  graphNode('tool-current', 'tool', 14, {
    modelCallId: 'model-current',
    name: 'web_search',
    sourceId: 'web-search-call',
    request: { query: '真实链路' },
    result: '搜索完成',
  }),
  graphNode('subagent-outer', 'subagent', 15, {
    modelCallId: 'model-current',
    name: 'researcher',
    graphNamespace: ['tools:outer'],
    request: { description: '核验链路来源' },
  }),
  graphNode('subagent-human', 'human_message', 16, {
    parentSubagentId: 'subagent-outer',
    graphNamespace: ['tools:outer'],
    content: '核验链路来源',
  }),
  graphNode('subagent-context', 'plan', 17, {
    parentSubagentId: 'subagent-outer',
    graphNamespace: ['tools:outer'],
    name: 'Plan',
    result: { title: '子智能体计划' },
  }),
  graphNode('subagent-model', 'model', 18, {
    parentSubagentId: 'subagent-outer',
    graphNamespace: ['tools:outer'],
    name: 'deepseek-v4-flash',
    provider: 'deepseek',
    model: 'deepseek-v4-flash',
    request: { messages: [] },
  }),
  graphNode('subagent-tool', 'tool', 19, {
    parentSubagentId: 'subagent-outer',
    modelCallId: 'subagent-model',
    graphNamespace: ['tools:outer'],
    name: 'read_file',
    request: { file_path: 'README.md' },
    result: '读取完成',
  }),
  graphNode('subagent-inner', 'subagent', 20, {
    parentSubagentId: 'subagent-outer',
    modelCallId: 'subagent-model',
    graphNamespace: ['tools:outer', 'tools:inner'],
    name: 'researcher',
    request: { description: '继续核验嵌套链路' },
  }),
  graphNode('inner-human', 'human_message', 21, {
    parentSubagentId: 'subagent-inner',
    graphNamespace: ['tools:outer', 'tools:inner'],
    content: '继续核验嵌套链路',
  }),
  graphNode('inner-assistant', 'assistant_message', 22, {
    parentSubagentId: 'subagent-inner',
    graphNamespace: ['tools:outer', 'tools:inner'],
    content: '嵌套核验完成',
  }),
  graphNode('subagent-assistant', 'assistant_message', 23, {
    parentSubagentId: 'subagent-outer',
    modelCallId: 'subagent-model',
    graphNamespace: ['tools:outer'],
    content: '',
    toolCallOnly: true,
  }),
  graphNode('subagent-leaf', 'subagent', 24, {
    name: 'researcher',
    graphNamespace: ['tools:leaf'],
    request: { description: '无子节点任务' },
  }),
  graphNode('model-final', 'model', 25, {
    name: 'deepseek-v4-pro',
    provider: 'deepseek',
    model: 'deepseek-v4-pro',
    request: { messages: [] },
  }),
  graphNode('assistant-current', 'assistant_message', 26, {
    modelCallId: 'model-final',
    content: '链路完成',
  }),
  graphNode('skill-read', 'tool', 27, {
    modelCallId: 'model-final',
    name: 'read_file',
    request: { file_path: '/skills/research/SKILL.md' },
    result: 'Skill 内容',
  }),
  graphNode('failed-tool', 'tool', 28, {
    modelCallId: 'model-final',
    name: 'web_search',
    status: 'failed',
    request: { query: '失败场景' },
    failure: {
      errorType: 'builtins.TimeoutError',
      message: '搜索服务在期限内未响应',
    },
  }),
]

const tracePage = (
  sourceNodes: TraceGraphNode[] = nodes,
  detailsOmitted = false,
): TraceGraphPage => ({
  turns: [
    { id: 'turn-browser-1', ordinal: 1, startedAt: BASE_TIME },
    {
      id: 'turn-browser-2',
      ordinal: 2,
      startedAt: timestamp(SECOND_TURN_OFFSET + 1_000),
    },
  ].filter((turn) => sourceNodes.some((node) => node.turnId === turn.id)),
  nodes: sourceNodes,
  orderedNodeIds: sourceNodes.map((node) => node.id),
  matchedNodeIds: sourceNodes.map((node) => node.id),
  nextCursor: null,
  asOfSeq: 40,
  completeness: {
    callTrackingMissing: false,
    relationshipEvidenceMissing: false,
    detailsOmitted,
  },
})

const responseNodes = nodes.filter((node) => (
  node.modelCallId === 'model-current'
  && ['assistant_message', 'tool', 'subagent'].includes(node.kind)
))
const responsePage = (detailsOmitted = false) => tracePage(
  responseNodes,
  detailsOmitted,
)

const emptyGraph = (asOfSeq: number): TraceGraph => ({
  turns: [],
  nodes: [],
  orderedNodeIds: [],
  matchedNodeIds: [],
  asOfSeq,
  completeness: {
    callTrackingMissing: false,
    relationshipEvidenceMissing: false,
    detailsOmitted: false,
  },
})

const detail = (
  threadId = THREAD_ID,
  includeTaskTrace = true,
): ConversationHistoryDetail => ({ accessMode: 'write_approval',
  id: threadId === THREAD_ID ? 1 : 2,
  threadId,
  title: threadId === THREAD_ID ? '链路浏览器会话' : '另一个会话',
  titleSource: 'default',
  titleGenerationStatus: 'idle',
  titleSeq: 0,
  lastModel: 'deepseek-v4-pro',
  pinned: false,
  asOfSeq: 30,
  generation: `browser-generation:${threadId}`,
  observedAt: '2026-09-05T00:00:00.000000Z',
  headRunId: RUN_ID,
  runFailures: [],
  availableHeads: [RUN_ID],
  historyCursor: null,
  messageCount: 30,
  toolCallCount: 2,
  messages: Array.from({ length: 30 }, (_, index) => ({
    agui: null,
    id: `${threadId}-message-${index + 1}`,
    traceSeq: index + 1,
    sourceId: `assistant-history-${index + 1}`,
    graphNamespace: [],
    runId: RUN_ID,
    role: 'assistant' as const,
    content: `第 ${index + 1} 段历史回复，用于验证链路与对话切换后仍能精确恢复阅读位置。`,
    contentOmitted: false,
    status: 'completed' as const,
    createdAt: timestamp(index * 1_000),
    completedAt: timestamp(index * 1_000 + 500),
  })),
  reasoning: [],
  graph: emptyGraph(30),
  state: { root: {}, subgraphs: {} },
  interactions: [],
  status: { execution: 'succeeded', headRunId: RUN_ID },
  completeness: { missingPrefix: false, missingTail: false, payloadOmitted: false },
  taskTrace: includeTaskTrace ? {
    status: 'ready',
    todoGroups: [{
      id: 'trace-browser-todos',
      userMessageId: 'human-current',
      userMessagePreview: '继续核验真实链路',
      groupToolCallId: 'todo-tool-call',
      createdAt: timestamp(1_000),
      status: 'completed',
      todos: [{ id: 'verify-trace', content: '核验链路', status: 'completed' }],
    }],
  } : null,
  createdAt: BASE_TIME,
  updatedAt: timestamp(30_000),
})

async function mockChainTraceStudio(
  page: Page,
  options: {
    theme?: 'light' | 'dark'
    language?: 'zh-CN' | 'en'
    snapshot?: TraceGraphPage
    directSnapshot?: TraceGraphPage
  } = {},
) {
  const {
    theme = 'light',
    language = 'zh-CN',
    snapshot = tracePage(),
    directSnapshot = responsePage(),
  } = options
  const pageErrors: string[] = []
  page.on('pageerror', (error) => pageErrors.push(error.message))
  page.on('console', (message) => {
    if (message.type() === 'error') pageErrors.push(message.text())
  })
  await page.addInitScript(({
    session,
    threadId,
    selectedTheme,
    selectedLanguage,
    initialSnapshotJson,
  }) => {
    const initialSnapshot = JSON.parse(initialSnapshotJson) as TraceGraphPage
    window.localStorage.setItem('tinkerfin.auth.session', JSON.stringify(session))
    window.localStorage.setItem('tinkerfin:theme', selectedTheme)
    window.localStorage.setItem('tinkerfin:language', selectedLanguage)
    const traceWindow = window as typeof window & {
      __traceFollowUrls: string[]
      __traceSnapshotUrls: string[]
      __traceDirectUrls: string[]
    }
    traceWindow.__traceFollowUrls = []
    traceWindow.__traceSnapshotUrls = []
    traceWindow.__traceDirectUrls = []
    const originalFetch = window.fetch.bind(window)
    window.fetch = (input, init) => {
      const rawUrl = typeof input === 'string'
        ? input
        : input instanceof URL ? input.href : input.url
      const url = new URL(rawUrl, window.location.origin)
      if (url.pathname === `/api/conversation/${threadId}/trace/graph/follow`) {
        traceWindow.__traceFollowUrls.push(url.href)
      }
      if (url.pathname === `/api/conversation/${threadId}/trace/graph` && !url.searchParams.has('modelCallId')) {
        traceWindow.__traceSnapshotUrls.push(url.href)
        const kinds = new Set(url.searchParams.getAll('kind'))
        const query = url.searchParams.get('query')?.toLocaleLowerCase() ?? ''
        const direct = initialSnapshot.nodes.filter((node) => (
          (kinds.size === 0 || kinds.has(node.kind))
          && (!query || JSON.stringify(node).toLocaleLowerCase().includes(query))
        ))
        const byId = new Map(initialSnapshot.nodes.map((node) => [node.id, node]))
        const included = new Set(direct.map((node) => node.id))
        const pending = direct.map((node) => node.parentSubagentId)
        while (pending.length > 0) {
          const parentId = pending.pop()
          if (!parentId || included.has(parentId)) continue
          const parent = byId.get(parentId)
          if (!parent) throw new Error('Fixture contains a missing Subagent parent')
          included.add(parentId)
          pending.push(parent.parentSubagentId)
        }
        const filteredNodes = initialSnapshot.nodes.filter((node) => included.has(node.id))
        const directIds = new Set(direct.map((node) => node.id))
        const filteredSnapshot = {
          ...initialSnapshot,
          turns: initialSnapshot.turns.filter((turn) => (
            filteredNodes.some((node) => node.turnId === turn.id)
          )),
          nodes: filteredNodes,
          orderedNodeIds: filteredNodes.map((node) => node.id),
          matchedNodeIds: filteredNodes
            .filter((node) => directIds.has(node.id))
            .map((node) => node.id),
        }
        return Promise.resolve(Response.json({ code: 0, message: 'success', data: filteredSnapshot }))
      }
      if (url.pathname === `/api/conversation/${threadId}/trace/graph`) {
        traceWindow.__traceDirectUrls.push(url.href)
      }
      return originalFetch(input, init)
    }
  }, {
    session: {
      token: 'chain-browser-token',
      tokenType: 'Bearer',
      expiresAt: '2099-01-01T00:00:00.000Z',
      user,
    },
    threadId: THREAD_ID,
    selectedTheme: theme,
    selectedLanguage: language,
    initialSnapshotJson: JSON.stringify(snapshot),
  })

  await page.route('**/api/**', async (route) => {
    const url = new URL(route.request().url())
    if (url.pathname === '/api/auth/me') {
      await fulfillJson(route, { expires_at: '2099-01-01T00:00:00.000Z', user })
      return
    }
    if (url.pathname === '/api/models') {
      await fulfillJson(route, {
        items: [{
          modelId: 'deepseek-v4-pro',
          displayName: 'DeepSeek V4 Pro',
          connectionId: 'test-provider', connectionDisplayName: '测试提供方', reasoningEnabled: false,
          isDefault: true,
        }],
        defaultModelId: 'deepseek-v4-pro',
      })
      return
    }
    if (url.pathname === '/api/conversation/config') {
      await fulfillJson(route, { dayRanges: [7, 30] })
      return
    }
    if (url.pathname === '/api/conversation/history') {
      await fulfillJson(route, {
        items: [THREAD_ID, OTHER_THREAD_ID].map((threadId, index) => ({ accessMode: 'full',
          id: index + 1,
          threadId,
          title: threadId === THREAD_ID ? '链路浏览器会话' : '另一个会话',
          status: 'idle',
          lastRunId: RUN_ID,
          lastModel: 'deepseek-v4-pro',
          messageCount: 30,
          toolCallCount: 2,
          hasPendingInterrupt: false,
          pendingInteractionKind: null,
          pinned: false,
          createdAt: BASE_TIME,
          updatedAt: timestamp(30_000 - index),
        })),
        nextCursor: null,
      })
      return
    }
    if (url.pathname === `/api/conversation/${THREAD_ID}/history`) {
      await fulfillJson(
        route,
        detail(THREAD_ID, url.searchParams.get('includeTaskTrace') !== 'false'),
      )
      return
    }
    if (url.pathname === `/api/conversation/${OTHER_THREAD_ID}/history`) {
      await fulfillJson(
        route,
        detail(OTHER_THREAD_ID, url.searchParams.get('includeTaskTrace') !== 'false'),
      )
      return
    }
    if (url.pathname === `/api/conversation/${THREAD_ID}/trace`) {
      const includeTaskTrace = url.searchParams.get('includeTaskTrace') !== 'false'
      await route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body: `event: trace\ndata: ${JSON.stringify({
          type: 'snapshot',
          snapshot: detail(THREAD_ID, includeTaskTrace),
        })}\n\n`,
      })
      return
    }
    if (url.pathname === `/api/conversation/${THREAD_ID}/trace/graph`) {
      await fulfillJson(route, directSnapshot)
      return
    }
    await route.fulfill({ status: 404, contentType: 'application/json', body: '{}' })
  })

  await page.goto(`/?thread=${THREAD_ID}`)
  const conversationLabel = language === 'en' ? 'Conversation' : '对话'
  const traceLabel = language === 'en' ? 'Trace' : '链路'
  await expect(page.getByRole('tab', { name: conversationLabel })).toHaveAttribute(
    'aria-selected',
    'true',
  )
  await expect(page.getByRole('tab', { name: traceLabel })).toBeVisible()
  return pageErrors
}

const traceRequests = (page: Page) => page.evaluate(() => {
  const traceWindow = window as typeof window & {
    __traceFollowUrls: string[]
    __traceSnapshotUrls: string[]
    __traceDirectUrls: string[]
  }
  return {
    follow: traceWindow.__traceFollowUrls,
    snapshots: traceWindow.__traceSnapshotUrls,
    direct: traceWindow.__traceDirectUrls,
  }
})

const traceRow = (page: Page, nodeId: string) => (
  page.locator(`[data-trace-node-id="${nodeId}"]`)
)

for (const theme of ['light', 'dark'] as const) {
  for (const width of [320, 768, 1024, 1440]) {
    test(`抽屉打开后主区滚动条自动隐藏 ${theme} ${width}`, async ({ page }, testInfo) => {
      await page.setViewportSize({ width, height: 700 })
      await page.emulateMedia({ reducedMotion: 'reduce' })
      const errors = await mockChainTraceStudio(page, { theme })
      await page.clock.install()
      const conversationBar = page.locator('.conversation-region > .ui-overlay-scrollbar')
      await page.getByRole('region', { name: '对话内容', exact: true }).hover()
      await expect(conversationBar).toHaveCSS('opacity', '1')
      if (width === 1440) {
        const thumb = (await conversationBar.locator('.ui-overlay-scrollbar__thumb').boundingBox())!
        await page.mouse.move(thumb.x + thumb.width / 2, thumb.y + thumb.height / 2)
        await page.mouse.down()
        await page.mouse.move(1, 1)
        await page.mouse.up()
        await page.clock.runFor(1_200)
        await expect(conversationBar).toHaveCSS('opacity', '0')
      }
      await page.getByRole('button', { name: '任务轨迹 1', exact: true }).click()
      await expect(page.getByRole('complementary', { name: '任务轨迹' })).toBeVisible({ visible: width === 320 || width === 1440 })
      await page.mouse.move(1, 1)
      await page.clock.runFor(1_200)
      await expect(conversationBar).toHaveCSS('opacity', '0')
      await page.screenshot({ path: testInfo.outputPath(`scrollbar-todo-${theme}-${width}.png`) })
      if (width === 1440) await page.getByRole('button', { name: '关闭任务轨迹' }).click()
      if (width === 320) await page.getByRole('button', { name: '返回对话' }).click()
      await page.getByRole('tab', { name: '链路', exact: true }).click()
      const close = page.getByRole('button', { name: '关闭链路详情' })
      // 并排布局会自动选择节点，先关闭以覆盖用户主动打开详情的路径
      if (width === 1440) await close.click()
      const node = traceRow(page, 'model-current')
      await node.click()
      await expect(close).toBeVisible({ visible: width === 1440 })
      await page.mouse.move(1, 1)
      await page.clock.runFor(1_200)
      const ledgerBar = page.locator('.chain-trace-ledger-scroll-host > .ui-overlay-scrollbar')
      await expect(ledgerBar).toHaveAttribute('data-scrollable', 'true')
      await expect(ledgerBar).toHaveCSS('opacity', '0')
      if (width === 1440) {
        await expect(node).toBeFocused()
        await page.keyboard.press('ArrowDown')
        await page.clock.runFor(1_200)
        await expect(ledgerBar).toHaveCSS('opacity', '1')
        await close.click()
        await page.clock.runFor(32)
        await expect(node).toBeFocused()
        await page.mouse.move(1, 1)
        await page.clock.runFor(1_200)
        await expect(ledgerBar).toHaveCSS('opacity', '0')
        await node.press('Enter')
        await page.clock.runFor(1_200)
        await expect(ledgerBar).toHaveCSS('opacity', '1')
      } else if (width === 320) {
        await expect(page.getByRole('button', { name: '返回链路' })).toBeFocused()
      } else {
        await expect(node).toBeFocused()
      }
      await page.screenshot({ path: testInfo.outputPath(`scrollbar-chain-${theme}-${width}.png`) })
      expect(errors).toEqual([])
    })
  }
}

test('六类时间线、平级台账、Subagent 作用域和详情保持同一权威顺序', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  const pageErrors = await mockChainTraceStudio(page)
  const traceTab = page.getByRole('tab', { name: '链路' })
  const sidebarToggle = page.getByRole('button', { name: '收起侧边栏' })
  const [traceTabBox, sidebarToggleBox] = await Promise.all([
    traceTab.boundingBox(),
    sidebarToggle.boundingBox(),
  ])
  expect(Math.abs(
    (traceTabBox?.y ?? 0) + (traceTabBox?.height ?? 0) / 2
      - ((sidebarToggleBox?.y ?? 0) + (sidebarToggleBox?.height ?? 0) / 2),
  )).toBeLessThanOrEqual(1)

  await traceTab.click()
  await expect(page.getByRole('tabpanel', { name: '链路' })).toBeVisible()
  await expect.poll(async () => (await traceRequests(page)).snapshots.length).toBe(1)
  await expect(page.getByRole('region', { name: '调用时间线' })).toBeVisible()
  await expect(page.getByLabel('链路节点', { exact: true })).toBeVisible()
  await expect.poll(() => page.getByLabel('链路节点', { exact: true }).evaluate(
    (element) => Math.abs(element.scrollHeight - element.clientHeight - element.scrollTop),
  )).toBeLessThanOrEqual(1)
  await expect(page.getByRole('tab', { name: '树形' })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '技术' })).toHaveCount(0)

  expect(await page.locator('.chain-trace-lane-label').allTextContents()).toEqual([
    '用户',
    '上下文',
    '模型',
    '工具',
    '子智能体',
    '助手',
  ])
  await expect(page.getByRole('group', { name: '节点类型' })).toHaveCount(0)

  const outerShell = traceRow(page, 'subagent-outer').locator('..')
  const outerToggle = outerShell.locator('.chain-trace-ledger-toggle')
  await expect(outerToggle).toHaveAttribute('aria-expanded', 'true')
  const controlledIds = (await outerToggle.getAttribute('aria-controls'))?.split(' ') ?? []
  expect(controlledIds).toContain('trace-ledger-node-subagent-inner')
  for (const controlledId of controlledIds) {
    await expect(page.locator(`#${controlledId}`)).toHaveCount(1)
  }
  await expect(
    traceRow(page, 'subagent-inner').locator('..').locator('.chain-trace-ledger-toggle'),
  ).toHaveAttribute('aria-expanded', 'true')
  await expect(
    traceRow(page, 'subagent-leaf').locator('..').locator('.chain-trace-ledger-toggle'),
  ).toHaveCount(0)

  await traceRow(page, 'inner-assistant').click()
  const details = page.locator('.chain-trace-details')
  await expect(details).toBeVisible()
  await outerToggle.click()
  await expect(traceRow(page, 'inner-assistant')).toBeHidden()
  await expect(details).toBeHidden()
  await expect(outerToggle).toBeFocused()
  await outerToggle.click()
  await expect(traceRow(page, 'inner-assistant')).toBeVisible()

  await traceRow(page, 'failed-tool').click()
  await expect(details.getByText('搜索服务在期限内未响应')).toBeVisible()
  await details.getByRole('button', { name: '关闭链路详情' }).click()
  await expect(details).toBeHidden()
  await expect(traceRow(page, 'failed-tool')).toBeFocused()

  await traceRow(page, 'model-current').click()
  await details.getByRole('tab', { name: '响应' }).click()
  await expect(details.getByText('准备委派任务')).toBeVisible()
  await expect(details.getByText(/"name": "web_search"/)).toBeVisible()
  await expect(details.getByText(/"finish_reason": "tool_calls"/)).toBeVisible()
  await details.getByRole('tab', { name: '用量' }).click()
  await expect(details.getByText('4,872')).toBeVisible()
  await expect(details.getByText('4,736')).toBeVisible()
  await expect(details.getByText('123')).toBeVisible()
  await details.getByRole('tab', { name: '系统提示词' }).click()
  await expect(details.getByRole('heading', { name: '浏览器系统提示词' })).toBeVisible()

  await expect(traceRow(page, 'skill-read')).toContainText('read_file')
  await expect(traceRow(page, 'skill-read')).toContainText('SKILL.md')
  await expect(page.getByText('Skill', { exact: true })).toHaveCount(0)
  await expect(page.locator('.chain-trace-detail-resizer')).toHaveCount(0)

  const geometry = await page.locator('.workspace-main').evaluate((workspace) => {
    const header = workspace.querySelector<HTMLElement>('.chat-header')!
    const toolbar = workspace.querySelector<HTMLElement>('.chain-trace-toolbar')!
    const summary = workspace.querySelector<HTMLElement>('.chain-trace-range-summary')!
    const search = workspace.querySelector<HTMLElement>('.chain-trace-search-control')!
    const ledgerHeader = workspace.querySelector<HTMLElement>('.chain-trace-ledger-header')!
    const rootRow = workspace.querySelector<HTMLElement>(
      '[data-trace-node-id="human-current"]',
    )!
    const rail = rootRow.querySelector<HTMLElement>('.chain-trace-ledger-rail')!
    const typePill = rootRow.querySelector<HTMLElement>('.chain-trace-type-pill')!
    const main = rootRow.querySelector<HTMLElement>('.chain-trace-ledger-main')!
    const duration = rootRow.querySelector<HTMLElement>('.chain-trace-duration')!
    const drawerHeader = workspace.querySelector<HTMLElement>('.chain-trace-details-header')!
    const drawerTitle = drawerHeader.querySelector<HTMLElement>('.chain-trace-details-context')!
    const close = drawerHeader.querySelector<HTMLElement>('.ui-icon-button')!
    const searchIcon = search.querySelector<SVGElement>('.ui-icon-button__icon svg')!
    const closeIcon = close.querySelector<SVGElement>('.ui-icon-button__icon svg')!
    const rootRect = rootRow.getBoundingClientRect()
    const railRect = rail.getBoundingClientRect()
    const typePillRect = typePill.getBoundingClientRect()
    const typeHeaderRect = ledgerHeader.children[1]!.getBoundingClientRect()
    const mainRect = main.getBoundingClientRect()
    const subagentRow = workspace.querySelector<HTMLElement>(
      '[data-trace-node-id="subagent-outer"]',
    )!
    const subagentTitleRect = subagentRow.querySelector<HTMLElement>(
      '.chain-trace-node-title strong',
    )!.getBoundingClientRect()
    const subagentPreviewRect = subagentRow.querySelector<HTMLElement>(
      '.chain-trace-node-content',
    )!.getBoundingClientRect()
    const titleRect = drawerTitle.getBoundingClientRect()
    const closeRect = close.getBoundingClientRect()
    return {
      headerHeight: header.getBoundingClientRect().height,
      headerBorderTop: getComputedStyle(header).borderTopWidth,
      headerBorderBottom: getComputedStyle(header).borderBottomWidth,
      toolbarPadding: getComputedStyle(toolbar).paddingLeft,
      ledgerPadding: getComputedStyle(ledgerHeader).paddingLeft,
      summaryInset: summary.getBoundingClientRect().left
        - toolbar.getBoundingClientRect().left,
      searchInset: toolbar.getBoundingClientRect().right
        - search.getBoundingClientRect().right,
      rootInset: rail.getBoundingClientRect().left - rootRect.left,
      rootHeight: rootRect.height,
      railToTypeGap: typePillRect.left - (
        railRect.left + Number.parseFloat(getComputedStyle(rail, '::after').width)
      ),
      typeToContentGap: mainRect.left - typePillRect.right,
      titlePreviewAlignment: Math.abs(
        subagentTitleRect.top + subagentTitleRect.height / 2
          - (subagentPreviewRect.top + subagentPreviewRect.height / 2),
      ),
      typeWidths: [...workspace.querySelectorAll<HTMLElement>('.chain-trace-type-pill')]
        .map((element) => element.getBoundingClientRect().width),
      typeHeaderAlignment: Math.abs(
        typeHeaderRect.left + typeHeaderRect.width / 2
          - (typePillRect.left + typePillRect.width / 2),
      ),
      toolOnlyColor: getComputedStyle(
        workspace.querySelector<HTMLElement>(
          '[data-trace-node-id="subagent-assistant"] .chain-trace-node-content',
        )!,
      ).color,
      previewColor: getComputedStyle(
        workspace.querySelector<HTMLElement>(
          '[data-trace-node-id="subagent-outer"] .chain-trace-node-content',
        )!,
      ).color,
      durationInset: rootRect.right - duration.getBoundingClientRect().right,
      drawerWidth: workspace.querySelector<HTMLElement>('.chain-trace-details')!
        .getBoundingClientRect().width,
      drawerHeaderHeight: drawerHeader.getBoundingClientRect().height,
      drawerAlignment: Math.abs(
        titleRect.top + titleRect.height / 2 - (closeRect.top + closeRect.height / 2),
      ),
      actionAlignment: Math.abs(
        search.getBoundingClientRect().left + search.getBoundingClientRect().width / 2
          - (closeRect.left + closeRect.width / 2),
      ),
      actionIconLeftAlignment: Math.abs(
        searchIcon.getBoundingClientRect().left - closeIcon.getBoundingClientRect().left,
      ),
      actionIconRightAlignment: Math.abs(
        searchIcon.getBoundingClientRect().right - closeIcon.getBoundingClientRect().right,
      ),
      selectedBackground: getComputedStyle(
        workspace.querySelector<HTMLElement>(
          '[data-trace-node-id="model-current"]',
        )!,
      ).backgroundColor,
    }
  })
  expect(geometry).toMatchObject({
    headerHeight: 64,
    headerBorderTop: '0px',
    headerBorderBottom: '0px',
    toolbarPadding: geometry.ledgerPadding,
    drawerWidth: 400,
    drawerHeaderHeight: 64,
    rootHeight: 40,
  })
  expect(Math.abs(geometry.rootInset - geometry.durationInset)).toBeLessThanOrEqual(1)
  expect(Math.abs(geometry.summaryInset - geometry.searchInset)).toBeLessThanOrEqual(1)
  expect(geometry.titlePreviewAlignment).toBeLessThanOrEqual(0.5)
  expect(Math.abs(geometry.railToTypeGap - geometry.typeToContentGap)).toBeLessThanOrEqual(0.5)
  expect(Math.max(...geometry.typeWidths) - Math.min(...geometry.typeWidths))
    .toBeLessThanOrEqual(0.5)
  expect(geometry.typeHeaderAlignment).toBeLessThanOrEqual(0.5)
  expect(geometry.toolOnlyColor).toBe(geometry.previewColor)
  expect(geometry.drawerAlignment).toBeLessThanOrEqual(1)
  expect(geometry.actionAlignment).toBeLessThanOrEqual(1)
  expect(geometry.actionIconLeftAlignment).toBeLessThanOrEqual(0.5)
  expect(geometry.actionIconRightAlignment).toBeLessThanOrEqual(0.5)
  expect(geometry.selectedBackground).not.toBe('rgba(0, 0, 0, 0)')

  expect(pageErrors).toEqual([])
})

test('固定列标题独立于节点滚动并同步横向列位置', async ({ page }) => {
  await page.setViewportSize({ width: 640, height: 800 })
  const pageErrors = await mockChainTraceStudio(page)
  await page.getByRole('tab', { name: '链路' }).click()
  const ledger = page.getByLabel('链路节点', { exact: true })
  const headerViewport = page.locator('.chain-trace-ledger-header-viewport')
  const headerTop = await headerViewport.evaluate((element) => (
    element.getBoundingClientRect().top
  ))

  await ledger.evaluate((element) => element.scrollTo({
    top: 160,
    left: 80,
    behavior: 'instant',
  }))
  await ledger.hover()
  await expect.poll(() => headerViewport.evaluate((element) => element.scrollLeft)).toBe(80)
  const geometry = await page.locator('.chain-trace-ledger-host').evaluate((host) => {
    const header = host.querySelector<HTMLElement>('.chain-trace-ledger-header-viewport')!
    const turnHeading = host.querySelector<HTMLElement>('.chain-trace-turn-heading')!
    const scrollbar = host.querySelector<HTMLElement>(
      '.chain-trace-ledger-scroll-host > .ui-overlay-scrollbar--vertical',
    )!
    const headerRect = header.getBoundingClientRect()
    const scrollbarRect = scrollbar.getBoundingClientRect()
    const turnCaps = [...host.querySelectorAll<HTMLElement>('.chain-trace-ledger-turn')]
      .map((turn) => {
        const firstRail = turn.querySelector<HTMLElement>(
          '.chain-trace-ledger-row.is-turn-root-start .chain-trace-ledger-rail',
        )!
        const lastRail = turn.querySelector<HTMLElement>(
          '.chain-trace-ledger-row.is-turn-root-end .chain-trace-ledger-rail',
        )!
        return {
          firstTop: Number.parseFloat(getComputedStyle(firstRail, '::before').top),
          firstHalf: firstRail.getBoundingClientRect().height / 2,
          firstElbowTop: Number.parseFloat(getComputedStyle(firstRail, '::after').top),
          firstElbowHeight: Number.parseFloat(getComputedStyle(firstRail, '::after').height),
          firstElbowBorderLeft: getComputedStyle(firstRail, '::after').borderLeftWidth,
          firstElbowRadius: getComputedStyle(firstRail, '::after').borderBottomLeftRadius,
          lastBottom: Number.parseFloat(getComputedStyle(lastRail, '::before').bottom),
          lastHalf: lastRail.getBoundingClientRect().height / 2,
        }
      })
    return {
      headerTop: headerRect.top,
      turnHeadingPosition: getComputedStyle(turnHeading).position,
      scrollbarGap: scrollbarRect.top - headerRect.bottom,
      turnCaps,
    }
  })
  expect(geometry).toMatchObject({
    headerTop,
    turnHeadingPosition: 'static',
    scrollbarGap: 3,
  })
  expect(geometry.turnCaps).toHaveLength(2)
  geometry.turnCaps.forEach((cap) => {
    expect(Math.abs(cap.firstTop - cap.firstHalf)).toBeLessThanOrEqual(0.5)
    expect(Math.abs(cap.firstElbowTop - cap.firstHalf)).toBeLessThanOrEqual(0.5)
    expect(cap.firstElbowHeight).toBeLessThanOrEqual(1)
    expect(cap.firstElbowBorderLeft).toBe('0px')
    expect(cap.firstElbowRadius).toBe('0px')
    expect(Math.abs(cap.lastBottom - cap.lastHalf)).toBeLessThanOrEqual(0.5)
  })
  expect(pageErrors).toEqual([])
})

test('选中节点只展示所属 Turn 且蓝色选区严格对齐节点条', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 800 })
  const pageErrors = await mockChainTraceStudio(page)
  await page.getByRole('tab', { name: '链路' }).click()
  const summary = page.locator('.chain-trace-range-summary')
  const lastTick = page.locator('.chain-trace-ticks span').last()

  await traceRow(page, 'old-human').click()
  await expect(summary).not.toContainText('当前范围')
  await expect(summary).toContainText('第 1 轮')
  await expect(summary).toContainText('2 节点')
  await expect(lastTick).toHaveText('280 毫秒')
  const firstTurnAlignment = await page.evaluate(() => {
    const bar = document.querySelector<HTMLElement>('.chain-trace-timeline-bar.is-selected')!
    const selection = document.querySelector<HTMLElement>('.chain-trace-timeline-selection')!
    const barRect = bar.getBoundingClientRect()
    const selectionRect = selection.getBoundingClientRect()
    return {
      left: Math.abs(barRect.left - selectionRect.left),
      right: Math.abs(barRect.right - selectionRect.right),
    }
  })
  expect(firstTurnAlignment.left).toBeLessThanOrEqual(0.5)
  expect(firstTurnAlignment.right).toBeLessThanOrEqual(0.5)

  await traceRow(page, 'human-current').click()
  await expect(summary).toContainText('第 2 轮')
  await expect(summary).toContainText('19 节点')
  await expect(lastTick).toHaveText('1.88 秒')
  const secondTurnAlignment = await page.evaluate(() => {
    const bar = document.querySelector<HTMLElement>('.chain-trace-timeline-bar.is-selected')!
    const selection = document.querySelector<HTMLElement>('.chain-trace-timeline-selection')!
    const barRect = bar.getBoundingClientRect()
    const selectionRect = selection.getBoundingClientRect()
    return {
      left: Math.abs(barRect.left - selectionRect.left),
      right: Math.abs(barRect.right - selectionRect.right),
    }
  })
  expect(secondTurnAlignment.left).toBeLessThanOrEqual(0.5)
  expect(secondTurnAlignment.right).toBeLessThanOrEqual(0.5)
  expect(pageErrors).toEqual([])
})

test('搜索结果中的 Model 详情只补取一次完整响应且不重复读取列表', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 850 })
  const pageErrors = await mockChainTraceStudio(page)
  await page.getByRole('tab', { name: '链路' }).click()
  await page.getByRole('button', { name: '搜索链路节点' }).click()
  await page.getByRole('searchbox', { name: '搜索链路节点' }).fill('4872')
  await expect.poll(async () => (await traceRequests(page)).snapshots.length).toBe(2)
  const snapshotCount = (await traceRequests(page)).snapshots.length

  await expect.poll(async () => (await traceRequests(page)).direct.length).toBe(1)
  const requests = await traceRequests(page)
  expect(requests.snapshots).toHaveLength(snapshotCount)
  const direct = new URL(requests.direct[0]!)
  expect(direct.searchParams.get('modelCallId')).toBe('model-current')
  expect(direct.searchParams.getAll('kind')).toEqual([])
  const details = page.locator('.chain-trace-details')
  await details.getByRole('tab', { name: '响应' }).click()
  await expect(details.getByText('准备委派任务')).toBeVisible()
  await expect(details.getByText(/"name": "researcher"/)).toBeVisible()
  expect(pageErrors).toEqual([])
})

test('详情省略时 fail-closed，英文界面不会把空响应展示为成功', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 850 })
  const pageErrors = await mockChainTraceStudio(page, {
    language: 'en',
    directSnapshot: responsePage(true),
  })
  await page.getByRole('tab', { name: 'Trace' }).click()
  await page.getByRole('button', { name: 'Search trace nodes' }).click()
  await page.getByRole('searchbox', { name: 'Search trace nodes' })
    .fill('4872')
  await expect.poll(async () => (await traceRequests(page)).snapshots.length).toBe(2)
  const details = page.locator('.chain-trace-details')
  await details.getByRole('tab', { name: 'Response' }).click()
  const notifications = page.getByRole('list', { name: 'System notifications' })
  await expect(notifications.getByRole('status')).toHaveText('Failed to load the complete response')
  await expect(page.getByText('Failed to load the complete response', { exact: true })).toHaveCount(1)
  await expect(details.getByRole('alert')).toHaveCount(0)
  await expect(details.getByRole('button', { name: 'Reload', exact: true })).toBeVisible()
  await expect(details.getByText('准备委派任务')).toHaveCount(0)
  expect(pageErrors).toEqual([])
})

test('全部模式按执行顺序等宽排列，并以一条竖线分隔每轮', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  const pageErrors = await mockChainTraceStudio(page)
  await page.getByRole('tab', { name: '链路' }).click()

  const details = page.locator('.chain-trace-details')
  await expect(details).toBeVisible()
  await details.getByRole('button', { name: '关闭链路详情' }).click()

  await expect(page.getByRole('region', { name: '执行序列', exact: true })).toBeVisible()
  const summary = page.locator('.chain-trace-range-summary')
  await expect(summary).toContainText('共 2 轮')
  await expect(summary).toContainText(`${nodes.length} 节点`)
  await expect(page.locator('.chain-trace-ticks')).toHaveCount(0)

  const blocks = page.locator('.chain-trace-sequence-block')
  await expect(blocks).toHaveCount(nodes.length)
  const widths = await blocks.evaluateAll((elements) => (
    elements.map((element) => element.getBoundingClientRect().width)
  ))
  expect(Math.max(...widths) - Math.min(...widths)).toBeLessThanOrEqual(0.5)
  const sequenceEdges = await page.locator('.chain-trace-sequence-grid').evaluate((grid) => {
    const items = [...grid.querySelectorAll<HTMLElement>('.chain-trace-sequence-block')]
    const gridRect = grid.getBoundingClientRect()
    const chartRect = grid.parentElement!.getBoundingClientRect()
    const itemRects = items.map((item) => item.getBoundingClientRect())
    const boundaryRect = grid.querySelector<HTMLElement>(
      '.chain-trace-sequence-turn-boundary',
    )!.getBoundingClientRect()
    const previousRect = itemRects
      .filter((rect) => rect.right <= boundaryRect.left)
      .reduce((nearest, rect) => rect.right > nearest.right ? rect : nearest)
    const nextRect = itemRects
      .filter((rect) => rect.left > boundaryRect.right)
      .reduce((nearest, rect) => rect.left < nearest.left ? rect : nearest)
    return {
      left: Math.min(...itemRects.map((rect) => rect.left)) - chartRect.left,
      beforeBoundary: boundaryRect.left - previousRect.right,
      afterBoundary: nextRect.left - boundaryRect.right,
      gridLeft: Math.abs(Math.min(...itemRects.map((rect) => rect.left)) - gridRect.left),
      right: chartRect.right - Math.max(...itemRects.map((rect) => rect.right)),
      gridRight: Math.abs(Math.max(...itemRects.map((rect) => rect.right)) - gridRect.right),
    }
  })
  expect(sequenceEdges.left).toBeCloseTo(1, 1)
  expect(sequenceEdges.beforeBoundary).toBeCloseTo(1, 1)
  expect(sequenceEdges.afterBoundary).toBeCloseTo(1, 1)
  expect(sequenceEdges.right).toBeCloseTo(1, 1)
  expect(sequenceEdges.gridLeft).toBeLessThanOrEqual(0.5)
  expect(sequenceEdges.gridRight).toBeLessThanOrEqual(0.5)

  const boundaries = page.locator('.chain-trace-sequence-turn-boundary')
  await expect(boundaries).toHaveCount(1)
  const separation = await page.evaluate(() => {
    const previous = document.querySelector<HTMLElement>(
      '[data-trace-sequence-node-id="old-assistant"]',
    )!.getBoundingClientRect()
    const next = document.querySelector<HTMLElement>(
      '[data-trace-sequence-node-id="human-current"]',
    )!.getBoundingClientRect()
    const boundary = document.querySelector<HTMLElement>(
      '.chain-trace-sequence-turn-boundary',
    )!.getBoundingClientRect()
    const grid = document.querySelector<HTMLElement>(
      '.chain-trace-sequence-grid',
    )!.getBoundingClientRect()
    return {
      previousRight: previous.right,
      nextLeft: next.left,
      boundaryX: boundary.x,
      boundaryWidth: boundary.width,
      boundaryTop: boundary.top,
      boundaryBottom: boundary.bottom,
      gridTop: grid.top,
      gridBottom: grid.bottom,
    }
  })
  expect(separation.boundaryX).toBeGreaterThan(separation.previousRight)
  expect(separation.boundaryX + separation.boundaryWidth)
    .toBeLessThan(separation.nextLeft)
  expect(Math.abs(
    separation.boundaryX + separation.boundaryWidth / 2
      - (separation.previousRight + separation.nextLeft) / 2,
  )).toBeLessThanOrEqual(1)
  expect(Math.abs(separation.boundaryTop - separation.gridTop)).toBeLessThanOrEqual(1)
  expect(Math.abs(separation.boundaryBottom - separation.gridBottom)).toBeLessThanOrEqual(1)

  await page.locator('[data-trace-sequence-node-id="human-current"]').click()
  await expect(page.getByRole('region', { name: '调用时间线' })).toBeVisible()
  await expect(summary).toContainText('第 2 轮')
  await expect(summary).toContainText(
    `${nodes.filter((node) => node.turnId === 'turn-browser-2').length} 节点`,
  )
  await expect(page.locator('.chain-trace-ticks')).toBeVisible()
  expect(pageErrors).toEqual([])
})

test('从链路页选择其他会话始终返回对话页', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 800 })
  const pageErrors = await mockChainTraceStudio(page)
  await page.getByRole('tab', { name: '链路' }).click()
  await page.getByText('另一个会话', { exact: true }).click()
  await expect(page).toHaveURL(new RegExp(`thread=${OTHER_THREAD_ID}`))
  await expect(page.getByRole('tab', { name: '对话' })).toHaveAttribute(
    'aria-selected',
    'true',
  )
  await expect(page.getByRole('tabpanel', { name: '链路' })).toHaveCount(0)
  expect(pageErrors).toEqual([])
})

async function verifyViewports(
  page: Page,
  theme: 'light' | 'dark',
) {
  for (const width of [320, 768, 1024, 1440]) {
    await page.setViewportSize({ width, height: 900 })
    await expect(page.locator('#chain-trace-panel')).toBeVisible()
    const details = page.locator('.chain-trace-details')
    if (width === 1440) {
    await expect(details).toBeVisible()
    const box = await details.boundingBox()
    expect(Math.round(box?.width ?? 0)).toBe(Math.min(width, 400))
    expect(Math.round((box?.x ?? 0) + (box?.width ?? 0))).toBe(width)
    const actionAlignment = await page.evaluate(() => {
      const search = document.querySelector<HTMLElement>('.chain-trace-search-control')!
        .getBoundingClientRect()
      const close = document.querySelector<HTMLElement>(
        '.chain-trace-details-header .ui-icon-button',
      )!.getBoundingClientRect()
      const searchIcon = document.querySelector<SVGElement>(
        '.chain-trace-search-trigger .ui-icon-button__icon svg',
      )!.getBoundingClientRect()
      const closeIcon = document.querySelector<SVGElement>(
        '.chain-trace-details-header .ui-icon-button__icon svg',
      )!.getBoundingClientRect()
      return {
        center: Math.abs(
          search.left + search.width / 2 - (close.left + close.width / 2),
        ),
        left: Math.abs(searchIcon.left - closeIcon.left),
        right: Math.abs(searchIcon.right - closeIcon.right),
      }
    })
    expect(actionAlignment.center).toBeLessThanOrEqual(1)
    expect(actionAlignment.left).toBeLessThanOrEqual(0.5)
    expect(actionAlignment.right).toBeLessThanOrEqual(0.5)
    } else if (width === 320) {
      await expect(details).toBeVisible()
      expect(await details.boundingBox()).toEqual({ x: 0, y: 0, width, height: 900 })
      await expect(page.getByRole('button', { name: '返回链路' })).toBeFocused()
    } else {
      await expect(details).toBeHidden()
      await expect(page.getByRole('dialog', { name: '链路详情' })).toHaveCount(0)
    }
    const overflow = await page.evaluate(() => (
      document.documentElement.scrollWidth - window.innerWidth
    ))
    expect(overflow).toBeLessThanOrEqual(0)
    if (process.env.TINKERFIN_VISUAL_QA_DIR) {
      await page.screenshot({
        path: resolve(
          process.env.TINKERFIN_VISUAL_QA_DIR,
          `chain-trace-${theme}-${width}.png`,
        ),
        fullPage: true,
      })
    }
  }
}

test('链路详情随宿主自动隐藏并恢复，不隔离主区焦点', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.emulateMedia({ reducedMotion: 'reduce' })
  const pageErrors = await mockChainTraceStudio(page, { theme: 'dark' })
  await page.getByRole('tab', { name: '链路' }).click()
  await traceRow(page, 'failed-tool').click()
  await verifyViewports(page, 'dark')
  const details = page.getByRole('complementary', { name: '链路详情' })
  const handle = page.getByRole('separator', { name: '调整链路详情宽度' })
  await handle.press('End')
  await expect(handle).toHaveAttribute('aria-valuenow', '520')
  const host = page.locator('.chain-trace-content-grid')
  for (const [width, expected] of [[900, 380], [820, 300], [819, 0], [1179, 520]]) {
    await host.evaluate((element, width) => { (element as HTMLElement).style.width = `${width}px` }, width)
    if (expected) await expect(handle).toHaveAttribute('aria-valuenow', String(expected))
    else await expect(details).toBeHidden()
  }
  await host.evaluate(element => { (element as HTMLElement).style.removeProperty('width') })
  await page.setViewportSize({ width: 768, height: 900 })
  await expect(details).toBeHidden()
  await traceRow(page, 'failed-tool').click()
  await expect(page.getByText('展开窗口后可查看', { exact: true })).toBeVisible()
  await expect(page.locator('#root')).not.toHaveAttribute('aria-hidden')
  await expect(page.getByRole('dialog', { name: '链路详情' })).toHaveCount(0)
  await page.setViewportSize({ width: 1440, height: 900 })
  await expect(handle).toHaveAttribute('aria-valuenow', '520')
  await expect(details).toContainText('builtins.TimeoutError')
  await handle.press('Home')
  await expect(handle).toHaveAttribute('aria-valuenow', '300')
  await page.getByRole('tab', { name: '对话', exact: true }).click()
  await page.getByRole('tab', { name: '链路', exact: true }).click()
  await expect(handle).toHaveAttribute('aria-valuenow', '300')
  await page.getByRole('button', { name: '关闭链路详情' }).click()
  await page.setViewportSize({ width: 320, height: 900 })
  await page.setViewportSize({ width: 1440, height: 900 })
  await expect(details).toBeHidden()
  expect(pageErrors).toEqual([])
})

test('浅色四视口不产生页面溢出', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  const pageErrors = await mockChainTraceStudio(page, { theme: 'light' })
  await page.getByRole('tab', { name: '链路' }).click()
  await verifyViewports(page, 'light')
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light')
  expect(pageErrors).toEqual([])
})

test('窄屏首次进入滚到底，触控搜索与详情关闭图标同轴', async ({ page }) => {
  await page.setViewportSize({ width: 768, height: 900 })
  const cdp = await page.context().newCDPSession(page)
  await cdp.send('Emulation.setTouchEmulationEnabled', { enabled: true, maxTouchPoints: 1 })
  const pageErrors = await mockChainTraceStudio(page)
  await page.getByRole('tab', { name: '链路' }).click()
  const ledger = page.getByLabel('链路节点', { exact: true })
  await expect(ledger).toBeVisible()
  await expect.poll(() => ledger.evaluate((element) => (
    Math.abs(element.scrollHeight - element.clientHeight - element.scrollTop)
  ))).toBeLessThanOrEqual(1)
  await expect(page.getByRole('dialog', { name: '链路详情' })).toHaveCount(0)

  for (const width of [320, 768, 1024, 1440]) {
    await page.setViewportSize({ width, height: 900 })
    await traceRow(page, 'failed-tool').click()
    if (width === 320) {
      await page.getByRole('button', { name: '返回链路' }).click()
      continue
    }
    if (width !== 1440) {
      await expect(page.getByRole('complementary', { name: '链路详情' })).toBeHidden()
      continue
    }
    const geometry = await page.evaluate(() => {
      const search = document.querySelector<SVGElement>(
        '.chain-trace-search-trigger .ui-icon-button__icon svg',
      )!.getBoundingClientRect()
      const close = document.querySelector<SVGElement>(
        '.chain-trace-details-header .ui-icon-button__icon svg',
      )!.getBoundingClientRect()
      return { left: Math.abs(search.left - close.left), right: Math.abs(search.right - close.right) }
    })
    expect(geometry.left).toBeLessThanOrEqual(0.5)
    expect(geometry.right).toBeLessThanOrEqual(0.5)
    await page.keyboard.press('Escape')
  }
  expect(pageErrors).toEqual([])
})

test('序列选择收起的子节点会展开所属作用域并在关闭详情后恢复焦点', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  const pageErrors = await mockChainTraceStudio(page)
  await page.getByRole('tab', { name: '链路' }).click()
  await page.getByRole('button', { name: '关闭链路详情' }).click()
  const outer = traceRow(page, 'subagent-outer').locator('..')
    .getByRole('button', { name: /^收起子智能体/ })
  await outer.click()
  await expect(traceRow(page, 'inner-assistant')).toBeHidden()
  await page.getByRole('region', { name: '执行序列' })
    .getByRole('button', { name: /选择 助手，嵌套核验完成/ }).click()
  await expect(traceRow(page, 'inner-assistant')).toBeVisible()
  await page.getByRole('button', { name: '关闭链路详情' }).click()
  await expect(traceRow(page, 'inner-assistant')).toBeFocused()
  expect(pageErrors).toEqual([])
})

test('错误详情将请求入口与错误摘要紧凑并排', async ({ page }) => {
  await mockChainTraceStudio(page)
  await page.getByRole('tab', { name: '链路', exact: true }).click()
  for (const theme of ['light', 'dark']) {
    await page.evaluate(value => { document.documentElement.dataset.theme = value }, theme)
    for (const width of [320, 768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 900 })
      const close = page.getByRole('button', { name: width === 320 ? '返回链路' : '关闭链路详情' })
      if (await close.isVisible()) await close.click()
      await traceRow(page, 'failed-tool').click()
      if (width !== 320 && width !== 1440) {
        await expect(page.getByRole('complementary', { name: '链路详情' })).toBeHidden()
        continue
      }
      const panel = page.getByRole('region', { name: '错误详情' })
      await expect(panel).toContainText('搜索服务在期限内未响应')
      const request = panel.getByRole('button', { name: '查看请求' })
      const titleBox = await panel.getByText('builtins.TimeoutError', { exact: true }).boundingBox()
      const actionBox = await request.boundingBox()
      expect(actionBox!.x).toBeGreaterThan(titleBox!.x + titleBox!.width)

      await request.click()
      await expect(page.getByRole('tab', { name: '请求', exact: true })).toHaveAttribute('aria-selected', 'true')
      await close.click()
    }
  }
})

for (const theme of ['light', 'dark'] as const) {
  for (const language of ['zh-CN', 'en'] as const) {
    test(`移动链路全屏详情返回列表并保留滚动 ${theme} ${language}`, async ({ page }, testInfo) => {
      await page.setViewportSize({ width: 320, height: 800 })
      await page.emulateMedia({ reducedMotion: 'reduce' })
      const cdp = await page.context().newCDPSession(page)
      await cdp.send('Emulation.setTouchEmulationEnabled', { enabled: true, maxTouchPoints: 1 })
      const errors = await mockChainTraceStudio(page, { theme, language })
      const english = language === 'en'
      await page.getByRole('tab', { name: english ? 'Trace' : '链路', exact: true }).click()
      const detail = page.getByRole('complementary', { name: english ? 'Trace details' : '链路详情' })
      const ledger = page.getByLabel(english ? 'Trace nodes' : '链路节点', { exact: true })
      await expect(detail).toBeHidden()
      const node = traceRow(page, 'model-current')
      await node.scrollIntoViewIfNeeded()
      const top = await ledger.evaluate(element => element.scrollTop)
      await node.click()
      const back = page.getByRole('button', { name: english ? 'Back to trace' : '返回链路' })
      await expect(back).toBeFocused()
      expect(await detail.boundingBox()).toEqual({ x: 0, y: 0, width: 320, height: 800 })
      await expect(ledger).toBeHidden()
      await expect(page.getByRole('tab', { name: english ? 'Conversation' : '对话', exact: true })).toBeHidden()
      await expect(page.getByRole('separator')).toHaveCount(0)
      await expect(page.getByRole('dialog')).toHaveCount(0)
      await detail.getByRole('tab', { name: english ? 'Request' : '请求', exact: true }).click()
      await expect(detail).toContainText('浏览器系统提示词')
      await page.screenshot({ path: testInfo.outputPath(`mobile-trace-${theme}-${language}-320.png`) })
      await back.click()
      await expect(node).toBeFocused()
      await expect.poll(() => ledger.evaluate(element => element.scrollTop)).toBe(top)
      await node.press('Enter')
      await detail.getByRole('tab', { name: english ? 'Request' : '请求', exact: true }).click()
      await page.setViewportSize({ width: 767, height: 800 })
      expect(await detail.boundingBox()).toEqual({ x: 0, y: 0, width: 767, height: 800 })
      await page.setViewportSize({ width: 768, height: 800 })
      await expect(detail).toBeHidden()
      await expect(ledger).toBeVisible()
      await page.setViewportSize({ width: 1440, height: 800 })
      await expect(detail.getByRole('tab', { name: english ? 'Request' : '请求', exact: true })).toHaveAttribute('aria-selected', 'true')
      await expect(page.getByRole('separator')).toHaveAttribute('aria-valuenow', '400')
      await page.setViewportSize({ width: 320, height: 800 })
      await back.click()
      await traceRow(page, 'failed-tool').click()
      await expect(detail).toContainText('builtins.TimeoutError')
      await page.keyboard.press('Escape')
      await expect(traceRow(page, 'failed-tool')).toBeFocused()
      await expect(page.getByText(/展开窗口后可查看|Expand the window to view/)).toHaveCount(0)
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
      expect(errors).toEqual([])
    })
  }
}
