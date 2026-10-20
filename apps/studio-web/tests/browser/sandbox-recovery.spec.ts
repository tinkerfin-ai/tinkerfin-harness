import { expect, test } from '@playwright/test'
import type { ConversationHistoryDetail } from '../../src/api/conversation/history'
import { emptyTraceGraph } from '../../src/test/traceFixtures'
import { installLiveRun } from './fixtures/liveRun'
import { installNotificationStream } from './fixtures/notifications'
import { installProjectScope } from './fixtures/projects'

const time = '2026-10-12T00:00:00.000000Z'
const threadId = 'sandbox-recovery'
const runId = 'preparing-run'
const user = { user_id: 1, username: 'recovery-test', avatar_url: null, roles: [], disabled: false }
const snapshot = (): ConversationHistoryDetail => ({
  projectId: 'project-1', archived: false, accessMode: 'write_approval', id: 1, threadId,
  title: '运行环境恢复', titleSource: 'user', titleGenerationStatus: 'idle', titleSeq: 1,
  lastModel: 'main', pinned: false, asOfSeq: 2, generation: 'recovery-generation', observedAt: time,
  headRunId: runId, availableHeads: [runId], historyCursor: null, messageCount: 1, toolCallCount: 0,
  messages: [{ id: 'question', agui: null, traceSeq: 1, runId, graphNamespace: [], role: 'user',
    content: '检查项目文件', contentOmitted: false, status: 'completed', createdAt: time, completedAt: time }],
  reasoning: [], graph: emptyTraceGraph(2), state: { root: {}, subgraphs: {} },
  submissionResult: null, planResults: [], interactionAvailability: [], interactions: [],
  status: { execution: 'running', headRunId: runId, phase: 'preparing' },
  completeness: { missingPrefix: false, missingTail: false, payloadOmitted: false },
  taskTrace: { status: 'ready', todoGroups: [] }, createdAt: time, updatedAt: time, runFailures: [],
})

for (const theme of ['light', 'dark']) for (const width of [320, 768, 1024, 1440]) {
  test(`准备反馈和中断结果保留 ${theme} ${width}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 960 })
    await page.emulateMedia({ reducedMotion: 'reduce' })
    await page.addInitScript(({ user, theme }) => {
      localStorage.setItem('tinkerfin:theme', theme)
      localStorage.setItem('tinkerfin.auth.session', JSON.stringify({ token: 'test-only',
        serverAddress: 'http://127.0.0.1:8090', tokenType: 'Bearer', expiresAt: '2099-01-01T00:00:00Z', user }))
    }, { user, theme })
    let detail = snapshot()
    let submissions = 0
    await page.route('**/api/**', async route => {
      const url = new URL(route.request().url())
      const path = url.pathname
      let data: unknown = {}
      if (path === '/api/auth/me') data = { user, expires_at: '2099-01-01T00:00:00Z' }
      else if (path === '/api/models') data = { items: [{ modelId: 'main', displayName: 'Main', connectionId: 'test-provider', connectionDisplayName: '测试提供方', reasoningEnabled: false, isDefault: true }], defaultModelId: 'main' }
      else if (path === '/api/conversation/config') data = { dayRanges: [7, 30] }
      else if (path === '/api/skills/installations' || path === '/api/skills/selection') data = []
      else if (path === '/api/conversation/history') data = { items: [{ ...detail, status: detail.status.execution === 'running' ? 'running' : 'error', lastRunId: runId, hasPendingInterrupt: false, pendingInteractionKind: null }], nextCursor: null }
      else if (path.endsWith('/history')) data = { ...detail, taskTrace: url.searchParams.get('includeTaskTrace') === 'false' ? null : detail.taskTrace }
      else if (path === '/api/conversation/chat') { submissions += 1; await route.abort(); return }
      await route.fulfill({ json: { code: 0, message: 'success', data } })
    })
    await installNotificationStream(page)
    await installProjectScope(page)
    const live = await installLiveRun(page, detail)
    await page.goto('/?project=project-1&thread=' + threadId)
    await live.emit({ type: 'CUSTOM', name: 'tinkerfin.run.phase', value: { threadId, runId, phase: 'preparing' } })
    const content = page.getByRole('region', { name: '对话内容', exact: true })
    await expect(content.getByRole('status', { name: '任务仍在继续' })).toBeVisible()
    await expect(content.getByText('正在准备运行环境')).toHaveCount(0)
    await page.screenshot({ path: testInfo.outputPath(`preparing-${theme}-${width}.png`) })
    detail = { ...detail, asOfSeq: 5, graph: emptyTraceGraph(5), observedAt: '2026-10-12T00:00:00.000001Z',
      status: { execution: 'failed', headRunId: runId },
      runFailures: [{ runId, errorCode: 'workspace_stopped', failedAt: time, retryable: true }] }
    await live.emit({ type: 'RUN_ERROR', code: 'workspace_stopped', rawEvent: { runId } })
    await live.finish()
    await expect(content.getByText('任务暂时无法执行，请稍后重试')).toBeVisible()
    await expect(content.getByLabel('任务仍在继续')).toHaveCount(0)
    await expect(content.getByRole('button', { name: '重试', exact: true })).toBeVisible()
    await expect(content.getByText('历史同步失败，请重新加载')).toHaveCount(0)
    const draft = page.getByRole('textbox', { name: '消息输入' })
    await draft.fill('保留我的草稿')
    await page.screenshot({ path: testInfo.outputPath(`stopped-${theme}-${width}.png`) })
    await expect(draft).toHaveValue('保留我的草稿')
    detail = { ...snapshot(), status: { execution: 'unknown', headRunId: runId },
      observedAt: '2026-10-12T00:00:00.000001Z', completeness: { ...detail.completeness, missingTail: true } }
    await page.reload()
    await expect(content.getByText('执行已中断，结果未确认')).toBeVisible()
    await expect(content.getByLabel('任务仍在继续')).toHaveCount(0)
    await expect(content.getByText('检查项目文件')).toBeVisible()
    await expect(content.getByRole('button', { name: '重新加载' })).toBeVisible()
    await expect(content.getByRole('button', { name: '重试', exact: true })).toHaveCount(0)
    expect(submissions).toBe(0)
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    await page.screenshot({ path: testInfo.outputPath(`unconfirmed-${theme}-${width}.png`) })
  })
}
