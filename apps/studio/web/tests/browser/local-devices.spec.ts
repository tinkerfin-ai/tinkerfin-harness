import { join } from 'node:path'
import { expect, test, type Page } from '@playwright/test'
import type { ConversationHistoryDetail } from '../../src/api/conversation/history'
import type { ChatRequestPayload, ConversationAgUiEvent, InterruptEvent } from '../../src/api/conversation/types'
import type { LocalDevice } from '../../src/features/devices/api'
import { traceGraphNode, traceGraphWithNodes } from '../../src/test/traceFixtures'
import { rootToolId, toolReviewInterrupts } from '../../src/test/aguiFixtures'
import { installNotificationStream } from './fixtures/notifications'
import { installProjectScope } from './fixtures/projects'

const time = '2030-01-01T00:00:00Z'
const threadId = 'local-collaboration'
const toolId = rootToolId('write')
const fileArgs = { file_path: '/Users/test/reports/summary.md', content: '# 项目报告汇总\n\n已整理研究结论和下一步事项' }
const interrupts: InterruptEvent[] = toolReviewInterrupts('parent-review', [{
  toolCallId: toolId, name: 'write_file', args: fileArgs,
  description: '写入报告汇总\n\n在已授权的报告目录中创建 summary.md',
}])
const user = { user_id: 1, username: 'local-device-test', avatar_url: null, roles: [], disabled: false }
const device: LocalDevice = {
  device_id: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', name: '设计用 MacBook Pro', online: false, revoked: false,
  paired_at: time, last_seen_at: time, capabilities: { browser: true, mcp: false, desktop: false, desktop_capture: false, open_url: true },
}
const history: ConversationHistoryDetail = {
  id: 1, projectId: 'project-1', threadId, title: '整理本机报告', titleSource: 'user', titleSeq: 1, titleGenerationStatus: 'idle',
  archived: false, pinned: false, lastModel: 'main', accessMode: 'write_approval',
  asOfSeq: 5, generation: 'generation', observedAt: time, headRunId: 'cloud-run', availableHeads: ['cloud-run'], historyCursor: null,
  messageCount: 1, toolCallCount: 1, messages: [{ id: 'question', agui: null, traceSeq: 1, runId: 'cloud-run', graphNamespace: [], role: 'user',
    content: '整理我 Mac 上授权目录中的报告，生成一个汇总文件', contentOmitted: false, status: 'completed', createdAt: time, completedAt: time }],
  reasoning: [], graph: traceGraphWithNodes([
    traceGraphNode({ id: 'local-write', agui: { kind: 'tool', toolCallId: toolId }, sourceId: 'write',
      name: 'write_file', runId: 'cloud-run', graphNamespace: [], status: 'waiting', startedSeq: 4, updatedSeq: 5,
      startedAt: time, completedAt: null, request: fileArgs }),
  ], 5), state: { root: {}, subgraphs: {} },
  interactions: [{ agui: interrupts, id: 'local-approval', traceSeq: 5, sourceId: 'parent-review', graphNamespace: [],
    runId: 'cloud-run', kind: 'tool_approval', toolCallIds: [toolId], status: 'pending', payloadOmitted: false,
    payload: interrupts[0].metadata?.langgraphValue, openedAt: time, resolvedAt: null }],
  interactionAvailability: [{ interruptId: 'parent-review', state: 'available', submissionRunId: null }], submissionResult: null,
  planResults: [], runFailures: [], status: { execution: 'waiting', headRunId: 'cloud-run' },
  completeness: { missingPrefix: false, missingTail: false, payloadOmitted: false }, taskTrace: { status: 'ready', todoGroups: [] }, createdAt: time, updatedAt: time,
}

declare global {
  interface Window {
    localConversationStream: {
      ready: Promise<ChatRequestPayload>
      requests: ChatRequestPayload[]
      finish: (events: ConversationAgUiEvent[]) => void
    }
  }
}

/** 续跑保持开放，只有用例显式发送终态后才结束主会话流 */
async function installResumeStream(page: Page) {
  await page.addInitScript(({ threadId, initialSequence }) => {
    const originalFetch = window.fetch
    let publish!: (chunk: string) => void
    let finish!: () => void
    let received!: (payload: ChatRequestPayload) => void
    let sequence = initialSequence
    const encode = (events: ConversationAgUiEvent[]) => events.map(event => 'id: ' + (++sequence) + '\ndata: ' + JSON.stringify(event) + '\n\n').join('')
    window.localConversationStream = {
      ready: new Promise<ChatRequestPayload>(resolve => { received = resolve }), requests: [],
      finish(events) { publish(encode(events)); finish() },
    }
    window.fetch = async (input, init) => {
      const request = new Request(input, init)
      if (new URL(request.url).pathname !== '/api/conversation/chat') return originalFetch(input, init)
      const payload: ChatRequestPayload = await request.json()
      window.localConversationStream.requests.push(payload)
      const encoder = new TextEncoder()
      let cleanup!: () => void
      return new Response(new ReadableStream<Uint8Array>({
        start(controller) {
          const abort = () => { cleanup(); controller.error(new DOMException('Aborted', 'AbortError')) }
          cleanup = () => request.signal.removeEventListener('abort', abort)
          publish = chunk => controller.enqueue(encoder.encode(chunk))
          finish = () => { cleanup(); controller.close() }
          request.signal.addEventListener('abort', abort, { once: true })
          publish(encode([{ type: 'RUN_STARTED', threadId, runId: payload.runId }]))
          received(payload)
        },
        cancel() { cleanup() },
      }), { headers: { 'Content-Type': 'text/event-stream' } })
    }
  }, { threadId, initialSequence: history.asOfSeq })
}

async function prepare(page: Page, theme: string, language = 'zh-CN', deviceName = device.name) {
  let currentHistory = structuredClone(history)
  const cancellations: string[] = []
  const unexpectedRequests: string[] = []
  await page.clock.setFixedTime(new Date(time))
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.addInitScript(({ user, theme, language }) => {
    localStorage.setItem('tinkerfin.auth.session', JSON.stringify({ token: 'isolated-local-test', serverAddress: 'http://127.0.0.1:8090', tokenType: 'Bearer', expiresAt: '2099-01-01T00:00:00Z', user }))
    localStorage.setItem('tinkerfin:theme', theme)
    localStorage.setItem('tinkerfin:language', language)
  }, { user, theme, language })
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url())
    const path = url.pathname
    let data: unknown = {}
    if (path === '/api/auth/me') data = { user, expires_at: '2099-01-01T00:00:00Z' }
    else if (path === '/api/models') data = { items: [{ modelId: 'main', displayName: '主模型', connectionId: 'provider', connectionDisplayName: '测试模型', reasoningEnabled: true, isDefault: true }], defaultModelId: 'main' }
    else if (path === '/api/skills/installations') data = []
    else if (path === '/api/conversation/config') data = { dayRanges: [7, 30] }
    else if (path === '/api/conversation/history') data = { items: [{ ...currentHistory, status: currentHistory.status.execution,
      lastRunId: currentHistory.headRunId, hasPendingInterrupt: currentHistory.status.execution === 'waiting',
      pendingInteractionKind: currentHistory.status.execution === 'waiting' ? 'tool_approval' : null }], nextCursor: null }
    else if (path.endsWith('/history')) data = { ...currentHistory, taskTrace: url.searchParams.get('includeTaskTrace') === 'false' ? null : currentHistory.taskTrace }
    else if (path === '/api/devices') data = [{ ...device, name: deviceName }]
    else if (path.startsWith('/api/conversation/' + threadId + '/runs/') && path.endsWith('/cancel')) {
      expect(route.request().method()).toBe('POST')
      cancellations.push(path)
      data = { cancelled: true }
    }
    else if (path.startsWith('/api/models/input-capabilities')) data = { image_input_capability: { automatic: 'unknown', effective: 'unknown', source: 'unknown' } }
    else unexpectedRequests.push(path)
    await route.fulfill({ json: { code: 0, message: 'success', data } })
  })
  await installProjectScope(page)
  await page.route('**/api/devices/selection?*', route => route.fulfill({ json: { code: 0, message: 'success', data: { device_id: device.device_id, workspace_id: 'project-1' } } }))
  await installNotificationStream(page, true, false)
  await installResumeStream(page)
  await page.goto('/?project=project-1&thread=' + threadId)
  return { cancellations, unexpectedRequests, complete: async (runId: string, cancelled: boolean) => {
    const events: ConversationAgUiEvent[] = cancelled
      ? [{ type: 'RUN_ERROR', code: 'cancelled', message: 'Cancelled' }]
      : [{ type: 'RUN_FINISHED', threadId, runId, outcome: { type: 'success' } }]
    const asOfSeq = history.asOfSeq + 1 + events.length
    currentHistory = { ...currentHistory, headRunId: runId, availableHeads: [runId], asOfSeq,
      status: { execution: cancelled ? 'cancelled' : 'succeeded', headRunId: runId },
      graph: { ...currentHistory.graph, asOfSeq, nodes: currentHistory.graph.nodes.map(node => ({ ...node,
        status: cancelled ? 'cancelled' : 'succeeded', completedAt: time, updatedSeq: asOfSeq })) },
      interactions: currentHistory.interactions.map(item => ({ ...item, status: 'resolved', resolvedAt: time })),
      interactionAvailability: [{ interruptId: 'parent-review', state: 'resolved', submissionRunId: runId }],
    }
    await page.evaluate((serialized: string) => window.localConversationStream.finish(JSON.parse(serialized)), JSON.stringify(events))
    await page.evaluate(thread => window.emitResourceChange('studio.conversation.interactions.changed', thread), threadId)
  } }
}

for (const theme of ['light', 'dark']) for (const width of [320, 768, 1024, 1440]) {
  test('本地工具审批和设备设置 ' + theme + ' ' + width, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 960 })
    if (width === 320) {
      const cdp = await page.context().newCDPSession(page)
      await cdp.send('Emulation.setTouchEmulationEnabled', { enabled: true, maxTouchPoints: 1 })
    }
    const language = theme === 'dark' ? 'en' : 'zh-CN'
    const { cancellations, unexpectedRequests, complete } = await prepare(page, theme, language)
    const approve = page.getByRole('button', { name: language === 'en' ? 'Allow' : '允许', exact: true })
    await expect(approve).toBeVisible()
    await expect(page.getByRole('region', { name: language === 'en' ? 'Waiting for approval' : '等待审批', exact: true })).toBeVisible()
    await expect(page.getByRole('heading', { name: '写入报告汇总', exact: true })).toBeVisible()
    const target = page.getByRole('button', { name: language === 'en' ? 'Local · Disconnected' : '本地 · 未连接', exact: true })
    await expect(target).toHaveAttribute('aria-pressed', 'true')
    await expect(target.locator('svg')).toHaveCount(1)
    await expect(page.getByRole('button', { name: language === 'en' ? 'Configure' : '配置', exact: true })).toHaveCount(0)
    await expect(page.getByRole('button', { name: language === 'en' ? 'Cloud' : '云端', exact: true })).toBeDisabled()
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    const path = (name: string) => process.env.TINKERFIN_UI_EVIDENCE_DIR
      ? join(process.env.TINKERFIN_UI_EVIDENCE_DIR, name + '-' + theme + '-' + width + '.png') : testInfo.outputPath(name + '-' + theme + '-' + width + '.png')
    await page.screenshot({ path: path('local-task') })
    await approve.click()
    const command = await page.evaluate(() => window.localConversationStream.ready)
    expect(command.threadId).toBe(threadId)
    expect(command.messages).toEqual([])
    expect(command.resume).toEqual([{ interruptId: 'parent-review', status: 'resolved', payload: { type: 'approve' } }])
    expect(command.forwardedProps.projectId).toBe('project-1')
    const confirmation = page.getByRole('region', { name: language === 'en' ? 'Submission confirmation' : '提交确认', exact: true })
    await expect(confirmation).toBeVisible()
    const stop = page.getByRole('button', { name: language === 'en' ? 'Stop task' : '停止任务', exact: true })
    await expect(stop).toBeEnabled()
    const cancelled = theme === 'dark'
    if (cancelled) {
      await stop.click()
      await expect.poll(() => cancellations).toEqual(['/api/conversation/' + threadId + '/runs/' + command.runId + '/cancel'])
      await expect(page.getByRole('button', { name: 'Stopping task', exact: true })).toBeDisabled()
    }
    await complete(command.runId, cancelled)
    await expect(page.getByRole('textbox', { name: language === 'en' ? 'Message input' : '消息输入', exact: true })).toBeVisible()
    await expect(confirmation).toHaveCount(0)
    expect(await page.evaluate(() => window.localConversationStream.requests.length)).toBe(1)
    expect(cancellations).toHaveLength(cancelled ? 1 : 0)
    expect(unexpectedRequests).toEqual([])
    await expect(page.getByText(language === 'en' ? 'Local tasks' : '本机任务', { exact: true })).toHaveCount(0)
    const userMenu = page.getByRole('button', { name: language === 'en' ? 'Open user menu' : '打开用户菜单', exact: true })
    if (!await userMenu.isVisible()) await page.getByRole('button', { name: language === 'en' ? (width < 768 ? 'Open navigation' : 'Open sidebar') : (width < 768 ? '打开导航' : '打开侧边栏'), exact: true }).click()
    await userMenu.click()
    await page.getByRole('menuitem', { name: language === 'en' ? 'Settings' : '设置', exact: true }).click()
    await page.getByRole('navigation', { name: language === 'en' ? 'Settings categories' : '设置分类' }).getByRole('button', { name: language === 'en' ? 'Local devices' : '本地设备', exact: true }).click()
    await expect(page.getByRole('heading', { name: language === 'en' ? 'Local devices' : '本地设备' })).toBeVisible()
    const categories = page.getByRole('navigation', { name: language === 'en' ? 'Settings categories' : '设置分类' })
    await expect(categories.getByRole('button', { name: language === 'en' ? 'Local devices' : '本地设备', exact: true })).toBeInViewport({ ratio: 1 })
    if (width === 320) {
      const bounds = await categories.getByRole('button', { name: language === 'en' ? 'General' : '通用', exact: true }).boundingBox()
      expect(bounds!.width).toBeGreaterThanOrEqual(44)
      expect(bounds!.height).toBeGreaterThanOrEqual(44)
    }
    await expect(page.getByText(device.name, { exact: true })).toBeVisible()
    await expect(page.getByText(language === 'en' ? 'Managed automatically' : '自动管理')).toHaveCount(0)
    await expect(page.getByText('~/.tinkerfin/projects', { exact: true })).toHaveCount(0)
    const typography = await page.locator('.device-settings').evaluate(section => {
      const root = getComputedStyle(document.documentElement)
      const read = (selector: string) => Array.from(section.querySelectorAll(selector), element => {
        const style = getComputedStyle(element)
        return { size: style.fontSize, line: style.lineHeight }
      })
      return { title: read('h3'), text: read('strong, .device-settings__status'), actions: read('.ui-button--text'),
        expectedTitle: { size: root.getPropertyValue('--type-title-size').trim(), line: root.getPropertyValue('--type-title-line').trim() },
        expectedText: { size: root.getPropertyValue('--type-meta-size').trim(), line: root.getPropertyValue('--type-meta-line').trim() },
        expectedAction: { size: root.getPropertyValue('--type-meta-size').trim(), line: root.getPropertyValue('--type-ui-line').trim() } }
    })
    expect(typography.title).toEqual([typography.expectedTitle])
    expect(typography.text.length).toBeGreaterThan(0)
    for (const text of typography.text) expect(text).toEqual(typography.expectedText)
    for (const action of typography.actions) expect(action).toEqual(typography.expectedAction)
    const revoke = page.getByRole('button', { name: language === 'en' ? 'Revoke access' : '撤销授权', exact: true })
    await expect(revoke).toBeInViewport({ ratio: 1 })
    await expect(revoke).toHaveCSS('font-size', typography.expectedAction.size)
    await expect(revoke).toHaveCSS('cursor', 'pointer')
    const footerBounds = (await page.locator('.settings-panel__actions').boundingBox())!
    const layoutBounds = (await page.locator('.settings-layout').boundingBox())!
    expect(footerBounds.y + footerBounds.height).toBeCloseTo(layoutBounds.y + layoutBounds.height, 0)
    await testInfo.attach('device-settings-typography', { body: JSON.stringify(typography, null, 2), contentType: 'application/json' })
    const bounds = await page.getByRole('dialog').boundingBox()
    expect(bounds!.x).toBeGreaterThanOrEqual(0)
    expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(width)
    expect(await page.getByRole('region', { name: language === 'en' ? 'Settings' : '设置', exact: true }).evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true)
    await expect(page.locator('.settings-dialog')).toBeVisible()
    await page.screenshot({ path: path('local-devices') })
    await page.mouse.move(0, 0)
    await page.getByRole('dialog').press('Escape')
    if (!await target.isVisible()) await page.keyboard.press('Escape')
    await expect(target).toHaveAttribute('aria-pressed', 'true')
  })
}

for (const theme of ['light', 'dark']) for (const viewport of [{ width: 320, height: 360 }, { width: 622, height: 320 }]) {
  test('设备授权操作固定在设置底部 ' + theme + ' ' + viewport.width, async ({ page }, info) => {
    await page.setViewportSize(viewport)
    const name = '研究项目'.repeat(16)
    await prepare(page, theme, 'zh-CN', name)
    await page.goto('/?settings=devices')
    const revoke = page.getByRole('button', { name: '撤销授权', exact: true })
    await expect(revoke).toBeInViewport({ ratio: 1 })
    await expect(page.getByText(name, { exact: true })).toBeVisible()
    await page.screenshot({ path: info.outputPath('device-settings-fixed-actions.png') })
    await revoke.click()
    const confirm = page.getByRole('button', { name: '确认撤销设备', exact: true })
    await expect(confirm).toBeInViewport({ ratio: 1 })
    await expect(page.getByRole('group', { name: '撤销授权', exact: true })).toContainText(name)
    const footer = page.locator('.settings-panel__actions')
    const before = await footer.boundingBox()
    const content = page.getByRole('region', { name: '设置', exact: true })
    expect(await content.evaluate(element => element.scrollHeight > element.clientHeight)).toBe(true)
    await content.evaluate(element => { element.scrollTop = element.scrollHeight })
    expect(await footer.boundingBox()).toEqual(before)
    await expect(confirm).toBeInViewport({ ratio: 1 })
    await page.screenshot({ path: info.outputPath('device-settings-revoke-confirmation.png') })
    await page.getByRole('button', { name: '取消', exact: true }).click()
    await expect(revoke).toBeInViewport({ ratio: 1 })
  })
}
