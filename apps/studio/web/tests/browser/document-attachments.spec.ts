import { mockDownloadPermits } from './support/attachment-storage'
import { measureBounds } from './support/geometry'
import { expect, test } from '@playwright/test'
import { resolve } from 'node:path'

import source from './fixtures/multimodal-history.json' with { type: 'json' }

const user = {
  user_id: 1,
  username: 'document-attachment-test',
  display_name: '文件附件验收',
  avatar_url: null,
  roles: [],
  disabled: false,
}

const attachments = [
  { id: 'proposal', name: '项目实施方案.docx', mime_type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', size_bytes: 188_416 },
  { id: 'report', name: '项目摘要报告.pdf', mime_type: 'application/pdf', size_bytes: 2_516_582 },
  { id: 'readme', name: 'README.md', mime_type: 'text/markdown', size_bytes: 8_192 },
  { id: 'quote', name: '项目报价表.xlsx', mime_type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', size_bytes: 98_304 },
  { id: 'slides', name: '项目汇报演示.pptx', mime_type: 'application/vnd.openxmlformats-officedocument.presentationml.presentation', size_bytes: 6_081_741 },
  { id: 'unknown', name: 'model-weights.bin', mime_type: 'application/octet-stream', size_bytes: 50_331_648 },
]
const imageAttachment = {
  id: 'preview-image',
  name: '项目封面.png',
  mime_type: 'image/png',
  size_bytes: 4_881,
}

const documentHistory = {
  ...source,
  title: '项目交付文件',
  messages: source.messages.map(message => message.role === 'tool'
    ? {
        ...message,
        name: 'deliver_file',
        content: [imageAttachment, ...attachments].map(attachment => ({
          type: 'file',
          extras: { attachment },
        })),
      }
    : message),
}

test('生成文件使用紧凑类型卡片并沿用图片预览工具栏', async ({ page }, testInfo) => {
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  await page.addInitScript(user => {
    localStorage.setItem('tinkerfin.auth.session', JSON.stringify({
      token: 'browser-token',
      serverAddress: 'http://127.0.0.1:8090', tokenType: 'Bearer',
      expiresAt: '2099-01-01T00:00:00.000Z',
      user,
    }))
    localStorage.setItem('tinkerfin:theme', 'system')
  }, user)
  await page.route('**/{api,objects}/**', async route => {
    const url = new URL(route.request().url())
    const path = url.pathname
    let data: unknown = {}
    if (path === '/api/auth/me')
      data = { expires_at: '2099-01-01T00:00:00.000Z', user }
    else if (path === '/api/models')
      data = {
        items: [{
          modelId: documentHistory.lastModel,
          displayName: 'DeepSeek',

          connectionId: 'test-provider', connectionDisplayName: '测试提供方', reasoningEnabled: true,
          isDefault: true,
        }],
        defaultModelId: documentHistory.lastModel,
      }
    else if (path === '/api/conversation/config')
      data = { dayRanges: [7, 30] }
    else if (path === '/api/conversation/history')
      data = {
        items: [{
          ...documentHistory,
          status: 'idle',
          hasPendingInterrupt: false,
          updatedAt: new Date().toISOString(),
        }],
        nextCursor: null,
      }
    else if (path === `/api/conversation/${documentHistory.threadId}/history`)
      data = documentHistory
    else if (path === `/api/conversation/${documentHistory.threadId}/trace`) {
      await route.fulfill({
        contentType: 'text/event-stream',
        body: `event: trace\ndata: ${JSON.stringify({ type: 'snapshot', snapshot: documentHistory })}\n\n`,
      })
      return
    } else if (path === '/objects/readme') {
      await route.fulfill({
        body: '# TinkerFin Studio\n\n文件预览与图片预览使用同一套工具栏。\n\n## 使用\n\n点击文件卡片即可打开预览。',
        contentType: 'text/markdown',
      })
      return
    } else if (path === '/objects/preview-image') {
      await route.fulfill({
        path: resolve(process.cwd(), 'tests/browser/fixtures/chart.png'),
        contentType: 'image/png',
      })
      return
    }
    await route.fulfill({ json: { code: 0, message: 'success', data } })
  })

  await mockDownloadPermits(page)
  await page.goto('/')
  await page.getByRole('button', { name: `打开会话：${documentHistory.title}`, exact: true }).click()
  const fileCards = page.locator('.attachment-card:not(.attachment-card--image)')
  await expect(fileCards).toHaveCount(attachments.length)
  await expect(page.locator('.attachment-file-icon[data-file-kind="word"]')).toHaveText('DOCX')
  await expect(page.locator('.attachment-file-icon[data-file-kind="pdf"]')).toHaveText('PDF')
  await expect(page.locator('.attachment-file-icon[data-file-kind="markdown"]')).toHaveText('MD')
  await expect(page.locator('.attachment-file-icon[data-file-kind="sheet"]')).toHaveText('XLSX')
  await expect(page.locator('.attachment-file-icon[data-file-kind="slides"]')).toHaveText('PPTX')
  await expect(page.locator('.attachment-file-icon[data-file-kind="unknown"]')).toHaveText('BIN')

  for (const theme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: theme })
    await expect(page.locator('html')).toHaveAttribute('data-theme', theme)
    for (const width of [320, 768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 960 })
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy()
      const [cardBox, iconBox, imageBox] = await measureBounds(
        fileCards.first(),
        fileCards.first().locator('.attachment-file-icon'),
        page.locator('.attachment-card--image').first(),
      )
      expect(iconBox.y).toBeGreaterThanOrEqual(cardBox.y)
      expect(iconBox.y + iconBox.height).toBeLessThanOrEqual(cardBox.y + cardBox.height)
      expect(imageBox.width).toBe(cardBox.width)
      expect(iconBox).toMatchObject({ width: 44, height: 48 })
      await page.screenshot({ path: testInfo.outputPath(`files-${theme}-${width}.png`) })
    }

    await page.setViewportSize({ width: 1440, height: 960 })
    const imageOpener = page.getByRole('button', { name: '放大图片：项目封面.png', exact: true })
    await expect(imageOpener).toBeEnabled()
    await imageOpener.click()
    const imageDialog = page.getByRole('dialog', { name: '项目封面.png', exact: true })
    await expect(imageDialog.getByRole('img', { name: '项目封面.png', exact: true })).toBeVisible()
    const imageToolbar = await measureBounds(
      imageDialog.getByRole('button', { name: '引用附件：项目封面.png', exact: true }),
      imageDialog.getByRole('button', { name: '下载附件：项目封面.png', exact: true }),
      imageDialog.getByRole('button', { name: '图片信息', exact: true }),
      imageDialog.getByRole('button', { name: '关闭对话框', exact: true }),
    )
    await imageDialog.getByRole('button', { name: '关闭对话框', exact: true }).click()

    const readme = page.getByRole('button', { name: '预览文档：README.md', exact: true })
    const readmeCard = page.locator('.attachment-card').filter({ hasText: 'README.md' })
    const cardBackground = await readmeCard.evaluate(element => getComputedStyle(element).backgroundColor)
    await readme.click()
    const dialog = page.getByRole('dialog', { name: 'README.md', exact: true })
    await expect(dialog).toBeVisible()
    await expect(readmeCard.locator('.attachment-file-actions')).toHaveCSS('opacity', '0')
    await expect(readmeCard.locator('.attachment-file-actions')).toHaveCSS('pointer-events', 'none')
    const dialogBox = (await dialog.boundingBox())!
    expect(dialogBox).toMatchObject({ x: 0, y: 0, width: 1440, height: 960 })
    const title = dialog.locator('.attachment-document-title')
    await expect(title).toHaveCSS('font-weight', '400')
    const documentContent = dialog.getByRole('region', { name: '文档预览', exact: true })
    await expect(documentContent.getByRole('heading', { name: 'TinkerFin Studio', exact: true })).toHaveCSS('font-weight', '600')
    await expect(documentContent.getByText('点击文件卡片即可打开预览。', { exact: true })).toHaveCSS('font-weight', '400')
    await expect(title.locator('.attachment-file-icon')).toHaveText('MD')
    await expect(title.locator('.attachment-document-title__copy > span')).toHaveText('README.md')
    const rail = dialog.getByRole('complementary', { name: '本次交付文件', exact: true })
    await expect(rail.getByRole('button')).toHaveCount(attachments.length)
    await expect(rail.getByRole('button', { name: '查看文件：README.md', exact: true })).toHaveAttribute('aria-current', 'true')
    for (const name of ['引用附件：README.md', '下载附件：README.md', '文件信息', '关闭对话框']) {
      const button = dialog.getByRole('button', { name, exact: true })
      await expect(button).toBeVisible()
      await expect(button.locator('.ui-button__label')).toHaveCount(0)
    }
    const documentToolbar = await measureBounds(
      dialog.getByRole('button', { name: '引用附件：README.md', exact: true }),
      dialog.getByRole('button', { name: '下载附件：README.md', exact: true }),
      dialog.getByRole('button', { name: '文件信息', exact: true }),
      dialog.getByRole('button', { name: '关闭对话框', exact: true }),
    )
    expect(documentToolbar).toEqual(imageToolbar)
    await page.screenshot({ path: testInfo.outputPath(`preview-${theme}.png`) })
    await rail.getByRole('button', { name: '查看文件：model-weights.bin', exact: true }).click()
    await expect(page.getByRole('dialog', { name: 'model-weights.bin', exact: true })).toBeVisible()
    await expect(page.getByRole('heading', { name: '此格式无法在线预览', exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: '下载文件', exact: true })).toBeVisible()
    await page.screenshot({ path: testInfo.outputPath(`preview-unknown-${theme}.png`) })
    const unknownDialog = page.getByRole('dialog', { name: 'model-weights.bin', exact: true })
    await unknownDialog.getByRole('button', { name: '查看文件：README.md', exact: true }).click()
    await expect(dialog).toBeVisible()
    await dialog.getByRole('button', { name: '关闭对话框', exact: true }).click()
    await expect(readme).toBeFocused()
    await expect(readmeCard.locator('.attachment-file-actions')).toHaveCSS('opacity', '0')
    await expect(readmeCard.locator('.attachment-file-actions')).toHaveCSS('pointer-events', 'none')
    await readmeCard.hover()
    await expect(readmeCard.locator('.attachment-file-actions')).toHaveCSS('opacity', '1')
    await page.mouse.move(0, 0)
    await expect(readmeCard.locator('.attachment-file-actions')).toHaveCSS('opacity', '0')
    await readmeCard.locator('.attachment-description__title').hover()
    await expect(page.getByRole('tooltip')).toHaveCount(0)
    const slidesCard = page.locator('.attachment-card').filter({ hasText: '项目汇报演示.pptx' })
    await slidesCard.locator('.attachment-description__title').hover()
    await expect(page.getByRole('tooltip')).toHaveCount(0)
    await page.mouse.move(0, 0)
    await expect(readmeCard).toHaveCSS('background-color', cardBackground)
  }
  expect(errors).toEqual([])
})
