import { expect, test, type Page } from '@playwright/test'

const user = { user_id: 1, username: 'composer-test', display_name: '输入区验收', avatar_url: null, roles: [], disabled: false }
const names = ['deepseek-v4-pro', 'DeepSeek-V4-Flash', 'DeepSeek-V4-Flash-Vision']

function textStyle(element: Element) {
  const style = getComputedStyle(element)
  const menu = element.closest('[role="listbox"]')!
  const menuStyle = getComputedStyle(menu)
  return {
    fontFamily: style.fontFamily,
    fontSize: style.fontSize,
    fontWeight: style.fontWeight,
    lineHeight: style.lineHeight,
    letterSpacing: style.letterSpacing,
    color: style.color,
    height: element.getBoundingClientRect().height,
    paddingTop: style.paddingTop,
    paddingBottom: style.paddingBottom,
    menuPaddingTop: menuStyle.paddingTop,
    menuPaddingBottom: menuStyle.paddingBottom,
  }
}

async function openComposer(page: Page, locale = 'zh-CN', theme = 'light') {
  await page.addInitScript(user => localStorage.setItem('tinkerfin.auth.session', JSON.stringify({ token: 'browser-token', serverAddress: 'http://127.0.0.1:8090', tokenType: 'Bearer', expiresAt: '2099-01-01T00:00:00.000Z', user })), user)
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    let data: unknown = {}
    if (path === '/api/auth/me') data = { expires_at: '2099-01-01T00:00:00.000Z', user }
    else if (path === '/api/models') data = { items: names.map((name, index) => ({ modelId: name, displayName: name,connectionId: 'test-provider', connectionDisplayName: '测试提供方', reasoningEnabled: true, isDefault: index === 1 })), defaultModelId: names[1] }
    else if (path === '/api/conversation/config') data = { dayRanges: [7, 30] }
    else if (path === '/api/conversation/history') data = { items: [], nextCursor: null }
    await route.fulfill({ json: { code: 0, message: 'success', data } })
  })
  await page.addInitScript(({ locale, theme }) => {
    localStorage.setItem('tinkerfin:language', locale)
    localStorage.setItem('tinkerfin:theme', theme)
  }, { locale, theme })
  await page.goto('/')
}

test('模型菜单紧凑布局与文件选择等待反馈', async ({ page }, testInfo) => {
  await openComposer(page)
  const model = page.getByRole('button', { name: '选择模型', exact: true })
  await expect(model).toBeEnabled()
  for (const theme of ['light', 'dark']) {
    await page.getByRole('button', { name: '打开用户菜单' }).click()
    await page.getByRole('menuitem', { name: '设置', exact: true }).click()
    await page.getByRole('button', { name: '通用', exact: true }).click()
    await page.getByText(theme === 'light' ? '浅色' : '深色', { exact: true }).click()
    await page.getByRole('button', { name: '关闭对话框', exact: true }).click()
    await page.emulateMedia({ reducedMotion: 'reduce' })
    for (const width of [320, 768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 1000 })
      await model.click()
      const list = page.getByRole('listbox', { name: '模型选项' })
      await expect(list).toBeVisible()
      await expect(list).toHaveCSS('outline-width', '0px')
      await expect(list).toHaveCSS('border-top-width', '0px')
      const box = (await list.boundingBox())!
      expect(box.x).toBeGreaterThanOrEqual(0)
      expect(box.x + box.width).toBeLessThanOrEqual(width)
      await page.screenshot({ path: testInfo.outputPath(`models-${theme}-${width}.png`) })
      await list.press('Escape')
      await expect(model).toBeFocused()
    }
  }
  await model.click()
  await page.getByRole('listbox', { name: '模型选项' }).press('Home')
  await page.getByRole('listbox', { name: '模型选项' }).press('Enter')
  await expect(model).toContainText(names[0])
  const add = page.getByRole('button', { name: '添加本地附件', exact: true })
  const chooserEvent = page.waitForEvent('filechooser')
  await add.click()
  const chooser = await chooserEvent
  await expect(add).toHaveAttribute('aria-busy', 'true')
  await expect(add).toBeDisabled()
  await page.screenshot({ path: testInfo.outputPath('file-picker-loading.png') })
  await chooser.setFiles([])
  await expect(add).toBeEnabled()
  await expect(add).not.toHaveAttribute('aria-busy')
  await expect(add).toBeFocused()
  const device = await page.context().newCDPSession(page)
  await device.send('Emulation.setTouchEmulationEnabled', { enabled: true })
  await page.setViewportSize({ width: 320, height: 1000 })
  for (const name of ['打开命令和技能', '添加本地附件', '选择访问权限']) {
    const bounds = await page.getByRole('button', { name, exact: true }).evaluate(element => {
      const bounds = element.getBoundingClientRect()
      return { width: bounds.width, height: bounds.height }
    })
    expect(bounds.width).toBeGreaterThanOrEqual(44)
    expect(bounds.height).toBeGreaterThanOrEqual(44)
  }
  await model.click()
  const touchList = page.getByRole('listbox', { name: '模型选项' })
  const touchBox = (await touchList.boundingBox())!
  expect(touchBox.x).toBeGreaterThanOrEqual(0)
  expect(touchBox.x + touchBox.width).toBeLessThanOrEqual(320)
  for (const option of await touchList.getByRole('option').all()) {
    expect((await option.boundingBox())!.height).toBeGreaterThanOrEqual(44)
  }
  await page.screenshot({ path: testInfo.outputPath('models-touch-320.png') })
  await device.detach()
})

test('加号直接打开命令菜单，保留草稿并支持键盘选择、取消及附件切换', async ({ page }) => {
  await openComposer(page)
  const input = page.getByRole('textbox', { name: '消息输入' })
  const commands = page.getByRole('button', { name: '打开命令和技能', exact: true })
  const attachment = page.getByRole('button', { name: '添加本地附件', exact: true })
  const menu = page.getByRole('listbox', { name: '命令和技能建议' })
  await input.fill('保留的草稿')
  await commands.focus()
  await commands.press('Enter')
  await expect(menu).toBeVisible()
  await expect(input).toBeFocused()
  await expect(input).toHaveValue('保留的草稿')
  await expect(menu.getByRole('option')).toHaveCount(5)
  await expect(menu.getByRole('option', { name: /compact/ })).toHaveAttribute('aria-selected', 'true')
  await input.press('ArrowDown')
  await expect(menu.getByRole('option', { name: /plan 进入 Plan 模式/ })).toHaveAttribute('aria-selected', 'true')
  await input.press('Enter')
  await expect(input).toHaveValue('/plan 保留的草稿')
  await expect(menu).toBeHidden()
  await commands.click()
  await menu.getByRole('option', { name: /plan 进入 Plan 模式/ }).click()
  await expect(input).toHaveValue('/plan 保留的草稿')
  await expect(menu).toBeHidden()

  await commands.click()
  await input.press('Escape')
  await expect(input).toHaveValue('/plan 保留的草稿')
  await expect(menu).toBeHidden()
  await commands.click()
  const chooserEvent = page.waitForEvent('filechooser')
  await attachment.click()
  const chooser = await chooserEvent
  await expect(menu).toBeHidden()
  await chooser.setFiles([])
  await expect(attachment).toBeFocused()
  await expect(input).toHaveValue('/plan 保留的草稿')

  await input.fill('/')
  await expect(menu).toBeVisible()
  await input.press('Escape')
  await expect(input).toHaveValue('')
  await expect(menu).toBeHidden()
})

test('model 指令支持鼠标、键盘和加号入口，切换菜单时保留正文', async ({ page }) => {
  await openComposer(page)
  const input = page.getByRole('textbox', { name: '消息输入' })
  const commands = page.getByRole('button', { name: '打开命令和技能', exact: true })
  const suggestions = page.getByRole('listbox', { name: '命令和技能建议' })
  const model = page.getByRole('button', { name: '选择模型', exact: true })
  const models = page.getByRole('listbox', { name: '模型选项' })
  const requests: string[] = []
  page.on('request', request => {
    if (request.method() === 'POST') requests.push(new URL(request.url()).pathname)
  })

  for (const action of ['click', 'Enter', 'Tab', 'option-Enter']) {
    await input.fill('/mo')
    await expect(suggestions.getByText('指令（1）', { exact: true })).toBeVisible()
    if (action === 'click') await suggestions.getByRole('option', { name: /model/ }).click()
    else if (action === 'option-Enter') await suggestions.getByRole('option', { name: /model/ }).press('Enter')
    else await input.press(action)
    await expect(suggestions).toBeHidden()
    await expect(models).toBeVisible()
    await expect(models).toBeFocused()
    await expect(input).toHaveValue('')
    await models.press('Escape')
    await expect(model).toBeFocused()
  }

  await input.fill('保留的草稿')
  await commands.click()
  await suggestions.getByRole('option', { name: /model/ }).click()
  await expect(suggestions).toBeHidden()
  await expect(models).toBeFocused()
  await expect(input).toHaveValue('保留的草稿')
  await models.getByRole('option', { name: names[0], exact: true }).click()
  await expect(model).toContainText(names[0])
  await expect(model).toBeFocused()

  await input.focus()
  await input.evaluate(element => (element as HTMLTextAreaElement).setSelectionRange(0, 0))
  await input.pressSequentially('/model')
  await expect(input).toHaveValue('/model保留的草稿')
  await input.press('Enter')
  await expect(suggestions).toBeHidden()
  await expect(models).toBeFocused()
  await expect(input).toHaveValue('保留的草稿')
  await models.press('Escape')
  expect(requests).toEqual([])
})

test('Plan 指令没有任务正文时按钮和 Enter 均不提交', async ({ page }) => {
  await openComposer(page)
  const input = page.getByRole('textbox', { name: '消息输入' })
  const send = page.getByRole('button', { name: '发送消息', exact: true })
  await page.getByRole('button', { name: '打开命令和技能', exact: true }).click()
  await page.getByRole('option', { name: /plan 进入 Plan 模式/ }).click()
  await expect(input).toHaveValue('/plan ')
  await expect(send).toBeDisabled()
  await input.press('Enter')
  await expect(input).toHaveValue('/plan ')
  await input.fill('/plan \n  ')
  await expect(send).toBeDisabled()
  await input.press('Enter')
  await expect(input).toHaveValue('/plan \n  ')
  await input.fill('/plan 制定方案')
  await expect(send).toBeEnabled()
})


for (const locale of ['zh-CN', 'en']) {
  for (const theme of ['light', 'dark']) {
    test(`权限文字按工具栏实际宽度收起和恢复 ${locale} ${theme}`, async ({ page }, testInfo) => {
      await page.setViewportSize({ width: 1440, height: 900 })
      await page.emulateMedia({ reducedMotion: 'reduce' })
      await openComposer(page, locale, theme)
      const access = page.getByRole('button', { name: locale === 'en' ? 'Choose access permissions' : '选择访问权限' })
      const commands = page.getByRole('button', { name: locale === 'en' ? 'Open commands and skills' : '打开命令和技能' })
      const attachment = page.getByRole('button', { name: locale === 'en' ? 'Add local attachments' : '添加本地附件' })
      const label = access.locator('span')
      const toolbar = page.locator('.composer-toolbar-container')
      const model = page.locator('.composer-toolbar-trailing .ui-compact-picker-trigger')
      await expect(model).toBeEnabled()
      await access.click()
      await page.getByRole('option').first().click()
      await model.click()
      const providerTypography = await page.getByRole('group', { name: '测试提供方' })
        .getByText('测试提供方', { exact: true }).evaluate(textStyle)
      await page.getByRole('option').filter({ hasText: names[2] }).click()
      for (const width of [461, 460, 459, 600]) {
        await toolbar.evaluate((element, width) => { (element as HTMLElement).style.width = `${width}px` }, width)
        await expect(label).toBeVisible({ visible: width > 460 })
        await expect(access).toHaveAccessibleDescription(locale === 'en' ? 'Ask before writing' : '写入需审批')
        await expect(model.locator('span').first()).toBeVisible()
        if (width === 460) {
          await access.hover()
          await expect(page.getByRole('tooltip').filter({ hasText: locale === 'en' ? 'Ask before writing' : '写入需审批' })).toBeVisible()
        }
      }
      await toolbar.evaluate(element => { (element as HTMLElement).style.removeProperty('width') })
      for (const width of [320, 768, 1024, 1440]) {
        await page.setViewportSize({ width, height: 900 })
        await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
        await expect(model.locator('span').first()).toHaveCSS('text-overflow', 'ellipsis')
        if (width === 320) expect(await model.locator('span').first().evaluate(element => element.scrollWidth > element.clientWidth)).toBe(true)
        const bounds = await access.boundingBox()
        const modelBounds = await model.boundingBox()
        expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(modelBounds!.x)
        const controls = await toolbar.getByRole('button').evaluateAll(elements => elements.slice(0, 3).map(element => {
          const bounds = element.getBoundingClientRect()
          return { label: element.getAttribute('aria-label'), left: bounds.left, right: bounds.right, color: getComputedStyle(element).color }
        }))
        expect(controls.map(control => control.label)).toEqual(locale === 'en'
          ? ['Open commands and skills', 'Add local attachments', 'Choose access permissions']
          : ['打开命令和技能', '添加本地附件', '选择访问权限'])
        expect(controls[0]!.right).toBeLessThanOrEqual(controls[1]!.left)
        expect(controls[1]!.right).toBeLessThanOrEqual(controls[2]!.left)
        expect(controls[0]!.color).toBe(controls[2]!.color)
        expect(controls[1]!.color).toBe(controls[2]!.color)
        const iconGeometry = []
        for (const control of [commands, attachment, access]) {
          const icon = control.locator('svg').first()
          await expect(icon).toHaveCSS('width', '16px')
          await expect(icon).toHaveCSS('height', '16px')
          iconGeometry.push(await icon.evaluate(element => {
            const svg = element as SVGSVGElement
            const style = getComputedStyle(svg)
            const scale = Number.parseFloat(style.width) / svg.viewBox.baseVal.width
            const bounds = svg.getBBox()
            return {
              visibleSize: Math.max(bounds.width, bounds.height) * scale,
              strokeWidth: Number.parseFloat(style.strokeWidth) * scale,
            }
          }))
        }
        for (const geometry of iconGeometry.slice(1)) {
          expect(geometry.visibleSize).toBeCloseTo(iconGeometry[0]!.visibleSize, 1)
          expect(geometry.strokeWidth).toBeCloseTo(iconGeometry[0]!.strokeWidth, 2)
        }
        await expect(commands).not.toHaveAttribute('aria-describedby')
        await access.click()
        await expect(page.getByRole('option')).toHaveCount(2)
        await expect(page.getByRole('option').first()).toContainText(locale === 'en' ? 'Ask before writing' : '写入需审批')
        await page.getByRole('listbox').press('Escape')
        await page.screenshot({ path: testInfo.outputPath(`toolbar-${locale}-${theme}-${width}.png`) })
        await commands.click()
        const menu = page.getByRole('listbox', { name: locale === 'en' ? 'Command and skill suggestions' : '命令和技能建议' })
        await expect(menu).toBeVisible()
        for (const label of locale === 'en' ? ['Commands (4)', 'Skills'] : ['指令（4）', '技能']) {
          expect(await menu.getByText(label, { exact: true }).evaluate(textStyle)).toEqual(providerTypography)
        }
        const commandGroup = menu.getByRole('group', { name: locale === 'en' ? 'Commands' : '指令', exact: true })
        const skillHeading = menu.getByText(locale === 'en' ? 'Skills' : '技能', { exact: true })
        const lastCommandBounds = (await commandGroup.getByRole('option').last().boundingBox())!
        const skillHeadingBounds = (await skillHeading.boundingBox())!
        expect(skillHeadingBounds.y - lastCommandBounds.y - lastCommandBounds.height)
          .toBe(Number.parseFloat(providerTypography.menuPaddingTop))
        const menuBounds = (await menu.boundingBox())!
        expect(menuBounds.x).toBeGreaterThanOrEqual(0)
        expect(menuBounds.y).toBeGreaterThanOrEqual(0)
        expect(menuBounds.x + menuBounds.width).toBeLessThanOrEqual(width)
        await page.screenshot({ path: testInfo.outputPath(`commands-${locale}-${theme}-${width}.png`) })
        await page.getByRole('textbox', { name: locale === 'en' ? 'Message input' : '消息输入' }).press('Escape')
        await expect(menu).toBeHidden()
      }
    })
  }
}
