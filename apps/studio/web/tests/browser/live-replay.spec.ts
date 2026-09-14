import { createServer, type ServerResponse } from 'node:http'
import { expect, test } from '@playwright/test'
import { emptyTraceGraph } from '../../src/test/traceFixtures'

const threadId = 'thread-replay'
const runId = 'run-replay'
const user = { user_id: 1, username: 'replay', display_name: '续播验收', avatar_url: null, roles: [], disabled: false }
const restoredText = '已收到的长回复应该立即恢复，不应从第一个字重新播放。'.repeat(20)
const newText = '新的内容继续逐字显示'
const timestamp = '2026-09-14T00:00:00.000Z'

/** 持续保持连接，用可控信号推进第二段输出 */
test('刷新和无缓存新标签页在后台暂停时恢复正文，随后继续增量', async ({ page, context }, testInfo) => {
  const readers = new Set<ServerResponse>()
  const pendingReaders: ServerResponse[] = []
  let complete = false
  let posts = 0
  let attachments = 0
  const detail = (baseline = false) => ({
    id: 1, threadId, title: '续播验收', titleSource: 'default', titleGenerationStatus: 'idle', titleSeq: 0,
    accessMode: 'write_approval', lastModel: 'main', pinned: false, asOfSeq: complete && !baseline ? 10 : 2,
    generation: 'replay-test', observedAt: timestamp, headRunId: runId, availableHeads: [runId], historyCursor: null,
    runFailures: [], messageCount: complete && !baseline ? 1 : 0, toolCallCount: 0,
    messages: complete && !baseline ? [{
      id: 'answer', role: 'assistant', runId, graphNamespace: [], inSubagentScope: false,
      content: restoredText + newText, status: 'completed', createdAt: timestamp, updatedAt: timestamp,
      agui: { kind: 'message', messageId: 'answer' },
    }] : [],
    reasoning: [], graph: emptyTraceGraph(complete && !baseline ? 10 : 2),
    state: { root: {}, subgraphs: {} }, interactions: [],
    status: { execution: complete && !baseline ? 'succeeded' : 'running', headRunId: runId },
    completeness: { missingPrefix: false, missingTail: false, payloadOmitted: false },
    taskTrace: { status: 'ready', todoGroups: [] }, createdAt: timestamp, updatedAt: timestamp,
  })
  const event = (response: ServerResponse, seq: number, value: object) => response.write(`id: ${seq}\n${seq <= 281 ? 'event: replay\n' : ''}data: ${JSON.stringify(value)}\n\n`)
  const sendPendingReplay = () => {
    for (const response of pendingReaders.splice(0)) {
      response.write(`event: snapshot\ndata: ${JSON.stringify({ type: 'snapshot', snapshot: detail(true), replay: true })}\n\n`)
      event(response, 279, { type: 'RUN_STARTED', threadId, runId })
      event(response, 280, { type: 'TEXT_MESSAGE_START', messageId: 'answer', role: 'assistant' })
      event(response, 281, { type: 'TEXT_MESSAGE_CONTENT', messageId: 'answer', delta: restoredText })
    }
  }
  const server = createServer((request, response) => {
    response.writeHead(200, { 'Content-Type': 'text/event-stream', 'Access-Control-Allow-Origin': '*', 'Cache-Control': 'no-cache' })
    response.flushHeaders()
    attachments += 1
    readers.add(response)
    pendingReaders.push(response)
    response.on('close', () => readers.delete(response))
    request.on('error', () => response.destroy())
  })
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve))
  const address = server.address()
  if (!address || typeof address === 'string') throw new Error('Missing replay server address')
  await context.addInitScript(({ user }) => {
    localStorage.setItem('tinkerfin.auth.session', JSON.stringify({ token: 'browser-token', tokenType: 'Bearer', expiresAt: '2099-01-01T00:00:00Z', user }))
    sessionStorage.clear()
  }, { user })
  await context.route('**/api/**', async route => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (request.method() === 'POST') posts += 1
    if (path.endsWith(`/runs/${runId}/events`)) {
      expect(request.headers()['last-event-id']).toBeUndefined()
      await route.continue({ url: `http://127.0.0.1:${address.port}/events` })
      return
    }
    let data: unknown = {}
    if (path === '/api/auth/me') data = { expires_at: '2099-01-01T00:00:00Z', user }
    else if (path === '/api/models') data = { items: [{ modelId: 'main', displayName: 'Main', imageSupport: 'supported', reasoningEnabled: false, isDefault: true }], defaultModelId: 'main' }
    else if (path === '/api/conversation/config') data = { dayRanges: [7, 30] }
    else if (path === '/api/conversation/history') data = { items: [], nextCursor: null }
    else if (path.endsWith('/history')) data = detail()
    await route.fulfill({ json: { code: 0, message: 'success', data } })
  })
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  await page.clock.install()
  try {
    await page.goto(`/?thread=${threadId}`)
    await expect(page.getByRole('textbox', { name: '消息输入' })).toBeVisible()
    await expect.poll(() => attachments).toBe(1)
    await page.clock.pauseAt(new Date())
    sendPendingReplay()
    await expect(page.getByText(restoredText, { exact: true })).toBeVisible()
    await page.clock.resume()
    await page.reload()
    await expect(page.getByRole('textbox', { name: '消息输入' })).toBeVisible()
    await expect.poll(() => attachments).toBe(2)
    await page.clock.pauseAt(new Date())
    sendPendingReplay()
    await expect(page.getByText(restoredText, { exact: true })).toBeVisible()
    await page.clock.resume()
    const other = await context.newPage()
    other.on('pageerror', error => errors.push(error.message))
    await other.goto(`/?thread=${threadId}`)
    await expect(other.getByRole('textbox', { name: '消息输入' })).toBeVisible()
    await expect.poll(() => attachments).toBe(3)
    await other.clock.pauseAt(new Date())
    sendPendingReplay()
    await expect(other.getByText(restoredText, { exact: true })).toBeVisible()
    expect(complete).toBe(false)
    expect(posts).toBe(0)
    expect(attachments).toBe(3)
    await page.screenshot({ path: testInfo.outputPath('replay-paused.png') })
    complete = true
    for (const response of readers) {
      event(response, 282, { type: 'TEXT_MESSAGE_CONTENT', messageId: 'answer', delta: newText })
      event(response, 283, { type: 'TEXT_MESSAGE_END', messageId: 'answer' })
      event(response, 284, { type: 'RUN_FINISHED', threadId, runId, outcome: { type: 'success' } })
      response.end()
    }
    for (const target of [page, other]) {
      await expect(target.getByRole('button', { name: '停止任务', exact: true })).toHaveCount(0)
      await expect(target.getByText(restoredText, { exact: true })).toBeVisible()
    }
    // 浏览器上下文中的标签页共用虚拟时钟，每次只推进一次
    await page.clock.runFor(16)
    for (const target of [page, other]) {
      await expect(target.getByText(restoredText + newText[0], { exact: true })).toBeVisible()
    }
    await page.clock.runFor(newText.length * 16)
    for (const target of [page, other]) {
      await expect(target.getByText(restoredText + newText, { exact: true })).toHaveCount(1)
    }
    expect(posts).toBe(0)
    expect(errors).toEqual([])
  } finally {
    for (const response of readers) response.destroy()
    server.closeAllConnections()
    await new Promise<void>((resolve, reject) => server.close(error => error ? reject(error) : resolve()))
  }
})
