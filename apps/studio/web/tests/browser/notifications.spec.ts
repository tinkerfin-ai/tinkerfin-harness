import { expect, test, type Page } from '@playwright/test'
import type { ConversationHistoryDetail } from '../../src/api/conversation/history'

declare global {
  interface Window {
    notificationTest: {
      emit: (topic: string, key: string) => void
      disconnect: () => void
      visibility: (hidden: boolean) => void
      active: () => number
      connections: () => number
    }
    readNotificationApi: (request: { path: string; method: string; body: string; authorization: string | null }) => Promise<{ data?: unknown; stream?: string }>
  }
}

function conversation(threadId: string, runId: string, title: string): ConversationHistoryDetail {
  return {
    id: 1, threadId, title, titleSource: 'generated', titleGenerationStatus: 'succeeded', titleSeq: 1,
    accessMode: 'full', lastModel: 'main', pinned: false, createdAt: '2030-01-01T00:00:00Z', updatedAt: '2030-01-01T00:00:00Z',
    generation: 'test-generation', asOfSeq: 5, observedAt: '2030-01-01T00:00:00.000000Z', headRunId: runId, availableHeads: [runId], historyCursor: null,
    messageCount: 0, toolCallCount: 0, messages: [], reasoning: [], runFailures: [], state: { root: {}, subgraphs: {} }, interactions: [],
    status: { execution: 'succeeded', headRunId: runId }, completeness: { missingPrefix: false, missingTail: false, payloadOmitted: false },
    taskTrace: { status: 'ready', todoGroups: [] },
    graph: { asOfSeq: 5, turns: [], nodes: [], orderedNodeIds: [], matchedNodeIds: [], completeness: { callTrackingMissing: false, relationshipEvidenceMissing: false, detailsOmitted: false } },
  }
}

async function prepare(page: Page) {
  await page.clock.install({ time: new Date('2030-01-01T00:00:00Z') })
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.addInitScript(() => {
    const user = { user_id: 1, username: 'notifications', display_name: '通知验收', avatar_url: null, roles: [], disabled: false }
    localStorage.setItem('tinkerfin.auth.session', JSON.stringify({ token: 'browser-token', serverAddress: 'http://127.0.0.1:8090', tokenType: 'Bearer', expiresAt: '2099-01-01T00:00:00Z', user }))
    const original = window.fetch
    const readers = new Set<ReadableStreamDefaultController<Uint8Array>>()
    let connections = 0
    let hidden = false
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden })
    const encoder = new TextEncoder()
    window.notificationTest = {
      emit: (topic, key) => {
        const payload = { scope: { namespace: 'ns_1', owner_id: null }, topic, key, details: {} }
        for (const reader of readers) reader.enqueue(encoder.encode(`event: change\ndata: ${JSON.stringify(payload)}\n\n`))
      },
      disconnect: () => { for (const reader of readers) reader.close(); readers.clear() },
      visibility: value => { hidden = value; document.dispatchEvent(new Event('visibilitychange')) },
      active: () => readers.size,
      connections: () => connections,
    }
    window.fetch = async (input, init) => {
      const request = new Request(input, init)
      const path = new URL(request.url).pathname
      if (!path.startsWith('/api/')) return original(input, init)
      if (path === '/api/notifications') {
        connections += 1
        let detach: () => void
        return new Response(new ReadableStream<Uint8Array>({
          start(reader) {
            readers.add(reader)
            const abort = () => { if (readers.delete(reader)) reader.close(); detach() }
            detach = () => { readers.delete(reader); request.signal.removeEventListener('abort', abort) }
            request.signal.addEventListener('abort', abort, { once: true })
            reader.enqueue(encoder.encode('event: ready\ndata: {}\n\n'))
          },
          cancel() { detach() },
        }), { headers: { 'Content-Type': 'text/event-stream' } })
      }
      const result = await window.readNotificationApi({ path, method: request.method, body: request.method === 'GET' ? '' : await request.text(), authorization: request.headers.get('Authorization') })
      return result.stream === undefined
        ? new Response(JSON.stringify({ code: 0, message: 'success', data: result.data }), { headers: { 'Content-Type': 'application/json' } })
        : new Response(result.stream, { headers: { 'Content-Type': 'text/event-stream' } })
    }
  })
}

test('两个独立窗口通过通知同步会话，断连和隐藏期间的变化由基线恢复', async ({ browser, baseURL }) => {
  const contexts = await Promise.all([browser.newContext(), browser.newContext()])
  const pages = await Promise.all(contexts.map(context => context.newPage()))
  const threads = new Map<string, ConversationHistoryDetail>()
  const errors: string[] = []
  const broadcast = (topic: string, key: string) => Promise.all(pages.map(page => page.evaluate(({ topic, key }) => window.notificationTest.emit(topic, key), { topic, key })))
  try {
    for (const page of pages) {
      page.on('pageerror', error => errors.push(error.message))
      await prepare(page)
      await page.exposeFunction('readNotificationApi', async (request: { path: string; method: string; body: string; authorization: string | null }) => {
        expect(request.authorization).toBe('Bearer browser-token')
        if (request.path === '/api/auth/me') return { data: { expires_at: '2099-01-01T00:00:00Z', user: { user_id: 1, username: 'notifications', display_name: '通知验收', avatar_url: null, roles: [], disabled: false } } }
        if (request.path === '/api/models') return { data: { items: [{ modelId: 'main', displayName: 'Main', connectionId: 'provider', connectionDisplayName: '模型', reasoningEnabled: false, isDefault: true }], defaultModelId: 'main' } }
        if (request.path === '/api/conversation/config') return { data: { dayRanges: [7, 30] } }
        if (request.path === '/api/conversation/history') return { data: { items: [...threads.values()].map(item => ({
          id: item.id, threadId: item.threadId, title: item.title, titleSource: item.titleSource, titleGenerationStatus: item.titleGenerationStatus, titleSeq: item.titleSeq,
          status: 'idle', lastRunId: item.headRunId, lastModel: item.lastModel, accessMode: item.accessMode, messageCount: item.messageCount, toolCallCount: 0,
          hasPendingInterrupt: false, pendingInteractionKind: null, pinned: false, createdAt: item.createdAt, updatedAt: item.updatedAt,
        })), nextCursor: null } }
        if (request.path === '/api/conversation/chat') {
          const command = JSON.parse(request.body) as { runId: string; messages: { content: string }[] }
          const item = conversation('shared-thread', command.runId, command.messages[0].content)
          threads.set(item.threadId, item)
          await broadcast('studio.conversation.changed', item.threadId)
          return { stream: [
            { type: 'RUN_STARTED', threadId: item.threadId, runId: item.headRunId, title: item.title, titleSource: item.titleSource, titleSeq: item.titleSeq, titleGenerationStatus: item.titleGenerationStatus },
            { type: 'RUN_FINISHED', threadId: item.threadId, runId: item.headRunId },
          ].map((event, index) => `id: ${index + 1}\ndata: ${JSON.stringify(event)}\n\n`).join('') }
        }
        const item = threads.get(request.path.split('/')[3])
        if (item && request.path.endsWith('/title')) return { data: { threadId: item.threadId, title: item.title, titleSource: item.titleSource, titleGenerationStatus: item.titleGenerationStatus, titleSeq: item.titleSeq } }
        if (item && request.path.endsWith('/history')) return { data: item }
        throw new Error(`未预期的接口：${request.path}`)
      })
      await page.goto(baseURL!)
      await expect(page.getByRole('textbox', { name: '消息输入' })).toBeEnabled()
      await expect.poll(() => page.evaluate(() => window.notificationTest.active())).toBe(1)
    }
    const [first, second] = pages
    await first.getByRole('textbox', { name: '消息输入' }).fill('跨窗口会话')
    await first.getByRole('button', { name: '发送消息', exact: true }).click()
    await expect(second.getByRole('button', { name: '打开会话：跨窗口会话', exact: true })).toBeVisible()
    const item = threads.get('shared-thread')!
    item.title = '通知后的标题'
    item.titleSeq += 1
    await broadcast('studio.conversation.title.changed', item.threadId)
    await expect(second.getByRole('button', { name: '打开会话：通知后的标题', exact: true })).toBeVisible()

    item.title = '断连期间的标题'
    item.titleSeq += 1
    await second.evaluate(() => window.notificationTest.disconnect())
    await second.clock.runFor(1000)
    await expect(second.getByRole('button', { name: '打开会话：断连期间的标题', exact: true })).toBeVisible()
    await expect.poll(() => second.evaluate(() => window.notificationTest.active())).toBe(1)

    await second.evaluate(() => window.notificationTest.visibility(true))
    await expect.poll(() => second.evaluate(() => window.notificationTest.active())).toBe(0)
    item.title = '恢复可见后的标题'
    item.titleSeq += 1
    await broadcast('studio.conversation.title.changed', item.threadId)
    await second.evaluate(() => window.notificationTest.visibility(false))
    await expect(second.getByRole('button', { name: '打开会话：恢复可见后的标题', exact: true })).toBeVisible()
    await expect.poll(() => second.evaluate(() => window.notificationTest.active())).toBe(1)
    expect(errors).toEqual([])
  } finally {
    await Promise.all(contexts.map(context => context.close()))
  }
})
