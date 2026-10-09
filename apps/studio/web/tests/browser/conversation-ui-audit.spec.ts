import { expect, test, type CDPSession, type Locator, type Page } from '@playwright/test'

import type { ConversationHistoryDetail } from '../../src/api/conversation/history'
import original from './fixtures/multimodal-history.json' with { type: 'json' }
import { installLiveRun } from './fixtures/liveRun'
import { installNotificationStream } from './fixtures/notifications'
import { installProjectScope } from './fixtures/projects'

const user = { user_id: 1, username: 'conversation-ui', display_name: '附件交互验收', avatar_url: null, roles: [], disabled: false }
const documentName = '项目交付附件完整文件名-需要在键盘与触控设备上完整阅读-原始资料归档.bin'
const draftNames = Array.from({ length: 5 }, (_, index) => `项目交付材料-${index + 1}-包含完整说明与验证记录的附件.pdf`)
const widths = [320, 768, 1024, 1440]

function snapshot(running = false): ConversationHistoryDetail {
  const source = original as ConversationHistoryDetail
  const file = { type: 'file', extras: { attachment: { id: 'document-ui', name: documentName, mime_type: 'application/octet-stream', size_bytes: 1024 } } }
  return {
    ...source,
    title: '附件与正文交互',
    messages: source.messages.map(message => ({
      ...message,
      content: message.role === 'tool' ? [file] : message.role === 'user' ? '请提供本次交付附件' : '附件已整理完成，可打开文件信息查看完整名称',
    })),
    graph: { ...source.graph, nodes: source.graph.nodes.map(node => node.kind === 'tool' ? { ...node, result: [file] } : node) },
    status: { ...source.status, execution: running ? 'running' : 'succeeded' },
  }
}

async function openWorkspace(page: Page, history?: ConversationHistoryDetail) {
  const uploads = new Map<string, number>()
  await page.addInitScript(user => {
    localStorage.setItem('tinkerfin.auth.session', JSON.stringify({ token: 'browser-token', serverAddress: 'http://127.0.0.1:8090', tokenType: 'Bearer', expiresAt: '2099-01-01T00:00:00.000Z', user }))
    localStorage.setItem('tinkerfin:theme', 'system')
  }, user)
  await page.route('**/{api,objects}/**', async route => {
    const url = new URL(route.request().url())
    const path = url.pathname
    let data: unknown = {}
    if (path === '/api/auth/me') data = { expires_at: '2099-01-01T00:00:00.000Z', user }
    else if (path === '/api/skills/installations') data = []
    else if (path === '/api/models') data = { items: [{ modelId: 'test-model', displayName: '测试模型', connectionId: 'test', connectionDisplayName: '测试提供方', reasoningEnabled: true, isDefault: true }], defaultModelId: 'test-model' }
    else if (path === '/api/conversation/config') data = { dayRanges: [7, 30] }
    else if (path === '/api/conversation/history') data = { items: history ? [{ ...history, status: history.status.execution === 'running' ? 'running' : 'idle', hasPendingInterrupt: false, updatedAt: '2099-01-01T00:00:00.000Z' }] : [], nextCursor: null }
    else if (history && path === `/api/conversation/${history.threadId}/history`) data = history
    else if (history && path.endsWith('/trace')) {
      await route.fulfill({ contentType: 'text/event-stream', body: `event: trace\ndata: ${JSON.stringify({ type: 'snapshot', snapshot: history })}\n\n` })
      return
    } else if (path === '/api/attachments/uploads') {
      const name = route.request().postDataJSON().name as string
      const attempts = (uploads.get(name) ?? 0) + 1
      uploads.set(name, attempts)
      if (name === draftNames[0] && attempts === 1) {
        await route.fulfill({ status: 503, json: { code: 503, message: '上传暂不可用，请重试', data: null } })
        return
      }
      data = { attachment_id: `draft-${draftNames.indexOf(name)}`, url: new URL('/objects/upload', url).href, fields: {}, expires_in: 600 }
    } else if (path === '/objects/upload') {
      await route.fulfill({ status: 204 })
      return
    } else if (path.endsWith('/complete')) {
      const id = path.split('/')[3]
      data = { id, name: draftNames[Number(id.split('-')[1])], mime_type: 'application/pdf', size_bytes: 3 }
    } else if (route.request().method() === 'DELETE') data = null
    await route.fulfill({ json: { code: 0, message: 'success', data } })
  })
  await installNotificationStream(page)
  await installProjectScope(page)
  await page.goto('/')
  if (history) await page.getByRole('button', { name: `打开会话：附件与正文交互${history.status.execution === 'running' ? '，正在生成' : ''}`, exact: true }).click()
  return uploads
}

async function expectFullTouchTarget(button: Locator) {
  await button.scrollIntoViewIfNeeded()
  const target = await button.evaluate(element => {
    const box = element.getBoundingClientRect()
    const points = [box.top + 1, box.top + box.height / 2, box.bottom - 1]
    return {
      width: box.width,
      height: box.height,
      receivesTouch: points.every(y => element.contains(document.elementFromPoint(box.left + box.width / 2, y))),
    }
  })
  expect(target.width).toBeGreaterThanOrEqual(44)
  expect(target.height).toBeGreaterThanOrEqual(44)
  expect(target.receivesTouch).toBe(true)
}

async function tap(device: CDPSession, button: Locator) {
  await button.scrollIntoViewIfNeeded()
  const box = (await button.boundingBox())!
  await device.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x: box.x + box.width / 2, y: box.y + box.height / 2 }] })
  await device.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] })
}

for (const theme of ['light', 'dark'] as const) {
  test(`文件卡片保持单一预览入口，完整名称可由鼠标、键盘和触控阅读 ${theme}`, async ({ page }, testInfo) => {
    const errors: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    page.on('console', message => {
      if (message.type() === 'error' && /descendant|nested|hydration/i.test(message.text())) errors.push(message.text())
    })
    await page.emulateMedia({ colorScheme: theme, reducedMotion: 'reduce' })
    await openWorkspace(page, snapshot())
    const opener = page.getByRole('button', { name: `查看文件：${documentName}`, exact: true })
    for (const width of widths) {
      await page.setViewportSize({ width, height: 960 })
      await opener.scrollIntoViewIfNeeded()
      await expect(opener.locator('button')).toHaveCount(0)
      await opener.hover()
      const tooltip = page.getByRole('tooltip', { name: documentName, exact: true })
      await expect(tooltip).toBeVisible()
      await page.keyboard.press('Escape')
      await page.mouse.move(0, 0)
      await opener.focus()
      await page.keyboard.press('Tab')
      await page.keyboard.press('Shift+Tab')
      await expect(opener).toBeFocused()
      await expect(tooltip).toBeVisible()
      await page.screenshot({ path: testInfo.outputPath(`document-${theme}-${width}.png`) })
      await page.keyboard.press('Escape')
      await opener.getByText(documentName, { exact: true }).click()
      await expect(page.getByRole('dialog', { name: documentName, exact: true })).toHaveCount(1)
      await page.keyboard.press('Escape')
      await expect(opener).toBeFocused()
    }
    const device = await page.context().newCDPSession(page)
    await device.send('Emulation.setTouchEmulationEnabled', { enabled: true })
    for (const width of widths) {
      await page.setViewportSize({ width, height: 960 })
      await tap(device, opener)
      const dialog = page.getByRole('dialog', { name: documentName, exact: true })
      await expect(dialog).toHaveCount(1)
      const infoButton = dialog.getByRole('button', { name: '文件信息', exact: true })
      const closeButton = dialog.getByRole('button', { name: '关闭对话框', exact: true })
      await tap(device, infoButton)
      const information = dialog.getByRole('complementary', { name: '文件信息' })
      await expect(information.getByText(documentName, { exact: true })).toBeVisible()
      expect(await information.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true)
      await expectFullTouchTarget(infoButton)
      await expectFullTouchTarget(closeButton)
      await page.screenshot({ path: testInfo.outputPath(`document-info-touch-${theme}-${width}.png`) })
      await tap(device, infoButton)
      await expect(information).toHaveCount(0)
      await tap(device, closeButton)
      await expect(dialog).toHaveCount(0)
    }
    await device.detach()
    expect(errors).toEqual([])
  })

  test(`附件条完整承载触控名称、重试和移除目标 ${theme}`, async ({ page }, testInfo) => {
    const device = await page.context().newCDPSession(page)
    await device.send('Emulation.setTouchEmulationEnabled', { enabled: true })
    await page.emulateMedia({ colorScheme: theme, reducedMotion: 'reduce' })
    const uploads = await openWorkspace(page)
    await page.locator('input[type=file]').setInputFiles(draftNames.map(name => ({ name, mimeType: 'application/pdf', buffer: Buffer.from('PDF') })))
    const strip = page.getByLabel('待发送附件')
    const failed = page.getByRole('group', { name: draftNames[0], exact: true })
    await expect(failed.getByRole('status')).toHaveText('上传失败')
    for (const width of widths) {
      await page.setViewportSize({ width, height: 960 })
      expect(await strip.evaluate(element => element.scrollWidth > element.clientWidth)).toBe(true)
      for (const button of await failed.getByRole('button').all()) await expectFullTouchTarget(button)
      const last = page.getByRole('group', { name: draftNames.at(-1)!, exact: true })
      await expectFullTouchTarget(last.getByRole('button', { name: `移除附件：${draftNames.at(-1)}` }))
      expect(await strip.evaluate(element => element.scrollLeft)).toBeGreaterThan(0)
      await failed.scrollIntoViewIfNeeded()
      await page.screenshot({ path: testInfo.outputPath(`draft-touch-${theme}-${width}.png`) })
    }
    await page.setViewportSize({ width: 320, height: 960 })
    const filename = failed.getByRole('button', { name: draftNames[0], exact: true })
    await tap(device, filename)
    await expect(page.getByRole('tooltip', { name: draftNames[0], exact: true })).toBeVisible()
    await page.getByRole('textbox', { name: '消息输入' }).click()
    await failed.getByRole('button', { name: `重试附件：${draftNames[0]}` }).click()
    await expect(failed.getByRole('status')).toHaveCount(0)
    expect(uploads.get(draftNames[0])).toBe(2)
    await failed.getByRole('button', { name: `移除附件：${draftNames[0]}` }).click()
    await expect(failed).toHaveCount(0)
    await device.detach()
  })

  test(`减少动效在实时回复中立即显示正文并响应偏好切换 ${theme}`, async ({ page }, testInfo) => {
    await page.emulateMedia({ colorScheme: theme, reducedMotion: 'reduce' })
    const history = snapshot(true)
    const live = await installLiveRun(page, history)
    await openWorkspace(page, history)
    await live.emit(
      { type: 'TEXT_MESSAGE_START', messageId: 'motion-answer', role: 'assistant' },
      { type: 'TEXT_MESSAGE_CONTENT', messageId: 'motion-answer', delta: '实时正文遵守减少动效偏好' },
    )
    const answer = page.locator('[id="motion-answer"]')
    await expect(answer).toContainText('实时正文遵守减少动效偏好')
    await page.emulateMedia({ reducedMotion: 'no-preference' })
    await page.clock.install({ time: new Date('2030-01-01T00:00:00Z') })
    await page.clock.pauseAt(new Date('2030-01-01T00:00:01Z'))
    await live.emit({ type: 'TEXT_MESSAGE_CONTENT', messageId: 'motion-answer', delta: '，切换偏好后立即呈现全部新增内容' })
    await page.emulateMedia({ reducedMotion: 'reduce' })
    await page.clock.runFor(100)
    await expect(answer).toContainText('实时正文遵守减少动效偏好，切换偏好后立即呈现全部新增内容')
    for (const width of widths) {
      await page.setViewportSize({ width, height: 960 })
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
      await page.screenshot({ path: testInfo.outputPath(`text-reduced-${theme}-${width}.png`) })
    }
    await live.finish()
  })
}
