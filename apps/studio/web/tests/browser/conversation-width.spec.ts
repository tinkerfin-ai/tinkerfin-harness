import { expect, test, type Page } from '@playwright/test'
import fixture from './fixtures/multimodal-history.json' with { type: 'json' }

const user = { user_id: 1, username: 'width-test', display_name: '布局验收', avatar_url: null, roles: [], disabled: false }
const history = {
  ...fixture, title: '布局验收', lastModel: 'model-0', messageCount: 16, toolCallCount: 0,
  messages: Array.from({ length: 16 }, (_, i) => ({
    ...fixture.messages[0], id: `width-message-${i}`, sourceId: `width-source-${i}`, traceSeq: i + 1,
    role: i % 2 ? 'assistant' : 'user',
    content: i % 2 ? `回答 ${i}\n\n${'会话调宽应保留当前阅读消息，代码和表格在自身区域内滚动。'.repeat(35)}\n\n| 项目 | 数据 |\n| --- | --- |\n| 布局 | 容器自适应 |` : `问题 ${i}`,
  })),
  reasoning: [], interactions: [], runFailures: [],
  graph: { ...fixture.graph, turns: [], nodes: [], orderedNodeIds: [], matchedNodeIds: [] },
  taskTrace: { status: 'ready', todoGroups: [] },
}

async function openConversation(page: Page, theme: string) {
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.addInitScript(({ user, theme }) => {
    localStorage.setItem('tinkerfin.auth.session', JSON.stringify({ token: 'width-test-token', tokenType: 'Bearer', expiresAt: '2099-01-01T00:00:00Z', user }))
    localStorage.setItem('tinkerfin:theme', theme)
    localStorage.setItem('tinkerfin:language', 'zh-CN')
  }, { user, theme })
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (!path.startsWith('/api/')) { await route.continue(); return }
    let data: unknown = {}
    if (path === '/api/auth/me') data = { expires_at: '2099-01-01T00:00:00Z', user }
    else if (path === '/api/models') data = { defaultModelId: 'model-0', items: [{ modelId: 'model-0', displayName: 'Model', imageSupport: 'unknown', connectionId: 'test-provider', connectionDisplayName: '测试提供方', reasoningEnabled: false, isDefault: true }] }
    else if (path === '/api/conversation/config') data = { dayRanges: [7, 30] }
    else if (path === '/api/conversation/history') data = { items: [{ ...history, status: 'idle', hasPendingInterrupt: false, updatedAt: '2026-09-14T00:00:00Z' }], nextCursor: null }
    else if (path.endsWith(`/${history.threadId}/history`)) data = history
    else if (path.endsWith(`/${history.threadId}/trace`)) {
      await route.fulfill({ contentType: 'text/event-stream', body: `event: trace\ndata: ${JSON.stringify({ type: 'snapshot', snapshot: history })}\n\n` })
      return
    }
    await route.fulfill({ json: { code: 0, message: 'success', data } })
  })
  await page.goto(`/?thread=${history.threadId}`)
  await expect(page.getByRole('region', { name: '对话内容', exact: true })).toContainText('问题 0')
}

async function geometry(page: Page) {
  return page.evaluate(() => {
    const main = document.getElementById('main-content')!
    const list = document.querySelector('.message-list')!.getBoundingClientRect()
    const composer = document.querySelector('.composer')!.getBoundingClientRect()
    const parent = main.getBoundingClientRect()
    const gutter = parseFloat(getComputedStyle(main).getPropertyValue('--layout-page-gutter'))
    return { width: list.width, input: composer.width, center: list.x + list.width / 2 - parent.x - parent.width / 2,
      max: Math.floor(parent.width - gutter * 2 - 32), auto: Math.min(Math.floor(parent.width - gutter * 2 - 32), Math.round(Math.max(680, Math.min(parent.width * .64, 920)))),
      overflow: document.documentElement.scrollWidth > innerWidth, gutter }
  })
}

for (const theme of ['light', 'dark']) {
  test(`会话自动宽度及各尺寸安全边距 ${theme}`, async ({ page }, testInfo) => {
    await openConversation(page, theme)
    for (const width of [320, 768, 1024, 1440, 1920, 2560, 3840]) {
      await page.setViewportSize({ width, height: 960 })
      await expect.poll(async () => { const size = await geometry(page); return Math.abs(size.width - size.auto) }).toBeLessThan(1)
      const size = await geometry(page)
      expect(size.overflow).toBe(false)
      expect(Math.abs(size.center)).toBeLessThan(1)
      expect(size.input - size.width).toBeCloseTo(32, 0)
      await expect(page.getByRole('button', { name: '会话宽度', exact: true })).toHaveCount(0)
      if ([320, 1440, 2560].includes(width)) await page.screenshot({ path: testInfo.outputPath(`conversation-${theme}-${width}.png`) })
    }
  })
}

test('拖动和刷新保留偏好及历史阅读位置', async ({ page }) => {
  await page.setViewportSize({ width: 1920, height: 960 })
  await openConversation(page, 'light')
  const pane = page.getByRole('region', { name: '对话内容', exact: true })
  await pane.evaluate(element => {
    element.dispatchEvent(new WheelEvent('wheel', { bubbles: true, deltaY: -1 }))
    element.scrollTop = 1200
    element.dispatchEvent(new Event('scroll', { bubbles: true }))
  })
  const anchor = await pane.evaluate(element => {
    const top = element.getBoundingClientRect().top
    const message = Array.from(element.querySelectorAll('article[id]')).find(item => item.getBoundingClientRect().bottom > top)!
    return { id: message.id, offset: message.getBoundingClientRect().top - top }
  })
  const handle = page.getByRole('separator', { name: '调整会话右侧宽度' })
  const box = (await handle.boundingBox())!
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2)
  await page.mouse.down()
  await page.mouse.move(box.x + box.width / 2 + 80, box.y + box.height / 2)
  await page.mouse.up()
  await expect.poll(() => page.evaluate(() => localStorage.getItem('tinkerfin:conversation-width'))).toBe('1080')
  const offset = await pane.evaluate((element, id) => document.getElementById(id)!.getBoundingClientRect().top - element.getBoundingClientRect().top, anchor.id)
  expect(Math.abs(offset - anchor.offset)).toBeLessThan(2)
  await page.setViewportSize({ width: 768, height: 960 })
  await expect.poll(async () => { const size = await geometry(page); return Math.abs(size.width - size.max) }).toBeLessThan(1)
  expect(await page.evaluate(() => localStorage.getItem('tinkerfin:conversation-width'))).toBe('1080')
  await page.setViewportSize({ width: 1920, height: 960 })
  await page.reload()
  await expect(pane).toContainText('问题 0')
  await expect.poll(async () => (await geometry(page)).width).toBeCloseTo(1080, 0)
  await page.setViewportSize({ width: 1440, height: 960 })
  await page.getByRole('button', { name: '收起侧边栏', exact: true }).click()
  const expanded = page.getByRole('separator', { name: '调整会话右侧宽度' })
  await expanded.hover()
  const expandedBox = (await expanded.boundingBox())!
  await page.mouse.down()
  await page.mouse.move(1438, expandedBox.y + expandedBox.height / 2)
  await page.mouse.up()
  await expect.poll(async () => { const size = await geometry(page); return Math.abs(size.width - size.max) }).toBeLessThan(1)
})

for (const theme of ['light', 'dark']) {
  test(`设置与登录遵守用途宽度和页面安全边距 ${theme}`, async ({ page, browser }, testInfo) => {
    await page.setViewportSize({ width: 1440, height: 960 })
    await openConversation(page, theme)
    await page.getByRole('button', { name: '打开用户菜单' }).click()
    await page.getByRole('menuitem', { name: '设置', exact: true }).click()
    const settings = page.getByRole('dialog', { name: '设置', exact: true })
    for (const width of [320, 768, 1024, 1440, 2560]) {
      await page.setViewportSize({ width, height: 960 })
      const gutter = width <= 440 ? 12 : width <= 767 ? 16 : width <= 1023 ? 24 : 32
      await expect(settings).toHaveCSS('width', `${Math.min(960, width - gutter * 2)}px`)
      await expect(settings.getByRole('button', { name: '关闭对话框' })).toBeInViewport()
      if ([320, 1440].includes(width)) await page.screenshot({ path: testInfo.outputPath(`settings-${theme}-${width}.png`) })
    }
    const loggedOut = await browser.newContext({ reducedMotion: 'reduce' })
    try {
      const login = await loggedOut.newPage()
      await login.addInitScript(theme => {
        localStorage.setItem('tinkerfin:theme', theme)
        localStorage.setItem('tinkerfin:language', 'zh-CN')
      }, theme)
      await login.goto('/')
      const username = login.getByRole('textbox', { name: '用户名', exact: true })
      await expect(username).toBeVisible()
      for (const width of [320, 768, 1024, 1440, 2560]) {
        await login.setViewportSize({ width, height: 960 })
        await expect(username).toBeInViewport()
        const bounds = (await username.boundingBox())!
        const gutter = width <= 440 ? 12 : width <= 767 ? 16 : width <= 1023 ? 24 : 32
        expect(bounds.x).toBeGreaterThanOrEqual(gutter)
        expect(bounds.x + bounds.width).toBeLessThanOrEqual(width - gutter)
        expect(await login.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
        if ([320, 1440].includes(width)) await login.screenshot({ path: testInfo.outputPath(`login-${theme}-${width}.png`) })
      }
    } finally { await loggedOut.close() }
  })
}

test('触屏和窄布局按可用空间显示，不提供顶部调宽菜单', async ({ browser }) => {
  const context = await browser.newContext({ hasTouch: true, viewport: { width: 1440, height: 960 } })
  try {
    const page = await context.newPage()
    await openConversation(page, 'dark')
    await expect(page.getByRole('separator', { name: '调整会话右侧宽度' })).toBeHidden()
    await expect(page.getByRole('button', { name: '会话宽度', exact: true })).toHaveCount(0)
    await page.setViewportSize({ width: 640, height: 480 })
    await expect.poll(async () => { const size = await geometry(page); return Math.abs(size.width - size.max) }).toBeLessThan(1)
    expect((await geometry(page)).overflow).toBe(false)
  } finally { await context.close() }
})

for (const theme of ['light', 'dark']) {
  test(`调宽提示悬浮跟随并对齐输入框 ${theme}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width: 1440, height: 960 })
    await openConversation(page, theme)
    const left = page.getByRole('separator', { name: '调整会话左侧宽度' })
    const right = page.getByRole('separator', { name: '调整会话右侧宽度' })
    const indicator = left.locator('.conversation-width-indicator')
    await expect(indicator).toHaveCSS('opacity', '0')
    for (const width of [1024, 1440, 2560]) {
      await page.setViewportSize({ width, height: 960 })
      for (const [handle, edge] of [[left, 'left'], [right, 'right']] as const) {
        const box = (await handle.boundingBox())!
        for (const fraction of [.3, .7]) {
          const y = box.y + box.height * fraction
          await handle.hover({ position: { x: box.width / 2, y: box.height * fraction } })
          const mark = handle.locator('.conversation-width-indicator')
          await expect(mark).toHaveCSS('opacity', '1')
          const paired = (edge === 'left' ? right : left).locator('.conversation-width-indicator')
          await expect(paired).toHaveCSS('opacity', '1')
          const pairedBox = (await paired.boundingBox())!
          expect(Math.abs(pairedBox.y + pairedBox.height / 2 - y)).toBeLessThanOrEqual(1)
          const markBox = (await mark.boundingBox())!
          const composer = (await page.locator('.composer').boundingBox())!
          expect(Math.abs(markBox.x + markBox.width / 2 - (edge === 'left' ? composer.x - 16 : composer.x + composer.width + 16))).toBeLessThanOrEqual(1)
          expect(Math.abs(markBox.y + markBox.height / 2 - y)).toBeLessThanOrEqual(1)
        }
      }
    }
    await page.getByRole('button', { name: '收起侧边栏', exact: true }).click()
    const box = (await left.boundingBox())!
    await page.mouse.move(box.x + box.width / 2, box.y + box.height * .35)
    await expect(indicator).toHaveCSS('opacity', '1')
    const markBox = (await indicator.boundingBox())!
    const composer = (await page.locator('.composer').boundingBox())!
    expect(Math.abs(markBox.x + markBox.width / 2 - (composer.x - 16))).toBeLessThanOrEqual(1)
    await page.screenshot({ path: testInfo.outputPath(`handle-${theme}.png`) })
    await page.mouse.move(0, 0)
    await expect(indicator).toHaveCSS('opacity', '0')
    expect((await indicator.boundingBox())!.y).toBeCloseTo(markBox.y, 1)
    await expect(right.locator('.conversation-width-indicator')).toHaveCSS('opacity', '0')
    expect(await left.evaluate(element => (element as HTMLElement).tabIndex)).toBe(-1)
    expect(await right.evaluate(element => (element as HTMLElement).tabIndex)).toBe(-1)
  })
}

test('调宽不唤醒回到底部，真实滚动仍可显示并回到底部', async ({ page }) => {
  await page.setViewportSize({ width: 1920, height: 960 })
  await openConversation(page, 'light')
  const back = page.getByRole('button', { name: '回到底部', exact: true })
  const pane = page.getByRole('region', { name: '对话内容', exact: true })
  const right = page.getByRole('separator', { name: '调整会话右侧宽度' })
  const drag = async (delta: number) => {
    const box = (await right.boundingBox())!
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2)
    await page.mouse.down()
    await page.mouse.move(box.x + box.width / 2 + delta, box.y + box.height / 2, { steps: 4 })
    await page.mouse.up()
  }
  await expect(back).toHaveCount(0)
  await drag(-60)
  await expect(back).toHaveCount(0)
  await expect.poll(() => pane.evaluate(el => el.scrollHeight - el.scrollTop - el.clientHeight)).toBeLessThanOrEqual(1)
  await page.clock.install()
  await pane.evaluate(el => {
    el.dispatchEvent(new WheelEvent('wheel', { bubbles: true, deltaY: -1 }))
    el.scrollTop = 1200
    el.dispatchEvent(new Event('scroll', { bubbles: true }))
  })
  await expect(back).toBeVisible()
  await page.clock.fastForward(2000)
  await expect(back).toHaveCount(0)
  await drag(40)
  await page.clock.runFor(32)
  await expect(back).toHaveCount(0)
  await pane.evaluate(el => {
    el.dispatchEvent(new WheelEvent('wheel', { bubbles: true, deltaY: -1 }))
    el.scrollTop -= 80
    el.dispatchEvent(new Event('scroll', { bubbles: true }))
  })
  await expect(back).toBeVisible()
  await back.click()
  await expect(back).toHaveCount(0)
})

for (const theme of ['light', 'dark']) {
  test(`输入框外侧可拖动且鼠标操作没有焦点边框 ${theme}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width: 1440, height: 960 })
    await openConversation(page, theme)
    const composer = page.locator('.composer')
    const handle = page.getByRole('separator', { name: '调整会话左侧宽度' })
    const indicator = handle.locator('.conversation-width-indicator')
    const before = (await composer.boundingBox())!
    const start = (await handle.boundingBox())!
    const x = start.x + start.width / 2
    const y = before.y + before.height / 2
    await page.mouse.move(x, y)
    await expect(indicator).toHaveCSS('opacity', '1')
    const mark = (await indicator.boundingBox())!
    expect(Math.abs(mark.x + mark.width / 2 - (before.x - 16))).toBeLessThanOrEqual(1)
    expect(Math.abs(mark.y + mark.height / 2 - y)).toBeLessThanOrEqual(1)
    await expect(page.getByRole('separator', { name: '调整会话右侧宽度' }).locator('.conversation-width-indicator')).toHaveCSS('opacity', '1')
    await page.mouse.down()
    await expect(handle).not.toBeFocused()
    await expect(indicator).toHaveCSS('outline-style', 'none')
    await page.mouse.move(x - 32, y)
    await page.mouse.up()
    await expect.poll(async () => (await composer.boundingBox())!.width - before.width).toBeCloseTo(64, 0)
    const size = await geometry(page)
    expect(size.input - size.width).toBeCloseTo(32, 0)
    await expect(indicator).toHaveCSS('outline-style', 'none')
    await page.screenshot({ path: testInfo.outputPath(`composer-side-${theme}.png`) })
    await page.mouse.move(x + 100, y)
    await expect(indicator).toHaveCSS('opacity', '0')
  })
}
