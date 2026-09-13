import { expect, test } from '@playwright/test'
import type { ConversationHistoryDetail, TraceMessage } from '../../src/api/conversation/history'
import { emptyTraceGraph } from '../../src/test/traceFixtures'

const threadId = 'failure-browser'
const time = '2026-09-08T00:00:00.000Z'
const user = { user_id: 1, username: 'failure-test', display_name: '会话验收', avatar_url: null, roles: [], disabled: false }
const messages: TraceMessage[] = [1, 2, 3].map(id => ({
  id: `question-${id}`, agui: null, traceSeq: id, runId: `run-${id}`,
  graphNamespace: [], role: 'user', content: `你好 ${id}`, contentOmitted: false,
  status: 'completed', createdAt: time, completedAt: time,
}))
const detail: ConversationHistoryDetail = { accessMode: 'write_approval',
  id: 1, threadId, title: '连续失败验收', titleSource: 'user', titleGenerationStatus: 'idle', titleSeq: 1,
  lastModel: 'main', pinned: false, asOfSeq: 10, generation: 'failure-generation', observedAt: time,
  headRunId: 'run-3', availableHeads: ['run-3'], historyCursor: null, messageCount: 5, toolCallCount: 0,
  messages: [
    { ...messages[0], id: 'normal-question', runId: 'normal-run', traceSeq: 0, content: '正常问题' },
    { ...messages[0], id: 'normal-answer', runId: 'normal-run', traceSeq: 0, role: 'assistant', content: '图中左边是一个蓝色的圆形，右边是一个橙色的三角形。' },
    ...messages,
  ], reasoning: [], graph: emptyTraceGraph(10), state: { root: {}, subgraphs: {} }, interactions: [],
  status: { execution: 'failed', headRunId: 'run-3' }, completeness: { missingPrefix: false, missingTail: false, payloadOmitted: false },
  taskTrace: { status: 'ready', todoGroups: [] }, createdAt: time, updatedAt: time,
  runFailures: messages.map(message => ({ runId: message.runId, errorCode: 'runtime_initialization_error', failedAt: time, retryable: true })),
}
for (const theme of ['light', 'dark']) for (const width of [320, 768, 1024, 1440]) {
  test(`连续失败的间距和只读历史 ${theme} ${width}`, async ({ page }) => {
    await page.setViewportSize({ width, height: 960 })
    await page.addInitScript(({ user, theme }) => {
      localStorage.setItem('tinkerfin:theme', theme)
      localStorage.setItem('tinkerfin.auth.session', JSON.stringify({ token: 'test-only', tokenType: 'Bearer', expiresAt: '2099-01-01T00:00:00Z', user }))
    }, { user, theme })
    const posts: unknown[] = []
    await page.route('**/api/**', async route => {
      const url = new URL(route.request().url())
      const path = url.pathname
      let data: unknown = {}
      if (path === '/api/auth/me') data = { user, expires_at: '2099-01-01T00:00:00Z' }
      else if (path === '/api/models') data = { items: [{ modelId: 'main', displayName: 'Main', imageSupport: 'supported', reasoningEnabled: false, isDefault: true }], defaultModelId: 'main' }
      else if (path === '/api/conversation/config') data = { dayRanges: [7, 30] }
      else if (path === '/api/conversation/history') data = { items: [{ ...detail, status: 'error', lastRunId: 'run-3', hasPendingInterrupt: false, pendingInteractionKind: null }], nextCursor: null }
      else if (path.endsWith('/history')) data = { ...detail, taskTrace: url.searchParams.get('includeTaskTrace') === 'false' ? null : detail.taskTrace }
      else if (path === '/api/conversation/chat') { posts.push(route.request().postDataJSON()); await route.abort('connectionreset'); return }
      await route.fulfill({ json: { code: 0, message: 'success', data } })
    })
    await page.goto('/?thread=' + threadId)
    await expect(page.getByRole('region', { name: '会话异常' })).toHaveCount(3)
    await expect(page.getByRole('alert')).toHaveCount(0)
    const geometry = await page.locator('.message-list').evaluate(list => {
      const measure = (element: Element) => {
        const rect = element.getBoundingClientRect()
        const style = getComputedStyle(element)
        const before = element.previousElementSibling?.getBoundingClientRect()
        const after = element.nextElementSibling?.getBoundingClientRect()
        const button = element.querySelector('button')?.getBoundingClientRect()
        return {
          before: before ? rect.top - before.bottom : null,
          after: after ? after.top - rect.bottom : null,
          marginTop: style.marginTop, marginBottom: style.marginBottom,
          padding: style.padding, height: rect.height, x: rect.x, width: rect.width,
          right: rect.right, buttonHeight: button?.height,
        }
      }
      return {
        normal: measure(list.querySelector('.assistant-message')!),
        failures: [...list.querySelectorAll('[aria-label="会话异常"]')].map(measure),
      }
    })

    for (const [index, item] of geometry.failures.entries()) {
      expect(item.before).toBe(geometry.normal.before)
      expect(item.marginTop).toBe(geometry.normal.marginTop)
      expect(item.marginBottom).toBe(geometry.normal.marginBottom)
      if (index < 2) expect(item.after).toBe(geometry.normal.after)
      expect(item.x).toBe(geometry.normal.x)
      expect(item.width).toBe(geometry.normal.width)
      expect(item.x).toBeGreaterThanOrEqual(0)
      expect(item.right).toBeLessThanOrEqual(width)
      if (width === 320) expect(item.buttonHeight).toBeGreaterThanOrEqual(44)
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)

    expect(posts).toHaveLength(0)
    if (theme !== 'light' || width !== 320) return

    await page.reload()
    await expect(page.getByRole('region', { name: '会话异常' })).toHaveCount(3)
    await expect(page.getByRole('alert')).toHaveCount(0)
    expect(posts).toHaveLength(0)
    if (width === 320) await page.emulateMedia({ reducedMotion: 'reduce' })
    const draft = page.getByRole('textbox', { name: '消息输入' })
    await draft.fill('保留这个草稿')
    await page.getByRole('button', { name: '重试', exact: true }).first().click()
    await expect.poll(() => posts.length).toBe(1)
    expect(posts[0]).toMatchObject({ threadId, messages: [{ role: 'user', content: '你好 1' }] })
    await expect(draft).toHaveValue('保留这个草稿')
  })
}
