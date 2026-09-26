import { mockDownloadPermits } from './support/attachment-storage'
import { measureBounds } from './support/geometry'
import { test, expect } from '@playwright/test'
import { resolve } from 'node:path'
import source from './fixtures/multimodal-history.json' with { type: 'json' }

const images = ['第一张.png', '第二张.png', '第三张.png'].map((name, index) => ({
  type: 'image', file_id: `gallery-${index}`, mime_type: 'image/png',
  extras: { attachment: { id: `gallery-${index}`, name, mime_type: 'image/png', size_bytes: 4881 } },
}))
const history = {
  ...source, title: '多图预览回归',
  messages: source.messages.map(message => message.role === 'tool' ? { ...message, content: images } : message),
  graph: { ...source.graph, nodes: source.graph.nodes.map(node => node.kind === 'tool' ? { ...node, result: images } : node) },
}
const user = { user_id: 1, username: 'gallery-test', display_name: '图片验收', avatar_url: null, roles: [], disabled: false }

test('图片失败态、恢复与多图连续键盘浏览保持无边框布局', async ({ page }, testInfo) => {
  let previewAvailable = false
  let originalAvailable = false
  await page.addInitScript(user => localStorage.setItem('tinkerfin.auth.session', JSON.stringify({ token: 'browser-token', serverAddress: 'http://127.0.0.1:8090', tokenType: 'Bearer', expiresAt: '2099-01-01T00:00:00.000Z', user })), user)
  await page.route('**/{api,objects}/**', async route => {
    const url = new URL(route.request().url())
    let data: unknown = {}
    if (url.pathname === '/api/auth/me') data = { expires_at: '2099-01-01T00:00:00.000Z', user }
    else if (url.pathname === '/api/models') data = { items: [{ modelId: history.lastModel, displayName: '测试模型',connectionId: 'test-provider', connectionDisplayName: '测试提供方', reasoningEnabled: true, isDefault: true }], defaultModelId: history.lastModel }
    else if (url.pathname === '/api/conversation/config') data = { dayRanges: [7, 30] }
    else if (url.pathname === '/api/conversation/history') data = { items: [{ ...history, status: 'idle', hasPendingInterrupt: false, updatedAt: new Date().toISOString() }], nextCursor: null }
    else if (url.pathname === `/api/conversation/${history.threadId}/history`) data = history
    else if (url.pathname.endsWith('/trace')) {
      await route.fulfill({ contentType: 'text/event-stream', body: `event: trace\ndata: ${JSON.stringify({ type: 'snapshot', snapshot: history })}\n\n` }); return
    } else if (url.pathname.startsWith('/objects/')) {
      if (url.searchParams.get('variant') === 'preview' && !previewAvailable) { await route.fulfill({ status: 404, body: '' }); return }
      if (url.searchParams.get('variant') === 'original' && !originalAvailable) { await route.fulfill({ status: 503, body: 'unavailable' }); return }
      await route.fulfill({ path: resolve(process.cwd(), 'tests/browser/fixtures/chart.png'), contentType: 'image/png' }); return
    }
    await route.fulfill({ json: { code: 0, message: 'success', data } })
  })
  await mockDownloadPermits(page)
  await page.goto('/')
  await page.getByRole('button', { name: '打开会话：多图预览回归', exact: true }).click()
  const preview = page.getByRole('button', { name: '放大图片：第一张.png', exact: true })
  await expect(preview).toBeDisabled()
  const actions = page.getByRole('group', { name: '图片操作', exact: true }).first()
  await expect(actions.getByRole('button', { name: '重试附件', exact: true })).toBeVisible()
  await expect(actions.getByRole('button', { name: '引用附件：第一张.png', exact: true })).toBeDisabled()
  await expect(actions.getByRole('button', { name: '下载附件：第一张.png', exact: true })).toBeDisabled()
  for (const theme of ['light', 'dark']) {
    await page.getByRole('button', { name: '打开用户菜单' }).click()
    await page.getByRole('menuitem', { name: '设置' }).click()
    await page.getByRole('button', { name: '通用', exact: true }).click()
    await page.getByText(theme === 'dark' ? '深色' : '浅色', { exact: true }).click()
    await page.getByRole('button', { name: '关闭对话框', exact: true }).click()
    await page.emulateMedia({ reducedMotion: 'reduce' })
    for (const width of [320, 768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 1000 })
      await expect(page.locator('html')).toHaveAttribute('data-theme', theme)
      const controls = actions.getByRole('button')
      const [card, ...controlBoxes] = await measureBounds(preview, ...await controls.all())
      expect(controlBoxes).toHaveLength(3)
      let previousRight: number | undefined
      for (const box of controlBoxes) {
        expect(box.x).toBeGreaterThanOrEqual(card.x)
        expect(box.y).toBeGreaterThanOrEqual(card.y)
        expect(box.x + box.width).toBeLessThanOrEqual(card.x + card.width)
        expect(box.y + box.height).toBeLessThanOrEqual(card.y + card.height)
        expect(box.width).toBe(box.height)
        if (previousRight !== undefined) expect(box.x - previousRight).toBe(2)
        previousRight = box.x + box.width
      }
      for (const control of await controls.all()) {
        await expect(control).toHaveCSS('border-width', '0px')
      }
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy()
      await page.screenshot({ path: testInfo.outputPath(`image-error-${theme}-${width}.png`) })
    }
  }
  previewAvailable = true
  await actions.getByRole('button', { name: '重试附件', exact: true }).click()
  await expect(preview).toBeEnabled()
  for (let remaining = 2; remaining > 0; remaining--) {
    const retries = page.getByRole('button', { name: '重试附件', exact: true })
    await expect(retries).toHaveCount(remaining)
    await retries.first().click()
    await expect(retries).toHaveCount(remaining - 1)
  }
  await preview.click()
  const dialog = page.getByRole('dialog')
  await expect(dialog.getByText('原图暂时无法加载，仍可查看预览')).toBeVisible()
  await expect(dialog.getByRole('img', { name: '第一张.png' })).toBeVisible()
  originalAvailable = true
  await dialog.getByRole('button', { name: '重试原图' }).click()
  await expect(dialog.getByText('原图暂时无法加载，仍可查看预览')).toHaveCount(0)
  await dialog.getByRole('button', { name: '关闭对话框' }).click()
  await expect(preview).toBeFocused()
  const returnedMedia = page.locator('.attachment-media').first()
  await expect.poll(() => returnedMedia.evaluate(element => getComputedStyle(element, '::after').opacity)).toBe('0')
  await expect(returnedMedia.locator('.attachment-image-actions')).toHaveCSS('opacity', '0')
  await preview.click()
  await expect(dialog).toBeVisible()
  const next = dialog.getByRole('button', { name: '下一张', exact: true })
  await next.focus()
  await page.keyboard.press('Enter')
  await expect(dialog).toHaveAccessibleName('第二张.png')
  await expect(next).toBeFocused()
  await page.keyboard.press('Enter')
  await expect(dialog).toHaveAccessibleName('第三张.png')
  await expect(next).toHaveAttribute('aria-disabled', 'true')
  await expect(next).toBeFocused()
  await page.keyboard.press('Enter')
  await expect(dialog).toHaveAccessibleName('第三张.png')
  await page.keyboard.press('Escape')
  await expect(preview).toBeFocused()
  for (const width of [320, 768, 1024, 1440]) {
    await page.setViewportSize({ width, height: 1000 })
    for (const name of ['第一张.png', '第二张.png', '第三张.png']) {
      const button = page.getByRole('button', { name: `放大图片：${name}`, exact: true })
      const image = button.getByRole('img')
      await expect(button).toBeVisible()
      await expect(image).toBeVisible()
      const [bounds, picture] = await measureBounds(button, image)
      expect(picture.width).toBeCloseTo(bounds.width, 1)
      await expect(button).toHaveCSS('border-width', '0px')
    }
  }
})
