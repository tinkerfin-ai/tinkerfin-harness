import { expect, test } from '@playwright/test'
import { readFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { mockDownloadPermits } from './support/attachment-storage'

const user = { user_id: 1, username: 'attachment-test', display_name: '附件验收', avatar_url: null, roles: [], disabled: false }
const imageName = 'AI架构应用开发工程师-附件截图.png'
const sample = resolve(process.cwd(), 'tests/browser/fixtures/chart.png')
const modelCatalog = {
  items: [
    { modelId: 'flash', displayName: 'DeepSeek-V4-Flash',connectionId: 'test-provider', connectionDisplayName: '测试提供方', reasoningEnabled: true, isDefault: true },
    { modelId: 'vision', displayName: '视觉模型',connectionId: 'test-provider', connectionDisplayName: '测试提供方', reasoningEnabled: true, isDefault: false },
    { modelId: 'unknown', displayName: '自定义模型',connectionId: 'test-provider', connectionDisplayName: '测试提供方', reasoningEnabled: true, isDefault: false },
  ], defaultModelId: 'flash',
}

for (const source of ['picker', 'paste', 'drop'] as const) {
  test(`${source} 添加附件后显示失败、允许重试与移除，全部就绪后允许发送`, async ({ page }, testInfo) => {
    const errors: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    let finishFirst = () => {}
    let finishRetry = () => {}
    const firstUpload = new Promise<void>(resolve => { finishFirst = resolve })
    const retriedUpload = new Promise<void>(resolve => { finishRetry = resolve })
    let imageUploads = 0
    let submissions = 0
    await page.addInitScript(user => {
      localStorage.setItem('tinkerfin.auth.session', JSON.stringify({ token: 'browser-token', serverAddress: 'http://127.0.0.1:8090', tokenType: 'Bearer', expiresAt: '2099-01-01T00:00:00.000Z', user }))
      localStorage.setItem('tinkerfin:theme', 'system')
    }, user)
    await page.route('**/{api,objects}/**', async route => {
      const url = new URL(route.request().url())
      const path = url.pathname
      let data: unknown = {}
      if (path === '/api/auth/me') data = { expires_at: '2099-01-01T00:00:00.000Z', user }
      else if (path === '/api/models') data = modelCatalog
      else if (path === '/api/conversation/config') data = { dayRanges: [7, 30] }
      else if (path === '/api/conversation/history') data = { items: [], nextCursor: null }
      else if (path === '/api/attachments/uploads' && route.request().method() === 'POST') {
        const name = route.request().postDataJSON().name as string
        if (name === imageName) {
          imageUploads += 1
          if (imageUploads === 1) {
            await firstUpload
            await route.abort('failed')
            return
          }
          await retriedUpload
        }
        data = { attachment_id: name === imageName ? 'stored-image' : 'stored-document', url: new URL('/objects/upload', url).href, fields: {}, expires_in: 600 }
      } else if (path === '/objects/upload') { await route.fulfill({ status: 204 }); return
      } else if (path.endsWith('/complete')) {
        const image = path.includes('stored-image')
        data = { id: image ? 'stored-image' : 'stored-document', name: image ? imageName : '报告.pdf', mime_type: image ? 'image/png' : 'application/pdf', size_bytes: 3 }
      } else if (path === '/api/conversation/chat') {
        submissions += 1
        await route.fulfill({ status: 422, json: { code: 422, message: '发送失败，请保留草稿重试', data: null } })
        return
      } else if (route.request().method() === 'DELETE') data = null
      await route.fulfill({ json: { code: 0, message: 'success', data } })
    })
    try {
      if (source === 'picker') await page.setViewportSize({ width: 320, height: 960 })
      await page.goto('/')
      const input = page.getByRole('textbox', { name: '消息输入' })
      const send = page.getByRole('button', { name: '发送消息', exact: true })
      await input.fill('保留我的正文')
      await page.locator('input[type=file]').setInputFiles({ name: '报告.pdf', mimeType: 'application/pdf', buffer: Buffer.from('pdf') })
      await expect(send).toBeEnabled()
      const bytes = await readFile(sample)
      if (source === 'picker') {
        const chooserEvent = page.waitForEvent('filechooser')
        await page.getByRole('button', { name: '添加本地附件', exact: true }).click()
        await (await chooserEvent).setFiles({ name: imageName, mimeType: 'image/png', buffer: bytes })
      } else {
        await input.evaluate((element, { source, name, base64 }) => {
          const transfer = new DataTransfer()
          transfer.items.add(new File([Uint8Array.from(atob(base64), char => char.charCodeAt(0))], name, { type: 'image/png' }))
          element.dispatchEvent(source === 'paste'
            ? new ClipboardEvent('paste', { clipboardData: transfer, bubbles: true, cancelable: true })
            : new DragEvent('drop', { dataTransfer: transfer, bubbles: true, cancelable: true }))
        }, { source, name: imageName, base64: bytes.toString('base64') })
      }
      const card = page.getByRole('group', { name: imageName, exact: true })
      await expect(card).toHaveAttribute('aria-busy', 'true')
      await expect(card.getByRole('status')).toContainText('上传中')
      await expect(send).toBeDisabled()
      await input.press('Enter')
      expect(submissions).toBe(0)
      if (source === 'picker') {
        await page.emulateMedia({ reducedMotion: 'reduce' })
        await expect(card.locator('svg').first()).toHaveCSS('animation-name', 'none')
        await page.screenshot({ path: testInfo.outputPath('uploading-reduced-motion.png') })
      }
      finishFirst()
      await expect(card.getByRole('status')).toHaveText('上传失败')
      await expect(card.getByRole('button', { name: `重试附件：${imageName}` })).toBeEnabled()
      await expect(send).toBeDisabled()
      await input.press('Enter')
      expect(submissions).toBe(0)
      await expect(input).toHaveValue('保留我的正文')
      await expect(input).toHaveAccessibleDescription('')
      await expect(send).toHaveAccessibleDescription('')

      if (source === 'picker') {
        let checkedScrollToEnd = false
        for (const theme of ['light', 'dark'] as const) {
          await page.emulateMedia({ colorScheme: theme })
          await expect(page.locator('html')).toHaveAttribute('data-theme', theme)
          for (const width of [320, 768, 1024, 1440]) {
            await page.setViewportSize({ width, height: 960 })
            await expect(card.getByRole('status')).toBeInViewport()
            if (width >= 768 || !checkedScrollToEnd) {
              await expect(card.getByRole('button', { name: `移除附件：${imageName}` })).toBeInViewport()
              checkedScrollToEnd = true
            }
            expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy()
            // 单行高度及中性卡片是用户确认的视觉约束
            expect((await card.boundingBox())!.height).toBe(40)
            const documentCard = page.getByRole('group', { name: '报告.pdf', exact: true })
            const documentBox = (await documentCard.boundingBox())!
            const iconBox = (await documentCard.locator('.composer-attachment-icon').boundingBox())!
            const deleteBox = (await documentCard.getByRole('button', { name: '移除附件：报告.pdf' }).boundingBox())!
            expect(iconBox.width).toBe(deleteBox.width)
            expect(iconBox.x - documentBox.x).toBe(documentBox.x + documentBox.width - deleteBox.x - deleteBox.width)
            const contrast = await card.getByRole('status').evaluate(element => {
              const css = getComputedStyle(element)
              const luminance = (color: string) => {
                const channels = color.match(/[\d.]+/g)!.slice(0, 3).map(Number).map(value => {
                  const v = value / 255
                  return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4
                })
                return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722
              }
              const foreground = luminance(css.color)
              const background = luminance(css.backgroundColor)
              return (Math.max(foreground, background) + 0.05) / (Math.min(foreground, background) + 0.05)
            })
            expect(contrast).toBeGreaterThanOrEqual(4.5)
            const retryBox = (await card.getByRole('button', { name: `重试附件：${imageName}` }).boundingBox())!
            const removeBox = (await card.getByRole('button', { name: `移除附件：${imageName}` }).boundingBox())!
            expect(retryBox.width).toBe(24)
            expect(removeBox.width).toBe(24)
            expect(removeBox.x - retryBox.x - retryBox.width).toBe(0)
            await page.mouse.move(0, 0)
            const filename = card.getByRole('button', { name: imageName, exact: true })
            await filename.hover()
            const tooltip = page.getByRole('tooltip', { name: imageName, exact: true })
            await expect(tooltip).toBeVisible()
            const tooltipBox = (await tooltip.boundingBox())!
            expect(tooltipBox.x).toBeGreaterThanOrEqual(0)
            expect(tooltipBox.x + tooltipBox.width).toBeLessThanOrEqual(width)
            expect(await tooltip.evaluate(element => {
              const box = element.getBoundingClientRect()
              return element.contains(document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2))
            })).toBeTruthy()
            await page.screenshot({ path: testInfo.outputPath(`filename-${theme}-${width}.png`) })
            await page.keyboard.press('Escape')
            await expect(tooltip).toHaveCount(0)
            await filename.focus()
            await expect(tooltip).toBeVisible()
            await page.keyboard.press('Escape')
            await input.focus()
            await page.mouse.move(0, 0)
            await page.screenshot({ path: testInfo.outputPath(`attachment-${theme}-${width}.png`) })
          }
        }
        const device = await page.context().newCDPSession(page)
        await device.send('Emulation.setTouchEmulationEnabled', { enabled: true })
        for (const theme of ['light', 'dark'] as const) {
          await page.emulateMedia({ colorScheme: theme })
          for (const width of [320, 768, 1024, 1440]) {
            await page.setViewportSize({ width, height: 960 })
            expect((await card.boundingBox())!.height).toBe(44)
            for (const button of await card.getByRole('button').all()) {
              const box = (await button.boundingBox())!
              expect(box.width).toBeGreaterThanOrEqual(44)
              expect(box.height).toBe(44)
            }
            const retry = (await card.getByRole('button', { name: `重试附件：${imageName}` }).boundingBox())!
            const remove = (await card.getByRole('button', { name: `移除附件：${imageName}` }).boundingBox())!
            expect(remove.x - retry.x - retry.width).toBe(0)
            await page.screenshot({ path: testInfo.outputPath(`attachment-touch-${theme}-${width}.png`) })
          }
        }
        await page.setViewportSize({ width: 320, height: 960 })
        await card.getByRole('button', { name: imageName, exact: true }).click()
        await expect(page.getByRole('tooltip', { name: imageName, exact: true })).toBeVisible()
        await input.click()
        await expect(page.getByRole('tooltip', { name: imageName, exact: true })).toHaveCount(0)
        await page.screenshot({ path: testInfo.outputPath('attachment-touch-320.png') })
        await device.send('Emulation.setTouchEmulationEnabled', { enabled: false })
        await device.detach()
      }
      await page.getByRole('button', { name: '选择模型', exact: true }).click()
      await expect(page.getByRole('listbox', { name: '模型选项' })).toBeFocused()
      await page.getByRole('option', { name: '自定义模型', exact: true }).click()
      await expect(send).toBeDisabled()
      await page.getByRole('button', { name: '选择模型', exact: true }).click()
      await page.getByRole('option', { name: '视觉模型', exact: true }).click()
      await expect(page.getByRole('button', { name: '选择模型', exact: true })).toBeFocused()
      await expect(input).toHaveAccessibleDescription('')
      await expect(send).toHaveAccessibleDescription('')
      await expect(send).toBeDisabled()
      if (source === 'drop') {
        await card.getByRole('button', { name: `移除附件：${imageName}` }).click()
        await expect(card).toHaveCount(0)
      } else {
        await card.getByRole('button', { name: `重试附件：${imageName}` }).click()
        await expect(card).toHaveAttribute('aria-busy', 'true')
        await expect(send).toBeDisabled()
        finishRetry()
        await expect(card).not.toHaveAttribute('aria-busy')
        await expect(card.getByText('上传失败')).toHaveCount(0)
        await expect(card.getByRole('img', { name: imageName })).toBeVisible()
      }
      await expect(send).toBeEnabled()
      await expect(page.getByRole('group', { name: '报告.pdf', exact: true })).toBeVisible()
      await expect(input).toHaveValue('保留我的正文')
      expect(errors).toEqual([])
    } finally {
      finishFirst()
      finishRetry()
    }
  })
}

for (const locale of ['zh-CN', 'en'] as const) {
  test(`已上传图片可使用所选模型提交，失败保留正文与附件 ${locale}`, async ({ page }, testInfo) => {
    const english = locale === 'en'
    const submissions: string[] = []
    await page.addInitScript(({ user, locale }) => {
      localStorage.setItem('tinkerfin.auth.session', JSON.stringify({ token: 'browser-token', serverAddress: 'http://127.0.0.1:8090', tokenType: 'Bearer', expiresAt: '2099-01-01T00:00:00.000Z', user }))
      localStorage.setItem('tinkerfin:language', locale)
      localStorage.setItem('tinkerfin:theme', 'system')
    }, { user, locale })
    await page.route('**/{api,objects}/**', async route => {
      const url = new URL(route.request().url())
      const path = url.pathname
      let data: unknown = {}
      if (path === '/api/auth/me') data = { expires_at: '2099-01-01T00:00:00.000Z', user }
      else if (path === '/api/models') data = modelCatalog
      else if (path === '/api/conversation/config') data = { dayRanges: [7, 30] }
      else if (path === '/api/conversation/history') data = { items: [], nextCursor: null }
      else if (path === '/api/attachments/uploads') data = { attachment_id: 'ready-image', url: new URL('/objects/upload', url).href, fields: {}, expires_in: 600 }
      else if (path === '/objects/upload') { await route.fulfill({ status: 204 }); return }
      else if (path.endsWith('/complete')) data = { id: 'ready-image', name: imageName, mime_type: 'image/png', size_bytes: 3 }
      else if (path === '/objects/ready-image') { await route.fulfill({ path: sample, contentType: 'image/png' }); return }
      else if (path === '/api/conversation/chat') {
        const payload = route.request().postDataJSON()
        submissions.push(payload.forwardedProps.model)
        expect(payload.messages[0].content).toEqual(expect.arrayContaining([
          expect.objectContaining({ type: 'image', source: expect.objectContaining({ value: 'attachment:ready-image' }) }),
        ]))
        await route.fulfill({ status: 422, json: { code: 422, message: '发送失败', data: null } })
        return
      } else if (route.request().method() === 'DELETE') data = null
      await route.fulfill({ json: { code: 0, message: 'success', data } })
    })
    await mockDownloadPermits(page)
    await page.goto('/')
    await expect(page.locator('html')).toHaveAttribute('lang', locale)
    const input = page.getByRole('textbox', { name: english ? 'Message input' : '消息输入' })
    const send = page.getByRole('button', { name: english ? 'Send message' : '发送消息', exact: true })
    const model = page.getByRole('button', { name: english ? 'Select model' : '选择模型', exact: true })
    await input.fill('保留正文与附件')
    await page.locator('input[type=file]').setInputFiles({ name: imageName, mimeType: 'image/png', buffer: await readFile(sample) })
    const card = page.getByRole('group', { name: imageName, exact: true })
    await expect(card.getByRole('img', { name: imageName })).toBeVisible()
    await expect(card).not.toHaveAttribute('aria-busy')
    for (const [index, candidate] of modelCatalog.items.entries()) {
      await model.click()
      await expect(page.getByRole('option')).toHaveText(modelCatalog.items.map(item => item.displayName))
      await page.getByRole('option', { name: candidate.displayName, exact: true }).click()
      await expect(send).toBeEnabled()
      await expect(input).toHaveAccessibleDescription('')
      await expect(send).toHaveAccessibleDescription('')
      if (index === 1) await input.press('Enter')
      else await send.click()
      await expect.poll(() => submissions.length).toBe(index + 1)
      expect(submissions[index]).toBe(candidate.modelId)
      await expect(input).toHaveValue('保留正文与附件')
      await expect(card).toBeVisible()
      await expect(send).toBeEnabled()
    }
    await page.emulateMedia({ reducedMotion: 'reduce' })
    await page.setViewportSize({ width: 320, height: 960 })
    for (const theme of ['light', 'dark'] as const) {
      await page.emulateMedia({ colorScheme: theme })
      await expect(card).toBeInViewport()
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
      await page.screenshot({ path: testInfo.outputPath(`image-draft-${locale}-${theme}-320.png`) })
    }
    await card.getByRole('button', { name: english ? `Remove attachment: ${imageName}` : `移除附件：${imageName}`, exact: true }).click()
    await expect(card).toHaveCount(0)
    await expect(send).toBeEnabled()
    await expect(input).toHaveValue('保留正文与附件')
  })
}
