import { expect, test } from '@playwright/test'
import type { Page } from '@playwright/test'
import original from './fixtures/multimodal-history.json' with { type: 'json' }
import { diagrams, fence, flowchart } from './fixtures/mermaid'
import type { ConversationHistoryDetail } from '../../src/api/conversation/history'
import { installLiveRun } from './fixtures/liveRun'
import { mockDownloadPermits } from './support/attachment-storage'

const user = { user_id: 1, username: 'diagram-test', display_name: '图表验收', avatar_url: null, roles: [], disabled: false }
const assistantId = original.messages.at(-1)!.id
const jsonSource = '{\n  "name": "中文示例",\n  "count": 42,\n  "enabled": true\n}'

async function openConversation(page: Page, content: string, { theme = 'light', locale = 'zh-CN', document = false, running = false, beforeOpen }: { running?: boolean; beforeOpen?: (snapshot: ConversationHistoryDetail) => Promise<void>; theme?: 'light' | 'dark'; locale?: 'zh-CN' | 'en'; document?: boolean } = {}) {
  const attachment = { type: 'file', extras: { attachment: { id: 'diagram-doc', name: '流程说明.md', mime_type: 'text/markdown', size_bytes: 1024 } } }
  const snapshot: ConversationHistoryDetail = {
    ...(original as ConversationHistoryDetail), title: 'Markdown 图表',
    messages: (original as ConversationHistoryDetail).messages.map(message => message.id === assistantId ? { ...message, content } : message.role === 'tool' ? { ...message, content: document ? [attachment] : '操作完成' } : message),
    graph: { ...original.graph, nodes: (original as ConversationHistoryDetail).graph.nodes.map(node => node.id === assistantId ? { ...node, content } : node.kind === 'tool' ? { ...node, result: document ? [attachment] : '操作完成' } : node) },
  }
  if (running) snapshot.status = { ...snapshot.status, execution: 'running' }
  await beforeOpen?.(snapshot)
  await page.addInitScript(({ user, theme, locale }) => {
    localStorage.setItem('tinkerfin.auth.session', JSON.stringify({ token: 'browser-token', serverAddress: 'http://127.0.0.1:8090', tokenType: 'Bearer', expiresAt: '2099-01-01T00:00:00.000Z', user }))
    localStorage.setItem('tinkerfin:theme', theme)
    localStorage.setItem('tinkerfin:language', locale)
  }, { user, theme, locale })
  await page.route('**/{api,objects}/**', async route => {
    const path = new URL(route.request().url()).pathname
    let data: unknown = {}
    if (path === '/api/auth/me') data = { expires_at: '2099-01-01T00:00:00.000Z', user }
    else if (path === '/api/models') data = { items: [{ modelId: snapshot.lastModel, displayName: '测试模型',connectionId: 'test', connectionDisplayName: '测试提供方', reasoningEnabled: true, isDefault: true }], defaultModelId: snapshot.lastModel }
    else if (path === '/api/conversation/config') data = { dayRanges: [7, 30] }
    else if (path === '/api/conversation/history') data = { items: [{ ...snapshot, status: running ? 'running' : 'idle', hasPendingInterrupt: false, updatedAt: '2099-01-01T00:00:00.000Z' }], nextCursor: null }
    else if (path === `/api/conversation/${snapshot.threadId}/history`) data = snapshot
    else if (path.endsWith('/trace')) {
      await route.fulfill({ contentType: 'text/event-stream', body: `event: trace\ndata: ${JSON.stringify({ type: 'snapshot', snapshot })}\n\n` }); return
    } else if (path === '/objects/diagram-doc') { await route.fulfill({ contentType: 'text/markdown', body: content }); return }
    await route.fulfill({ json: { code: 0, message: 'success', data } })
  })
  if (document) await mockDownloadPermits(page)
  await page.goto('/')
  const conversationName = locale === 'en' ? 'Open conversation: Markdown 图表' : `打开会话：Markdown 图表${running ? '，正在生成' : ''}`
  await page.getByRole('button', { name: conversationName, exact: true }).click()
  return page.locator(`[id="${assistantId}"]`)
}

for (const theme of ['light', 'dark'] as const) {
  test(`八类 Mermaid 在 ${theme} 主题使用真实引擎成图`, async ({ page }, testInfo) => {
    const message = await openConversation(page, diagrams.map(source => fence(source)).join('\n\n'), { theme })
    const blocks = message.getByRole('figure', { name: 'Mermaid 图表', exact: true })
    await expect(blocks).toHaveCount(diagrams.length)
    for (let index = 0; index < diagrams.length; index++) {
      const image = blocks.nth(index).getByRole('img')
      await expect(image).toBeVisible()
      await expect(image).toHaveJSProperty('complete', true)
      expect(await image.evaluate(element => (element as HTMLImageElement).naturalWidth)).toBeGreaterThan(0)
      await blocks.nth(index).scrollIntoViewIfNeeded()
      await blocks.nth(index).screenshot({ path: testInfo.outputPath(`diagram-${theme}-${index}.png`) })
    }
  })
}

for (const [theme, locale] of [['light', 'zh-CN'], ['dark', 'en']] as const) {
  test(`图表和源码适应容器、全屏返回与高亮 ${theme} ${locale}`, async ({ page }, testInfo) => {
    const english = locale === 'en'
    await page.emulateMedia({ reducedMotion: 'reduce' })
    const message = await openConversation(page, `处理流程如下：\n\n${fence(flowchart)}\n\n${fence(jsonSource, 'json')}`, { theme, locale })
    const block = message.getByRole('figure', { name: english ? 'Mermaid diagram' : 'Mermaid 图表', exact: true })
    await expect(block.getByRole('img')).toBeVisible()
    const expand = block.getByRole('button', { name: english ? 'Enlarge diagram' : '放大图表', exact: true })
    for (const width of [320, 768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 960 })
      await block.scrollIntoViewIfNeeded()
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
      await page.screenshot({ path: testInfo.outputPath(`inline-${width}.png`) })
      await expect(block.getByRole('button', { name: english ? 'Copy source' : '复制源码', exact: true })).toHaveCount(0)
      await expect(expand.locator('.ui-button__label')).toHaveCount(0)
      await expand.click()
      const viewer = page.getByRole('dialog', { name: english ? 'Diagram' : '图表', exact: true })
      await expect(viewer).toHaveCount(1)
      expect(await viewer.boundingBox()).toMatchObject({ x: 0, y: 0, width, height: 960 })
      await viewer.getByRole('button', { name: english ? 'Reset to 100%' : '重置为 100%', exact: true }).click()
      await viewer.getByRole('button', { name: english ? 'Zoom in' : '放大', exact: true }).click()
      await expect(viewer.getByRole('button', { name: english ? 'Reset to 100%' : '重置为 100%', exact: true })).toHaveText('125%')
      const stage = viewer.getByRole('region', { name: english ? 'Diagram canvas' : '图表画布' })
      await stage.focus()
      await page.keyboard.press('ArrowRight')
      await viewer.getByRole('button', { name: english ? 'Fit to canvas' : '适应画布', exact: true }).click()
      await page.screenshot({ path: testInfo.outputPath(`fullscreen-${width}.png`) })
      await page.keyboard.press('Escape')
      await expect(expand).toBeFocused()
    }
    await page.setViewportSize({ width: 1440, height: 960 })
    await block.getByRole('tab', { name: english ? 'Source' : '源码', exact: true }).click()
    const source = block.getByRole('tabpanel', { name: english ? 'Source' : '源码', exact: true })
    await expect(source).toHaveText(flowchart)
    await expect(block.getByRole('button', { name: english ? 'Enlarge diagram' : '放大图表', exact: true })).toHaveCount(0)
    const copy = block.getByRole('button', { name: english ? 'Copy source' : '复制源码', exact: true })
    await expect(copy).toBeVisible()
    await expect(copy.locator('.ui-button__label')).toHaveCount(0)
    await expect(source.locator('code span').first()).toBeVisible()
    const json = message.locator('code[data-language="json"]')
    await expect(json.getByText('42', { exact: true })).toBeVisible()
    expect(await json.textContent()).toBe(jsonSource)
    const colors = await json.evaluate(code => [...new Set([...code.querySelectorAll('span')].map(span => getComputedStyle(span).color))])
    expect(colors.length).toBeGreaterThanOrEqual(4)
    await page.emulateMedia({ reducedMotion: 'reduce' })
    await page.screenshot({ path: testInfo.outputPath('highlighted-source.png') })
  })
}

test('文档中的图表放大只占用原弹窗，返回保留阅读位置', async ({ page }) => {
  await openConversation(page, `${fence(flowchart)}\n\n${fence(jsonSource, 'json')}`, { document: true })
  await page.getByRole('button', { name: '预览文档：流程说明.md', exact: true }).click()
  const document = page.getByRole('dialog', { name: '流程说明.md', exact: true })
  const expand = document.getByRole('button', { name: '放大图表', exact: true })
  await expect(expand).toBeEnabled()
  await expand.click()
  await expect(page.getByRole('dialog')).toHaveCount(1)
  await expect(page.getByRole('dialog')).toHaveAccessibleName('图表')
  await page.getByRole('button', { name: '返回原内容', exact: true }).click()
  await expect(document).toBeVisible()
  await expect(expand).toBeFocused()
  await document.getByRole('button', { name: '关闭对话框', exact: true }).click()
  await expect(page.getByRole('button', { name: '预览文档：流程说明.md', exact: true })).toBeFocused()
})

test('错误图表和外部资源不影响正文，也不发起外部请求', async ({ page }) => {
  const outside: string[] = []
  page.on('request', request => { if (request.url().includes('example.com')) outside.push(request.url()) })
  const message = await openConversation(page, [
    fence('flowchart LR\n A['), fence('architecture-beta'),
    fence('flowchart LR\n A@{img: "https://example.com/a.png"}'),
    fence('%%{init: {"securityLevel":"loose"}}%%\nflowchart LR\nA-->B'),
    fence('<script>window.diagramInjected = true</script>', 'html'), '正文仍可阅读',
  ].join('\n\n'))
  const failed = message.getByRole('figure', { name: 'Mermaid 图表', exact: true })
  await expect(failed).toHaveCount(4)
  await expect(message.getByRole('tab', { name: '图表', exact: true })).toHaveCount(0)
  await expect(message.getByRole('button', { name: '放大图表', exact: true })).toHaveCount(0)
  await expect(message.getByRole('button', { name: '复制源码', exact: true })).toHaveCount(4)
  for (const block of await failed.all()) await expect(block.locator('code')).toBeVisible()
  await expect(message.getByText('正文仍可阅读')).toBeVisible()
  expect(outside).toEqual([])
  expect(await page.evaluate(() => 'diagramInjected' in window)).toBe(false)
})


test('长图支持键盘与触控平移，切换主题保持全屏和缩放', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' })
  const source = 'flowchart LR\n A[一个需要横向浏览的中文长标签] --> B[Process with a long English label] --> C[完成]'
  const message = await openConversation(page, fence(source))
  await page.setViewportSize({ width: 320, height: 800 })
  await message.getByRole('button', { name: '放大图表', exact: true }).click()
  const viewer = page.getByRole('dialog', { name: '图表', exact: true })
  const canvas = viewer.getByRole('region', { name: '图表画布' })
  await canvas.press('1')
  const initial = await canvas.evaluate(element => element.scrollLeft)
  expect(initial).toBeGreaterThan(0)
  await canvas.press('ArrowLeft')
  await expect.poll(() => canvas.evaluate(element => element.scrollLeft)).toBeLessThan(initial)
  const position = await canvas.evaluate(element => element.scrollLeft)
  const box = (await canvas.boundingBox())!
  const cdp = await page.context().newCDPSession(page)
  await cdp.send('Emulation.setTouchEmulationEnabled', { enabled: true, maxTouchPoints: 1 })
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x: box.x + 200, y: box.y + 200 }] })
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x: box.x + 120, y: box.y + 200 }] })
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] })
  await expect.poll(() => canvas.evaluate(element => element.scrollLeft)).toBeGreaterThan(position)
  const url = await viewer.getByRole('img').getAttribute('src')
  await page.evaluate(() => { document.documentElement.dataset.theme = 'dark' })
  await expect(viewer.getByRole('img')).not.toHaveAttribute('src', url!)
  await expect(viewer.getByRole('button', { name: '重置为 100%' })).toHaveText('100%')
  await expect(viewer.getByRole('button', { name: '复制源码' })).toHaveCount(0)
})

test('回复中的源码结束后成图，暂停跟随后全屏返回保持阅读位置且回复继续', async ({ page }) => {
  await page.clock.install()
  let live!: Awaited<ReturnType<typeof installLiveRun>>
  await openConversation(page, fence(flowchart), { running: true, beforeOpen: async snapshot => {
    live = await installLiveRun(page, snapshot)
  } })
  const pane = page.getByRole('region', { name: '对话内容', exact: true })
  const content = fence('flowchart LR\nA-->B')
  await live.emit(
    { type: 'TEXT_MESSAGE_START', messageId: 'diagram-stream', role: 'assistant' },
    { type: 'TEXT_MESSAGE_CONTENT', messageId: 'diagram-stream', delta: content },
  )
  await page.clock.runFor(2000)
  const block = pane.getByRole('figure', { name: 'Mermaid 图表', exact: true }).last()
  await expect(block.getByRole('status')).toContainText('图表生成中')
  await expect(block.getByRole('img')).toHaveCount(0)
  await block.getByRole('tab', { name: '源码', exact: true }).click()
  await live.emit({ type: 'TEXT_MESSAGE_END', messageId: 'diagram-stream' })
  await page.clock.runFor(2000)
  await expect(block.getByRole('tab', { name: '源码', exact: true })).toHaveAttribute('aria-selected', 'true')
  await block.getByRole('tab', { name: '图表', exact: true }).click()
  await expect(block.getByRole('img')).toBeVisible()
  const expand = pane.getByRole('button', { name: '放大图表', exact: true }).first()
  await expand.scrollIntoViewIfNeeded()
  await pane.evaluate(element => {
    element.dispatchEvent(new WheelEvent('wheel', { bubbles: true, deltaY: -1 }))
    element.scrollTop = 0
    element.dispatchEvent(new Event('scroll', { bubbles: true }))
  })
  await page.clock.runFor(32)
  const top = await pane.evaluate(element => element.scrollTop)
  await expand.click()
  await live.emit(
    { type: 'TEXT_MESSAGE_START', messageId: 'continued-answer', role: 'assistant' },
    { type: 'TEXT_MESSAGE_CONTENT', messageId: 'continued-answer', delta: '图表打开期间继续回复' },
    { type: 'TEXT_MESSAGE_END', messageId: 'continued-answer' },
  )
  await page.clock.runFor(2000)
  await page.getByRole('button', { name: '返回原内容', exact: true }).click()
  await expect(pane).toContainText('图表打开期间继续回复')
  await expect(expand).toBeFocused()
  await expect.poll(() => pane.evaluate(element => element.scrollTop)).toBe(top)
  await live.finish()
})

for (const theme of ['light', 'dark'] as const) {
  test(`所有代码块表头颜色及纯图标复制位置一致 ${theme}`, async ({ page }, testInfo) => {
    await page.emulateMedia({ reducedMotion: 'reduce' })
    const message = await openConversation(page, [
      fence('flowchart TD\nA-->B'), fence('flowchart TD\nA-->'),
      fence('# Markdown 源码', 'markdown'), fence('{"value": 42}', 'json'), fence('普通文本', ''),
    ].join('\n\n'), { theme })
    const blocks = message.getByRole('figure')
    await expect(blocks).toHaveCount(5)
    await expect(blocks.nth(1).getByRole('tab')).toHaveCount(0)
    await blocks.first().getByRole('tab', { name: '源码', exact: true }).click()
    for (const width of [320, 768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 960 })
      const metrics = []
      for (const block of await blocks.all()) {
        const button = block.getByRole('button', { name: /^复制(?:源码)?$/ })
        await expect(button).toHaveText('')
        // 相对位置在同一次页面计算中读取，避免响应式布局或滚动更新跨越两次测量
        metrics.push(await button.evaluate(element => {
          const header = element.closest('figcaption')
          if (!header) throw new Error('代码复制按钮缺少表头')
          const headBox = header.getBoundingClientRect()
          const buttonBox = element.getBoundingClientRect()
          return {
            background: getComputedStyle(header).backgroundColor,
            height: headBox.height,
            right: headBox.right - buttonBox.x - buttonBox.width / 2,
            top: buttonBox.y + buttonBox.height / 2 - headBox.y,
          }
        }))
      }
      for (const metric of metrics.slice(1)) expect(metric).toEqual(metrics[0])
      await message.screenshot({ path: testInfo.outputPath(`source-headers-${theme}-${width}.png`) })
    }
  })
}
