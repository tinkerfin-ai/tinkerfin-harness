import { expect, test, type Page } from '@playwright/test'
import fixture from './fixtures/multimodal-history.json' with { type: 'json' }

const user = { user_id: 1, username: 'cache-test', display_name: '阅读恢复', avatar_url: null, roles: [], disabled: false }
const threadIds = ['cache-1', 'cache-2', 'cache-3', 'cache-4']
const title = (threadId: string) => `阅读恢复 ${threadId}`
const detail = (threadId: string, cursor: string | null) => {
  const start = threadId === 'cache-1' ? Number(cursor ?? 160) : 0
  const length = threadId === 'cache-1' ? 240 - start : 4
  return {
    ...fixture,
    threadId, title: title(threadId), lastModel: 'model-0', asOfSeq: 240,
    messageCount: threadId === 'cache-1' ? 240 : 4, toolCallCount: 0,
    historyCursor: start > 0 ? String(start - 80) : null,
    messages: Array.from({ length }, (_, offset) => {
      const index = start + offset
      return {
        ...fixture.messages[0], id: `${threadId}-message-${index}`, sourceId: `${threadId}-source-${index}`,
        traceSeq: index + 1, role: index % 2 ? 'assistant' : 'user',
        content: index % 2 ? `回答 ${index}\n\n${'缓存淘汰后应恢复原来的阅读位置。'.repeat(8)}${index === 239 ? '\n\n[阅读参考](#reading-reference)' : ''}` : `${threadId} 问题 ${index}`,
      }
    }),
    reasoning: [], interactions: [], runFailures: [],
    graph: { ...fixture.graph, asOfSeq: 240, turns: [], nodes: [], orderedNodeIds: [], matchedNodeIds: [] },
    taskTrace: { status: 'ready', todoGroups: [] },
  }
}

async function openFixture(page: Page, theme = 'light') {
  let failNextOlder = false
  let holdOlder: (() => Promise<void>) | undefined
  const olderRequests: string[] = []
  await page.setViewportSize({ width: 1440, height: 960 })
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.addInitScript(({ user, theme }) => {
    localStorage.setItem('tinkerfin.auth.session', JSON.stringify({ token: 'cache-test-token', serverAddress: 'http://127.0.0.1:8090', tokenType: 'Bearer', expiresAt: '2099-01-01T00:00:00Z', user }))
    localStorage.setItem('tinkerfin:theme', theme)
    localStorage.setItem('tinkerfin:language', 'zh-CN')
  }, { user, theme })
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url())
    const path = url.pathname
    let data: unknown = {}
    if (path === '/api/auth/me') data = { expires_at: '2099-01-01T00:00:00Z', user }
    else if (path === '/api/models') data = { defaultModelId: 'model-0', items: [{ modelId: 'model-0', displayName: 'Model', imageSupport: 'unknown', connectionId: 'test-provider', connectionDisplayName: '测试提供方', reasoningEnabled: false, isDefault: true }] }
    else if (path === '/api/automation/runs' || path === '/api/automation/tasks') data = { items: [], nextCursor: null }
    else if (path === '/api/automation/runs/counts') data = {}
    else if (path === '/api/automation/tasks/counts') data = { enabled: 0, paused: 0 }
    else if (path === '/api/conversation/config') data = { dayRanges: [7, 30] }
    else if (path === '/api/conversation/history') data = {
      items: threadIds.map(threadId => ({ ...detail(threadId, null), status: 'idle', hasPendingInterrupt: false, pendingInteractionKind: null, updatedAt: '2026-09-21T00:00:00Z' })), nextCursor: null,
    }
    else {
      const match = path.match(/^\/api\/conversation\/(cache-[1-4])\/history$/)
      if (match) {
        const cursor = url.searchParams.get('historyCursor')
        if (cursor !== null) {
          olderRequests.push(cursor)
          if (failNextOlder) {
            failNextOlder = false
            await route.fulfill({ status: 503, json: { code: 503, message: '历史页暂时不可用' } })
            return
          }
          const hold = holdOlder
          holdOlder = undefined
          await hold?.()
        }
        data = { ...detail(match[1], cursor), taskTrace: url.searchParams.get('includeTaskTrace') === 'false' ? null : detail(match[1], cursor).taskTrace }
      }
    }
    await route.fulfill({ json: { code: 0, message: 'success', data } })
  })
  await page.goto('/?thread=cache-1')
  await expect(page.getByText('cache-1 问题 238', { exact: true })).toBeAttached()
  return {
    olderRequests,
    failNextOlder: () => { failNextOlder = true },
    holdNextOlder: (hold: () => Promise<void>) => { holdOlder = hold },
  }
}

const pane = (page: Page) => page.getByRole('region', { name: '对话内容', exact: true })
const anchor = (page: Page) => page.locator('[id="cache-1-message-10"]')
const offset = (page: Page) => anchor(page).evaluate(element => {
  const viewport = element.closest('section[aria-label="对话内容"]')!
  return element.getBoundingClientRect().top - viewport.getBoundingClientRect().top
})

async function readEarlierPage(page: Page) {
  await page.getByRole('button', { name: '加载更早消息', exact: true }).click()
  await expect(page.getByText('cache-1 问题 80', { exact: true })).toBeAttached()
  await page.getByRole('button', { name: '加载更早消息', exact: true }).click()
  await expect(anchor(page)).toBeAttached()
  await pane(page).dispatchEvent('wheel', { deltaY: -100 })
  await anchor(page).evaluate(element => {
    const viewport = element.closest('section[aria-label="对话内容"]')!
    viewport.scrollTop += element.getBoundingClientRect().top - viewport.getBoundingClientRect().top + 25
  })
  await expect.poll(() => offset(page)).toBeCloseTo(-25, 0)
}

async function evictFirstConversation(page: Page) {
  for (const threadId of threadIds.slice(1)) {
    await page.getByRole('button', { name: `打开会话：${title(threadId)}`, exact: true }).click()
    await expect(page.getByText(`${threadId} 问题 0`, { exact: true })).toBeAttached()
  }
}

for (const theme of ['light', 'dark']) {
  test(`完成会话淘汰后重开恢复更早页阅读锚点且不抢焦点 ${theme}`, async ({ page }) => {
    const fixture = await openFixture(page, theme)
    await readEarlierPage(page)
    await evictFirstConversation(page)
    await page.getByRole('button', { name: `打开会话：${title('cache-1')}`, exact: true }).click()
    await expect(anchor(page)).toBeAttached()
    await expect.poll(() => offset(page)).toBeCloseTo(-25, 0)
    expect(fixture.olderRequests).toEqual(['80', '0', '80', '0'])
    await expect(anchor(page)).not.toBeFocused()
    await expect(anchor(page)).not.toHaveClass(/todo-trace-locate-target/)
  })
}

for (const interaction of ['鼠标', 'Space']) {
test(`${interaction}重试恢复早页失败后仍使用原阅读锚点`, async ({ page }) => {
  const fixture = await openFixture(page)
  await readEarlierPage(page)
  await evictFirstConversation(page)
  fixture.failNextOlder()
  await page.getByRole('button', { name: `打开会话：${title('cache-1')}`, exact: true }).click()
  const retry = page.getByRole('button', { name: '重试恢复阅读位置', exact: true })
  if (interaction === 'Space') {
    await retry.focus()
    await retry.press('Space')
  } else await retry.click()
  await expect(anchor(page)).toBeAttached()
  await expect.poll(() => offset(page)).toBeCloseTo(-25, 0)
  expect(fixture.olderRequests).toEqual(['80', '0', '80', '80', '0'])
  await expect(page.getByRole('button', { name: '重试恢复阅读位置', exact: true })).not.toBeAttached()
})
}

for (const reason of ['切换会话', '用户滚动', '键盘翻页', '链接空格', '自动化导航']) {
test(`${reason}取消未完成的阅读恢复，迟到历史页不改变当前会话`, async ({ page }) => {
  const fixture = await openFixture(page)
  await readEarlierPage(page)
  await evictFirstConversation(page)
  let started!: () => void
  let release!: () => void
  const requested = new Promise<void>(resolve => { started = resolve })
  const released = new Promise<void>(resolve => { release = resolve })
  fixture.holdNextOlder(async () => { started(); await released })
  await page.getByRole('button', { name: `打开会话：${title('cache-1')}`, exact: true }).click()
  await requested
  const cancelled = page.waitForEvent('requestfailed', request => new URL(request.url()).searchParams.get('historyCursor') === '80')
  if (reason === '切换会话') {
    await page.getByRole('button', { name: `打开会话：${title('cache-2')}`, exact: true }).click()
  } else if (reason === '自动化导航') {
    await page.getByRole('button', { name: '自动化', exact: true }).click()
  } else if (reason === '链接空格') {
    await page.getByRole('link', { name: '阅读参考', exact: true }).press('Space')
  } else if (reason === '键盘翻页') {
    await pane(page).focus()
    await pane(page).press('PageUp')
  } else {
    await pane(page).hover()
    await page.mouse.wheel(0, -120)
  }
  await cancelled
  release()
  if (reason === '自动化导航') {
    await expect(pane(page)).not.toBeAttached()
    await expect(page.getByRole('tab', { name: '历史', exact: true })).toBeVisible()
    await page.getByRole('button', { name: `打开会话：${title('cache-1')}`, exact: true }).click()
    await expect(anchor(page)).toBeAttached()
    await expect.poll(() => offset(page)).toBeCloseTo(-25, 0)
    return
  }
  await expect(page.getByText(reason === '切换会话' ? 'cache-2 问题 0' : 'cache-1 问题 238', { exact: true })).toBeAttached()
  await expect(anchor(page)).not.toBeAttached()
  await expect(page.getByRole('button', { name: '重试恢复阅读位置', exact: true })).not.toBeAttached()
})
}


test('进入自动化再返回同一会话仍恢复原阅读位置', async ({ page }) => {
  const fixture = await openFixture(page)
  await readEarlierPage(page)
  await page.getByRole('button', { name: '自动化', exact: true }).click()
  await expect(pane(page)).not.toBeAttached()
  await expect(page.getByRole('tab', { name: '历史', exact: true })).toBeVisible()
  await page.getByRole('button', { name: `打开会话：${title('cache-1')}`, exact: true }).click()
  await expect(anchor(page)).toBeAttached()
  await expect.poll(() => offset(page)).toBeCloseTo(-25, 0)
  expect(fixture.olderRequests).toEqual(['80', '0'])
  await expect(anchor(page)).not.toBeFocused()
})
