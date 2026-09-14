import { mockDownloadPermits } from './support/attachment-storage'
import { test, expect } from '@playwright/test'
import { resolve } from 'node:path'
import history from './fixtures/multimodal-history.json' with { type: 'json' }

const sample = resolve(process.cwd(), 'tests/browser/fixtures/multimodal.png')

const user = {
  user_id: 1,
  username: 'multimodal-test',
  display_name: '多模态验收',
  avatar_url: null,
  roles: [],
  disabled: false,
}
const models = [
  {
    model_id: 'flash',
    display_name: 'DeepSeek-V4-Flash',
    image_support: 'unsupported',
    reasoning_enabled: true,
    is_default: false,
  },
  {
    model_id: 'vision',
    display_name: 'DeepSeek-V4-Flash-Vision-Exp',
    image_support: 'supported',
    reasoning_enabled: true,
    is_default: true,
  },
]

test('附件发送失败保留输入与待发送图片', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  await page.addInitScript(
    (user) =>
      localStorage.setItem(
        'tinkerfin.auth.session',
        JSON.stringify({
          token: 'browser-test-token',
          tokenType: 'Bearer',
          expiresAt: '2099-01-01T00:00:00.000Z',
          user,
        }),
      ),
    user,
  )
  await page.route('**/{api,objects}/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    let data: unknown = {}
    if (path === '/api/auth/me')
      data = { expires_at: '2099-01-01T00:00:00.000Z', user }
    else if (path === '/api/models')
      data = {
        items: models.map((model) => ({
          modelId: model.model_id,
          displayName: model.display_name,
          reasoningEnabled: model.reasoning_enabled,
          imageSupport: model.image_support,
          isDefault: model.is_default,
        })),
        defaultModelId: 'vision',
      }
    else if (path === '/api/conversation/config') data = { dayRanges: [7, 30] }
    else if (path === '/api/conversation/history')
      data = { items: [], nextCursor: null }
    else if (path === '/api/attachments/uploads')
      data = { attachment_id: 'test-image', url: new URL('/objects/upload', route.request().url()).href, fields: {}, expires_in: 600 }
    else if (path === '/objects/upload') { await route.fulfill({ status: 204 }); return }
    else if (path === '/api/attachments/test-image/complete')
      data = { id: 'test-image', name: 'multimodal.png', mime_type: 'image/png', size_bytes: 1494354 }
    else if (path === '/objects/test-image') {
      await route.fulfill({ path: sample, contentType: 'image/png' })
      return
    } else if (path === '/api/conversation/chat') {
      await route.fulfill({
        status: 422,
        json: { code: 422, message: '发送失败，请保留草稿重试', data: null },
      })
      return
    } else if (route.request().method() === 'DELETE') {
      await route.fulfill({ json: { code: 0, message: 'success', data: null } })
      return
    }
    await route.fulfill({ json: { code: 0, message: 'success', data } })
  })
  await mockDownloadPermits(page)
  await page.goto('/')
  await page.locator('input[type=file]').setInputFiles(sample)
  await expect(page.getByLabel('待发送附件').getByRole('img', { name: 'multimodal.png', exact: true })).toBeVisible()
  await page.getByRole('textbox', { name: '消息输入' }).fill('这张图里是什么？')
  await expect(
    page.getByRole('button', { name: '发送消息', exact: true }),
  ).toBeEnabled()
  await page.getByRole('button', { name: '发送消息', exact: true }).click()
  await expect(page.getByRole('textbox', { name: '消息输入' })).toHaveValue(
    '这张图里是什么？',
  )
  await expect(page.getByText('multimodal.png').first()).toBeVisible()
  expect(errors).toEqual([])
})

test('真实图表历史中的图片预览、下载与引用', async ({ page }, testInfo) => {
  await page.addInitScript(user => localStorage.setItem('tinkerfin.auth.session', JSON.stringify({ token: 'browser-token', tokenType: 'Bearer', expiresAt: '2099-01-01T00:00:00.000Z', user })), user)
  await page.route('**/{api,objects}/**', async route => {
    const url = new URL(route.request().url())
    const path = url.pathname
    let data: unknown = {}
    if (path === '/api/auth/me') data = { expires_at: '2099-01-01T00:00:00.000Z', user }
    else if (path === '/api/models') data = { items: [{ modelId: history.lastModel, displayName: 'DeepSeek Vision', imageSupport: 'supported', reasoningEnabled: true, isDefault: true }], defaultModelId: history.lastModel }
    else if (path === '/api/conversation/config') data = { dayRanges: [7, 30] }
    else if (path === '/api/conversation/history') data = { items: [{ ...history, status: 'idle', hasPendingInterrupt: false, updatedAt: new Date().toISOString() }], nextCursor: null }
    else if (path === `/api/conversation/${history.threadId}/history`) data = history
    else if (path === `/api/conversation/${history.threadId}/trace`) {
      await route.fulfill({ contentType: 'text/event-stream', body: `event: trace\ndata: ${JSON.stringify({ type: 'snapshot', snapshot: history })}\n\n` }); return
    } else if (path.startsWith('/objects/')) {
      await route.fulfill({ path: resolve(process.cwd(), 'tests/browser/fixtures/chart.png'), contentType: 'image/png' }); return
    }
    await route.fulfill({ json: { code: 0, message: 'success', data } })
  })
  await mockDownloadPermits(page)
  await page.goto('/')
  await page.getByRole('button', { name: `打开会话：${history.title}`, exact: true }).click()
  const preview = page.getByRole('button', { name: '放大图片：验收图表.png', exact: true })
  await expect(preview).toBeEnabled()
  await preview.click()
  await expect(page.getByRole('dialog').getByRole('img', { name: '验收图表.png', exact: true })).toBeVisible()
  await page.getByRole('button', { name: '关闭对话框', exact: true }).click()
  await expect(preview).toBeFocused()
  await preview.hover()
  const download = page.waitForEvent('download')
  await page.getByRole('button', { name: '下载附件：验收图表.png', exact: true }).click()
  expect((await download).suggestedFilename()).toBe('验收图表.png')
  await page.getByRole('button', { name: '引用附件：验收图表.png', exact: true }).click()
  await expect(page.getByLabel('待发送附件').getByRole('img', { name: '验收图表.png' })).toBeVisible()
  for (const theme of ['light', 'dark']) {
    await page.getByRole('button', { name: '打开用户菜单' }).click()
    await page.getByRole('menuitem', { name: '设置' }).click()
    await page.getByRole('button', { name: '通用', exact: true }).click()
    await page.getByText(theme === 'dark' ? '深色' : '浅色', { exact: true }).click()
    await page.getByRole('button', { name: '关闭对话框', exact: true }).click()
    await page.emulateMedia({ reducedMotion: 'reduce' })
    for (const width of [320, 768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 960 })
      await expect(preview).toBeVisible()
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy()
      await page.screenshot({ path: testInfo.outputPath( `media-${theme}-${width}.png`) })
    }
  }
})
