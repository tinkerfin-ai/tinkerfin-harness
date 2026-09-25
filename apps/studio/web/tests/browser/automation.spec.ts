import { expect, test, type Page } from '@playwright/test'
import { createAutomationFixture, runFixture, taskFixture } from '../../src/test/automationFixtures'
import type { AutomationDraft } from '../../src/features/automation/model'

const user = { user_id: 1, username: 'automation-preview', display_name: '自动化预览', avatar_url: null, roles: [], disabled: false }

async function prepare(page: Page, language = 'zh-CN') {
  const requests: string[] = []
  const state = createAutomationFixture()
  const operations = new Map<string, unknown>()
  await page.clock.setFixedTime(new Date('2026-09-10T07:00:00Z'))
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.addInitScript(({ user, language }) => {
    localStorage.setItem('tinkerfin.auth.session', JSON.stringify({ token: 'browser-token', serverAddress: 'http://127.0.0.1:8090', tokenType: 'Bearer', expiresAt: '2099-01-01T00:00:00.000Z', user }))
    localStorage.setItem('tinkerfin:language', language)
  }, { user, language })
  await page.route('**/api/**', async (route) => {
    const url = new URL(route.request().url())
    const path = url.pathname
    const method = route.request().method()
    requests.push(`${method} ${path}`)
    let data: unknown = {}
    const query = url.searchParams.get('query')?.toLowerCase() ?? ''
    const status = url.searchParams.get('status')
    const matchingTasks = state.tasks.filter(task => task.name.toLowerCase().includes(query))
    const matchingRuns = state.runs.filter(run => run.name.toLowerCase().includes(query) && Date.parse(run.queuedAt) >= Date.parse(url.searchParams.get('from') ?? '2000-01-01') && Date.parse(run.queuedAt) < Date.parse(url.searchParams.get('until') ?? '2100-01-01'))
    if (path === '/api/auth/me') data = { expires_at: '2099-01-01T00:00:00.000Z', user }
    else if (path === '/api/models') data = { items: [{ modelId: 'main', displayName: '主模型', imageSupport: 'unsupported', connectionId: 'test-provider', connectionDisplayName: '测试提供方', reasoningEnabled: true, isDefault: true }], defaultModelId: 'main' }
    else if (path === '/api/conversation/config') data = { dayRanges: [7, 30] }
    else if (path === '/api/conversation/history') data = { items: [], nextCursor: null }
    else if (path === '/api/automation/tasks/counts') data = { enabled: matchingTasks.filter(task => task.enabled).length, paused: matchingTasks.filter(task => !task.enabled).length }
    else if (path === '/api/automation/tasks' && method === 'GET') data = { items: matchingTasks.filter(task => !status || task.enabled === (status === 'enabled')), nextCursor: null }
    else if (path === '/api/automation/tasks' && method === 'POST') {
      const command: { requestId: string; configuration: AutomationDraft } = route.request().postDataJSON()
      if (operations.has(command.requestId)) data = operations.get(command.requestId)
      else {
        data = taskFixture({ ...command.configuration, inputFiles: [], id: `created-${state.tasks.length}` })
        state.tasks.push(data as ReturnType<typeof taskFixture>)
        operations.set(command.requestId, data)
      }
    } else if (path === '/api/automation/tasks/batch') {
      const command: { operation: 'pause' | 'delete'; items: { taskId: string; requestId: string; expectedRevision: number }[] } = route.request().postDataJSON()
      data = command.items.map(item => {
        const task = state.tasks.find(task => task.id === item.taskId)
        if (!task) return { taskId: item.taskId, succeeded: false, error: '任务不存在' }
        if (command.operation === 'delete') state.tasks = state.tasks.filter(task => task.id !== item.taskId)
        else { task.enabled = false; task.revision += 1 }
        return { taskId: item.taskId, succeeded: true, error: null }
      })
    } else if (path.startsWith('/api/automation/tasks/') && method === 'POST') {
      const [, id, operation] = path.match(/tasks\/([^/]+)\/(run|pause|enable)$/)!
      const task = state.tasks.find(task => task.id === id)!
      const command: { requestId: string } = route.request().postDataJSON()
      if (operations.has(command.requestId)) data = operations.get(command.requestId)
      else if (operation === 'run') {
        const run = runFixture({ id: `manual-${state.runs.length}`, taskId: id, name: task.name, queuedAt: '2026-09-10T07:00:00Z', startedAt: null, finishedAt: null, status: 'queued', trigger: 'manual' })
        state.runs.push(run); data = run; operations.set(command.requestId, data)
      } else { task.enabled = operation === 'enable'; task.revision += 1; data = task; operations.set(command.requestId, data) }
    } else if (path.startsWith('/api/automation/tasks/') && method === 'PUT') {
      const command: { configuration: AutomationDraft } = route.request().postDataJSON()
      const task = state.tasks.find(task => task.id === path.split('/').at(-1))!
      Object.assign(task, command.configuration, { revision: task.revision + 1 }); data = task
    } else if (path === '/api/automation/runs/counts') {
      const counts: Record<string, number> = {}
      matchingRuns.forEach(run => { counts[run.status] = (counts[run.status] ?? 0) + 1 }); data = counts
    } else if (path === '/api/automation/runs') data = { items: matchingRuns.filter(run => !status || run.status === status), nextCursor: null }
    else if (path.startsWith('/api/automation/runs/')) {
      const run = state.runs.find(run => run.id === path.split('/').at(-1))!
      data = { ...run, messages: [], outputFiles: [], resultAvailable: true }
    } else throw new Error(`Unexpected request: ${method} ${path}`)
    await route.fulfill({ json: { code: 0, message: 'success', data } })
  })
  await page.goto('/')
  await expect(page.getByRole('textbox', { name: language === 'en' ? 'Message input' : '消息输入', exact: true })).toBeVisible()
  if ((page.viewportSize()?.width ?? 1440) < 768) {
    await page.getByRole('button', { name: language === 'en' ? 'Open navigation' : '打开导航', exact: true }).click()
  }
  await page.getByRole('button', { name: language === 'en' ? 'Automation' : '自动化', exact: true }).click()
  await expect(page.getByRole('tab', { name: language === 'en' ? 'History' : '历史', exact: true })).toHaveAttribute('aria-selected', 'true')
  await expect(page).toHaveTitle(language === 'en' ? 'TinkerFin - Automation' : 'TinkerFin - 自动化')
  return requests
}

test('自动化通过任务接口保存和运行，历史只读且删除保留历史', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  const requests = await prepare(page)
  await page.getByRole('button', { name: /查看运行：每日 AI 新闻简报，2026-09-09 09:00/ }).click()
  const result = page.getByRole('dialog', { name: '运行结果' })
  await expect(result.getByText('资讯来源暂时无法访问，本次未生成完整结果')).toBeVisible()
  await expect(result.getByRole('button', { name: /执行|审批/ })).toHaveCount(0)
  await result.getByRole('button', { name: '关闭对话框' }).click()
  await page.getByRole('tab', { name: '任务', exact: true }).click()
  await page.getByRole('button', { name: '执行 每日 AI 新闻简报', exact: true }).click()
  await expect(page.getByText('已加入运行队列', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: '新建自动化', exact: true }).click()
  await page.getByRole('textbox', { name: '任务名称' }).fill('浏览器任务')
  await page.getByRole('textbox', { name: '任务指令' }).fill('整理今天的公开新闻')
  await page.getByText('更多设置', { exact: true }).click()
  await expect(page.getByRole('button', { name: '选择访问权限' })).toContainText('完全访问')
  await page.getByRole('button', { name: '创建任务', exact: true }).click()
  await expect(page.getByRole('button', { name: '浏览器任务', exact: true })).toBeVisible()
  await page.getByRole('button', { name: '批量选择' }).click()
  await page.getByRole('checkbox', { name: '全选当前任务' }).check()
  await page.getByRole('button', { name: '删除', exact: true }).click()
  await expect(page.getByRole('dialog', { name: '删除所选任务' })).toBeVisible()
  await page.getByRole('button', { name: '删除任务', exact: true }).click()
  await expect(page.getByRole('heading', { name: '还没有自动化' })).toBeVisible()
  await expect(page.getByRole('button', { name: '撤销删除' })).toHaveCount(0)
  await page.getByRole('tab', { name: '历史', exact: true }).click()
  await expect(page.getByRole('button', { name: /查看运行：每日 AI 新闻简报/ }).first()).toBeVisible()
  expect(requests).toContain('POST /api/automation/tasks')
  expect(requests.some(value => value.includes('/api/conversation/chat'))).toBe(false)
  expect(errors).toEqual([])
})

test('自动化读取失败在内容区展示统一重试并通知全局 Toast', async ({ page }, testInfo) => {
  await prepare(page)
  const taskFailure = '**/api/automation/tasks?**'
  await page.route(taskFailure, route => route.fulfill({ status: 503, json: { code: 1001007004, message: 'unavailable', data: null } }))
  await page.getByRole('tab', { name: '任务', exact: true }).click()
  const taskAlert = page.getByRole('tabpanel', { name: '任务' }).getByRole('alert')
  await expect(taskAlert).toContainText('自动化数据加载失败')
  await expect(page.getByRole('list', { name: '系统提示' })).toContainText('自动化数据加载失败')
  for (const theme of ['light', 'dark']) {
    await page.evaluate(value => { document.documentElement.dataset.theme = value }, theme)
    for (const width of [320, 768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 900 })
      const position = await taskAlert.evaluate((element) => {
        const card = element.getBoundingClientRect()
        const region = document.querySelector('.automation-scroll')!.getBoundingClientRect()
        return { offsetX: Math.abs(card.left + card.width / 2 - region.left - region.width / 2), offsetY: Math.abs(card.top + card.height / 2 - region.top - region.height / 2), overflow: document.documentElement.scrollWidth > innerWidth }
      })
      expect(position.offsetX).toBeLessThanOrEqual(2)
      expect(position.offsetY).toBeLessThanOrEqual(2)
      expect(position.overflow).toBe(false)
      await page.screenshot({ path: testInfo.outputPath(`automation-failure-${theme}-${width}.png`) })
    }
  }
  await page.locator('#automation-panel').evaluate(element => { element.style.minHeight = '2400px' })
  await page.locator('.automation-scroll').evaluate(element => { element.scrollTop = 1200 })
  await expect(taskAlert).toBeInViewport()
  await expect(taskAlert.getByRole('button', { name: '重新加载' })).toBeVisible()
  await page.locator('#automation-panel').evaluate(element => { element.style.minHeight = '' })
  await page.evaluate(() => { document.documentElement.dataset.theme = 'light' })
  await page.unroute(taskFailure)
  await taskAlert.getByRole('button', { name: '重新加载' }).click()
  await expect(taskAlert).toHaveCount(0)

  const historyFailure = '**/api/automation/runs?**'
  await page.route(historyFailure, route => route.fulfill({ status: 503, json: { code: 1001007004, message: 'unavailable', data: null } }))
  await page.getByRole('tab', { name: '历史', exact: true }).click()
  const historyAlert = page.getByRole('tabpanel', { name: '历史' }).getByRole('alert')
  await expect(historyAlert).toContainText('自动化数据加载失败')
  await expect(page.getByRole('button', { name: '上一周' })).toBeVisible()
  await expect(page.getByText('暂无记录')).toHaveCount(0)
  await page.setViewportSize({ width: 320, height: 900 })
  await page.screenshot({ path: testInfo.outputPath('automation-history-failure-light-320.png') })
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.unroute(historyFailure)
  await historyAlert.getByRole('button', { name: '重新加载' }).click()
  await expect(historyAlert).toHaveCount(0)

  const runFailure = '**/api/automation/runs/*'
  await page.route(runFailure, route => route.fulfill({ status: 503, json: { code: 1001007004, message: 'unavailable', data: null } }))
  await page.getByRole('button', { name: /查看运行：每日 AI 新闻简报/ }).first().click()
  const dialog = page.getByRole('dialog', { name: '运行结果' })
  const resultAlert = dialog.getByRole('alert')
  await expect(resultAlert).toContainText('运行结果加载失败')
  await expect(page.getByRole('list', { name: '系统提示' })).toContainText('运行结果加载失败')
  await page.screenshot({ path: testInfo.outputPath('automation-run-result-failure.png') })
  await page.unroute(runFailure)
  await resultAlert.getByRole('button', { name: '重新加载' }).click()
  await expect(resultAlert).toHaveCount(0)
})

test('自动化沿用会话主题与四尺寸布局，共享表单弹层可使用', async ({ page }, testInfo) => {
  await prepare(page)
  for (const theme of ['light', 'dark']) {
    await page.evaluate((theme) => { document.documentElement.dataset.theme = theme }, theme)
    for (const width of [320, 768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 1000 })
      const header = page.locator('.chat-header')
      await expect(header).toHaveCSS('height', '64px')
      const dimensions = await page.evaluate(() => ({ width: innerWidth, scrollWidth: document.documentElement.scrollWidth }))
      expect(dimensions.scrollWidth).toBeLessThanOrEqual(dimensions.width)
      const failureFits = await page.locator('.automation-event-failure').evaluateAll((badges) => badges.every((badge) => {
        const chip = badge.closest('.automation-run-chip')!
        const badgeBox = badge.getBoundingClientRect()
        const chipBox = chip.getBoundingClientRect()
        return badgeBox.left >= chipBox.left && badgeBox.right <= chipBox.right
      }))
      expect(failureFits).toBe(true)
      await page.screenshot({ path: testInfo.outputPath(`automation-${theme}-${width}.png`) })
    }
  }
  await page.evaluate(() => { document.documentElement.dataset.theme = 'light' })
  await page.getByRole('tab', { name: '列表', exact: true }).click()
  const historyRow = page.getByRole('button', { name: /^14:25 投资组合收盘复盘/ })
  const rowBox = await historyRow.boundingBox()
  const titleBox = await historyRow.getByText('投资组合收盘复盘', { exact: true }).boundingBox()
  const statusBox = await historyRow.getByText('运行完成', { exact: true }).boundingBox()
  expect(rowBox).not.toBeNull()
  expect(titleBox).not.toBeNull()
  expect(statusBox).not.toBeNull()
  expect(titleBox!.x - rowBox!.x).toBeLessThanOrEqual(100)
  expect(rowBox!.x + rowBox!.width - statusBox!.x - statusBox!.width).toBeLessThanOrEqual(64)
  await page.screenshot({ path: testInfo.outputPath('automation-history-list.png') })
  await page.getByRole('tab', { name: '任务', exact: true }).click()
  await page.screenshot({ path: testInfo.outputPath('automation-tasks.png') })
  await page.getByRole('button', { name: '新建自动化', exact: true }).click()
  await page.getByRole('button', { name: /执行频率/ }).click()
  await page.getByRole('button', { name: '重复', exact: true }).click()
  await page.getByRole('option', { name: '每周', exact: true }).click()
  await page.getByRole('button', { name: '执行时间', exact: true }).click()
  await page.keyboard.press('Escape')
  await expect(page.getByRole('dialog', { name: '新建自动化' })).toBeVisible()
  await page.getByRole('button', { name: '执行时间', exact: true }).click()
  await page.getByRole('listbox', { name: '小时' }).getByRole('option', { name: '10', exact: true }).click()
  await page.getByRole('listbox', { name: '分钟' }).getByRole('option', { name: '30', exact: true }).click()
  await expect(page.getByRole('button', { name: '执行时间', exact: true })).toContainText('10:30')
  await page.getByRole('button', { name: '有效期 长期有效', exact: true }).click()
  await page.getByRole('button', { name: '开始日期', exact: true }).click()
  await expect(page.getByRole('application', { name: '选择日期' })).toBeVisible()
  await expect.poll(async () => {
    const trigger = await page.getByRole('button', { name: '开始日期', exact: true }).boundingBox()
    const popup = await page.getByRole('application', { name: '选择日期' }).boundingBox()
    return trigger && popup ? Math.abs(popup.x + popup.width - trigger.x - trigger.width) : Infinity
  }).toBeLessThanOrEqual(2)
  await page.screenshot({ path: testInfo.outputPath('automation-date-picker.png') })
  await page.getByRole('application', { name: '选择日期' }).locator('[data-date-value="2026-09-15"]').click()
  await expect(page.getByRole('button', { name: '开始日期', exact: true })).toContainText('2026/09/15')
  await page.getByRole('button', { name: '开始日期', exact: true }).click()
  await page.keyboard.press('Escape')
  await expect(page.getByRole('dialog', { name: '新建自动化' })).toBeVisible()
  await page.getByRole('button', { name: '完成', exact: true }).click()
  await page.screenshot({ path: testInfo.outputPath('automation-editor.png') })
  await page.setViewportSize({ width: 320, height: 1000 })
  await page.getByRole('button', { name: /执行频率/ }).click()
  await page.getByRole('button', { name: '重复', exact: true }).click()
  await expect(page.getByRole('option', { name: '单次', exact: true })).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('automation-editor-mobile.png') })
})

test('自动化英文界面保留用户任务名称', async ({ page }) => {
  await prepare(page, 'en')
  await expect(page.getByRole('tab', { name: 'Week', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: /View run: 每日 AI 新闻简报/ }).first()).toBeVisible()
  await page.getByRole('button', { name: 'New automation', exact: true }).click()
  await expect(page.getByRole('textbox', { name: 'Task name', exact: true })).toBeVisible()
})

test('触屏长任务名在任务与历史中保持可读且不产生横向溢出', async ({ browser }) => {
  const context = await browser.newContext({ viewport: { width: 320, height: 1000 }, hasTouch: true })
  const page = await context.newPage()
  try {
    await prepare(page)
    await page.getByRole('tab', { name: '任务', exact: true }).click()
    await page.getByRole('button', { name: '新建自动化', exact: true }).click()
    const name = 'A'.repeat(60)
    await page.getByRole('textbox', { name: '任务名称' }).fill(name)
    await page.getByRole('textbox', { name: '任务指令' }).fill('整理今天的公开新闻')
    await page.getByRole('button', { name: '创建任务', exact: true }).click()
    const task = page.getByRole('button', { name, exact: true })
    await expect(task).toBeVisible()
    expect(await task.evaluate((node) => node.getBoundingClientRect().right)).toBeLessThanOrEqual(320)
    expect((await task.boundingBox())?.height).toBeGreaterThanOrEqual(44)
    await page.getByRole('button', { name: `执行 ${name}`, exact: true }).click()
    await page.getByRole('tab', { name: '历史', exact: true }).click()
    await page.getByRole('tab', { name: '列表', exact: true }).click()
    const run = page.getByRole('button', { name: new RegExp(`15:00 ${name}`) })
    await expect(run).toBeVisible()
    expect(await run.evaluate((node) => node.scrollWidth <= node.clientWidth)).toBe(true)
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(320)
  } finally {
    await context.close()
  }
})

test('触控表单输入与七列日期在窄屏和宽屏均保持44px目标及视口边界', async ({ browser }) => {
  const context = await browser.newContext({ viewport: { width: 320, height: 900 }, hasTouch: true })
  const page = await context.newPage()
  try {
    await prepare(page)
    await page.getByRole('button', { name: '新建自动化', exact: true }).click()
    await page.getByRole('button', { name: '有效期 长期有效', exact: true }).click()
    for (const width of [320, 390, 768, 1440]) {
      await page.setViewportSize({ width, height: 900 })
      expect((await page.getByRole('textbox', { name: '任务名称', exact: true }).boundingBox())!.height).toBeGreaterThanOrEqual(44)
      await page.getByRole('button', { name: '开始日期', exact: true }).click()
      const calendar = page.getByRole('application', { name: '选择日期' })
      await expect(calendar).toBeVisible()
      await expect.poll(async () => {
        const box = await calendar.boundingBox()
        return box !== null && box.x >= 0 && box.x + box.width <= width
      }).toBe(true)
      for (const box of await calendar.locator('[data-date-value]').evaluateAll((days) => days.map((day) => ({ width: day.getBoundingClientRect().width, height: day.getBoundingClientRect().height })))) {
        expect(box.width).toBeGreaterThanOrEqual(44)
        expect(box.height).toBeGreaterThanOrEqual(44)
      }
      await calendar.locator('[data-date-value="2026-09-15"]').click()
      await expect(page.getByRole('button', { name: '开始日期', exact: true })).toContainText('2026/09/15')
    }
  } finally {
    await context.close()
  }
})

test('自动化弹窗标题正文在断点前后对齐，浅深主题占位文字达到4.5对比度', async ({ page }) => {
  await prepare(page)
  await page.getByRole('tab', { name: '列表', exact: true }).click()
  for (const theme of ['light', 'dark']) {
    await page.evaluate((value) => { document.documentElement.dataset.theme = value }, theme)
    for (const width of [320, 440, 600, 768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 900 })
      await page.getByRole('button', { name: '新建自动化', exact: true }).click()
      const editor = page.getByRole('dialog', { name: '新建自动化', exact: true })
      const title = await editor.getByRole('heading', { name: '新建自动化', exact: true }).boundingBox()
      const label = await editor.getByText('任务名称', { exact: true }).boundingBox()
      expect(Math.abs(title!.x - label!.x)).toBeLessThanOrEqual(1)
      const contrast = await editor.getByRole('textbox', { name: '任务指令', exact: true }).evaluate((input) => {
        const canvas = document.createElement('canvas')
        canvas.width = canvas.height = 1
        const context = canvas.getContext('2d')!
        const luminance = (color: string) => {
          context.clearRect(0, 0, 1, 1)
          context.fillStyle = color
          context.fillRect(0, 0, 1, 1)
          return Array.from(context.getImageData(0, 0, 1, 1).data).slice(0, 3)
            .map((channel) => channel / 255)
            .map((value) => value <= .04045 ? value / 12.92 : ((value + .055) / 1.055) ** 2.4)
            .reduce((sum, value, index) => sum + value * [.2126, .7152, .0722][index], 0)
        }
        const foreground = luminance(getComputedStyle(input, '::placeholder').color)
        const background = luminance(getComputedStyle(input.parentElement!).backgroundColor)
        return (Math.max(foreground, background) + .05) / (Math.min(foreground, background) + .05)
      })
      expect(contrast).toBeGreaterThanOrEqual(4.5)
      await editor.getByRole('button', { name: '取消', exact: true }).click()
      await page.getByRole('button', { name: /^14:25 投资组合收盘复盘/ }).click()
      const result = page.getByRole('dialog', { name: '运行结果', exact: true })
      const resultTitle = await result.getByRole('heading', { name: '运行结果', exact: true }).boundingBox()
      const resultContent = await result.getByRole('heading', { name: '投资组合收盘复盘', exact: true }).boundingBox()
      expect(Math.abs(resultTitle!.x - resultContent!.x)).toBeLessThanOrEqual(1)
      await result.getByRole('button', { name: '关闭对话框', exact: true }).click()
    }
  }
})

test('手机周历初始对齐今天，尺寸变化保留阅读位置且切换周次重新定位', async ({ browser }) => {
  const context = await browser.newContext({ viewport: { width: 320, height: 900 }, hasTouch: true })
  const page = await context.newPage()
  try {
    await prepare(page)
    const week = page.getByRole('region', { name: '本周运行历史，可横向滚动', exact: true })
    const today = page.getByRole('region', { name: '2026-09-10 的运行记录，可上下滚动', exact: true })
    const expectTodayAligned = () => expect.poll(async () => {
      const [weekBox, dayBox] = await Promise.all([week.boundingBox(), today.boundingBox()])
      return weekBox && dayBox ? Math.abs(weekBox.x - dayBox.x) : Infinity
    }).toBeLessThanOrEqual(1)
    await expectTodayAligned()
    await week.evaluate((node) => { node.scrollLeft = 120 })
    await page.setViewportSize({ width: 360, height: 900 })
    await expect.poll(() => week.evaluate((node) => node.scrollLeft)).toBe(120)
    await page.setViewportSize({ width: 1440, height: 900 })
    await expect.poll(() => week.evaluate((node) => node.scrollLeft)).toBe(0)
    await page.setViewportSize({ width: 320, height: 900 })
    await expectTodayAligned()
    await page.getByRole('button', { name: '上一周', exact: true }).click()
    const [previousBox, tabsBox] = await Promise.all([
      page.getByRole('button', { name: '上一周', exact: true }).boundingBox(),
      page.getByRole('tablist', { name: '运行历史显示方式' }).boundingBox(),
    ])
    expect(Math.abs(previousBox!.y - tabsBox!.y)).toBeLessThanOrEqual(1)
    await expect.poll(() => week.evaluate((node) => node.scrollLeft)).toBe(0)
    await page.getByRole('button', { name: '回到本周', exact: true }).click()
    await expectTodayAligned()
    for (let previous = 0; previous < 36; previous += 1) {
      await page.getByRole('button', { name: '上一周', exact: true }).click()
    }
    const range = page.getByRole('heading', { level: 2, name: '2025年12月29日 – 2026年1月4日' })
    await expect(range).toBeVisible()
    const [rangeBox, nextBox, viewBox] = await Promise.all([
      range.boundingBox(),
      page.getByRole('button', { name: '下一周', exact: true }).boundingBox(),
      page.getByRole('tablist', { name: '运行历史显示方式' }).boundingBox(),
    ])
    expect(rangeBox!.x + rangeBox!.width).toBeLessThanOrEqual(nextBox!.x)
    expect(nextBox!.x + nextBox!.width).toBeLessThanOrEqual(viewBox!.x)
  } finally {
    await context.close()
  }
})

test('时间选项键盘移动和退出保持弹窗焦点，不隐式保存时间', async ({ page }) => {
  await prepare(page)
  for (const width of [320, 1440]) {
    await page.setViewportSize({ width, height: 900 })
    await page.getByRole('button', { name: '新建自动化', exact: true }).click()
    const editor = page.getByRole('dialog', { name: '新建自动化', exact: true })
    await editor.getByRole('button', { name: /执行频率/ }).click()
    const trigger = editor.getByRole('button', { name: '执行时间', exact: true })
    await trigger.click()
    const hours = page.getByRole('listbox', { name: '小时' })
    const minutes = page.getByRole('listbox', { name: '分钟' })
    await expect(hours.getByRole('option', { name: '09', exact: true })).toBeFocused()
    await page.keyboard.press('ArrowDown')
    await expect(hours.getByRole('option', { name: '10', exact: true })).toBeFocused()
    await page.keyboard.press('Tab')
    await expect(minutes.getByRole('option', { name: '00', exact: true })).toBeFocused()
    await page.keyboard.press('Shift+Tab')
    await expect(hours.getByRole('option', { name: '10', exact: true })).toBeFocused()
    await page.keyboard.press('Tab')
    await page.keyboard.press('Tab')
    await expect(trigger).toBeFocused()
    await expect(trigger).toContainText('09:00')
    await expect(page.getByRole('dialog', { name: '选择时间', exact: true })).toHaveCount(0)
    await page.keyboard.press('Tab')
    await expect(editor.getByRole('button', { name: '完成', exact: true })).toBeFocused()
    await trigger.click()
    await expect(hours.getByRole('option', { name: '09', exact: true })).toBeFocused()
    await page.keyboard.press('Shift+Tab')
    await expect(trigger).toBeFocused()
    await page.keyboard.press('Escape')
    await expect(editor).toHaveCount(0)
    await expect(page.getByRole('button', { name: '新建自动化', exact: true })).toBeFocused()
  }
})

test('矮窗口展开设置不裁切摘要，频率与时间控件等宽等高且保持无边框', async ({ page }) => {
  await prepare(page)
  await page.getByRole('tab', { name: '任务', exact: true }).click()
  await page.getByRole('button', { name: '投资组合收盘复盘', exact: true }).click()
  await page.getByRole('button', { name: /执行频率/ }).click()
  for (const width of [320, 768, 1024, 1440]) {
    await page.setViewportSize({ width, height: 600 })
    for (const theme of ['light', 'dark']) {
      await page.evaluate((value) => { document.documentElement.dataset.theme = value }, theme)
      const summary = page.locator('.automation-schedule-summary')
      await summary.scrollIntoViewIfNeeded()
      const contentsFit = await summary.evaluate((element) => {
        const bounds = element.getBoundingClientRect()
        return [...element.querySelectorAll('button, small, strong')].every((child) => {
          const rect = child.getBoundingClientRect()
          return rect.top >= bounds.top && rect.bottom <= bounds.bottom
        })
      })
      expect(contentsFit).toBe(true)
      const repeat = page.getByRole('button', { name: '重复', exact: true })
      const time = page.getByRole('button', { name: '执行时间', exact: true })
      const [repeatBox, timeBox] = await Promise.all([repeat.boundingBox(), time.boundingBox()])
      expect(Math.abs(repeatBox!.width - timeBox!.width)).toBeLessThanOrEqual(1)
      expect(repeatBox!.height).toBe(timeBox!.height)
      expect(timeBox!.height).toBeGreaterThanOrEqual(44)
      for (const control of [repeat, time]) await expect(control).toHaveCSS('border-top-width', '0px')
      const backgrounds = await Promise.all([repeat, time].map((control) => control.evaluate((node) => getComputedStyle(node).backgroundColor)))
      expect(backgrounds[0]).toBe(backgrounds[1])
    }
  }
})

test('任务行图标、说明和留白均可编辑，执行与开关不触发编辑', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await prepare(page)
  await page.getByRole('tab', { name: '任务', exact: true }).click()
  const edit = page.getByRole('button', { name: '每日 AI 新闻简报', exact: true })
  const dialog = page.getByRole('dialog', { name: '编辑自动化', exact: true })
  await edit.locator('svg').click()
  await expect(dialog).toBeVisible()
  await dialog.getByRole('button', { name: '取消', exact: true }).click()
  await edit.getByText('每天 09:00', { exact: true }).click()
  await expect(dialog).toBeVisible()
  await dialog.getByRole('button', { name: '取消', exact: true }).click()
  const box = await edit.boundingBox()
  await edit.click({ position: { x: box!.width - 12, y: box!.height / 2 } })
  await expect(dialog).toBeVisible()
  await dialog.getByRole('button', { name: '取消', exact: true }).click()
  await expect(edit).toBeFocused()
  await page.keyboard.press('Enter')
  await expect(dialog).toBeVisible()
  await dialog.getByRole('button', { name: '取消', exact: true }).click()
  await page.getByRole('button', { name: '执行 每日 AI 新闻简报', exact: true }).click()
  await expect(page.getByText('已加入运行队列', { exact: true })).toBeVisible()
  await expect(dialog).toHaveCount(0)
  await page.getByRole('switch', { name: '暂停 每日 AI 新闻简报', exact: true }).click()
  await expect(page.getByRole('switch', { name: '启用 每日 AI 新闻简报', exact: true })).toHaveAttribute('aria-checked', 'false')
  await expect(dialog).toHaveCount(0)
})

test('紧凑搜索和状态菜单按查询与当前周计数，关闭恢复搜索入口', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await prepare(page)
  await page.getByRole('tab', { name: '任务', exact: true }).click()
  const searchTrigger = page.getByRole('button', { name: '搜索任务或运行历史', exact: true })
  await searchTrigger.click()
  const search = page.getByRole('searchbox', { name: '搜索任务或运行历史', exact: true })
  await expect(search).toBeFocused()
  const filter = page.getByRole('button', { name: '筛选状态', exact: true })
  await filter.click()
  await expect(page.getByRole('option', { name: /全部状态/ })).toContainText('5')
  await expect(page.getByRole('option', { name: /已暂停/ })).toContainText('1')
  await page.getByRole('option', { name: /已暂停/ }).click()
  await expect(page.getByRole('button', { name: '每日 AI 新闻简报', exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '竞品产品动态追踪', exact: true })).toBeVisible()
  await search.fill('竞品')
  await filter.click()
  await expect(page.getByRole('option', { name: /全部状态/ })).toContainText('1')
  await page.keyboard.press('Home')
  await page.keyboard.press('Enter')
  await expect(filter).toBeFocused()
  await search.focus()
  await page.keyboard.press('Escape')
  await expect(searchTrigger).toBeFocused()
  await expect(search).toHaveCount(0)
  await expect(page.getByRole('button', { name: '每日 AI 新闻简报', exact: true })).toBeVisible()
  await page.getByRole('tab', { name: '历史', exact: true }).click()
  await page.getByRole('button', { name: '上一周', exact: true }).click()
  await searchTrigger.click()
  await filter.click()
  await expect(page.getByRole('option', { name: /全部状态/ })).toContainText('1')
  await expect(page.getByRole('option', { name: /运行失败/ })).toContainText('0')
  await page.keyboard.press('Escape')
  for (const width of [320, 768, 1024, 1440]) {
    await page.setViewportSize({ width, height: 900 })
    await expect(search).toBeVisible()
    expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(0)
    await expect(page.locator('.automation-search-field')).toHaveCSS('border-top-width', '0px')
    const box = await search.boundingBox()
    expect(box!.width).toBeGreaterThan(0)
  }
})

test('会话与自动化默认完全访问，权限选择沿用模型样式', async ({ page }, testInfo) => {
  await prepare(page)
  await page.locator('.new-chat').click()
  const access = page.getByRole('button', { name: '选择访问权限' })
  await expect(access).toContainText('完全访问')
  await access.click()
  await expect(page.getByRole('option')).toHaveCount(2)
  await page.getByRole('option', { name: '写入需审批' }).click()
  await expect(access).toContainText('写入需审批')
  await expect(access).toBeFocused()
  for (const theme of ['light', 'dark']) for (const width of [320, 768, 1024, 1440]) {
    await page.evaluate(value => { document.documentElement.dataset.theme = value }, theme)
    await page.setViewportSize({ width, height: 900 })
    await expect(access).toBeVisible()
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width)
    const model = page.getByRole('button', { name: '选择模型', exact: true })
    expect(await access.evaluate(node => getComputedStyle(node).fontSize)).toBe(await model.evaluate(node => getComputedStyle(node).fontSize))
    if (width === 320 || width === 1440) await page.screenshot({ path: testInfo.outputPath(`permissions-${theme}-${width}.png`) })
  }
})


test('触控环境的会话与自动化权限按钮及选项达到44像素', async ({ browser, baseURL }) => {
  const context = await browser.newContext({ baseURL, hasTouch: true, viewport: { width: 390, height: 844 } })
  try {
    const page = await context.newPage()
    await prepare(page)
    for (const location of ['conversation', 'automation']) {
      await page.getByRole('button', { name: '打开导航', exact: true }).click()
      if (location === 'conversation') {
        await page.locator('.new-chat').click()
        await page.getByRole('button', { name: '关闭导航', exact: true }).click()
      } else {
        await page.getByRole('button', { name: '自动化', exact: true }).click()
        await page.getByRole('button', { name: '新建自动化', exact: true }).click()
        await page.getByText('更多设置', { exact: true }).click()
      }
      const access = page.getByRole('button', { name: '选择访问权限' })
      const box = await access.boundingBox()
      expect(box!.width).toBeGreaterThanOrEqual(44)
      expect(box!.height).toBeGreaterThanOrEqual(44)
      await access.tap()
      for (const option of await page.getByRole('option').all()) {
        expect((await option.boundingBox())!.height).toBeGreaterThanOrEqual(44)
      }
      await page.getByRole('option', { name: '完全访问' }).tap()
    }
  } finally { await context.close() }
})

test('自动化刷新保留页面，新会话的标志、输入框和提示位于视觉中线上方', async ({ page }, testInfo) => {
  await prepare(page)
  await expect(page).toHaveURL(/\/\?page=automation$/)
  await page.reload()
  await expect(page).toHaveURL(/\/\?page=automation$/)
  await expect(page.getByRole('tab', { name: '历史', exact: true })).toBeVisible()
  await page.locator('.new-chat').click()
  await expect(page.locator('.composer-dock.is-hero')).toBeVisible()
  await expect(page).not.toHaveURL(/page=automation/)
  for (const theme of ['light', 'dark']) {
    await page.evaluate(theme => { document.documentElement.dataset.theme = theme }, theme)
    for (const width of [320, 768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 900 })
      await expect.poll(() => page.evaluate(() => {
        const box = (selector: string) => document.querySelector(selector)!.getBoundingClientRect()
        const area = box('.workspace-main')
        const logo = box('.composer-hero')
        const note = box('.composer-note')
        const center = (logo.top + note.bottom) / 2
        return Math.abs((center - area.top) / area.height - 0.45)
      })).toBeLessThanOrEqual(0.01)
      await page.screenshot({ path: testInfo.outputPath(`new-conversation-position-${theme}-${width}.png`) })
    }
  }
  await page.setViewportSize({ width: 768, height: 400 })
  await expect(page.locator('.composer-hero')).toBeInViewport({ ratio: 1 })
  await expect(page.locator('.composer')).toBeInViewport({ ratio: 1 })
  await expect(page.locator('.composer-note')).toBeInViewport({ ratio: 1 })
  await page.reload()
  await expect(page.locator('.composer-dock.is-hero')).toBeVisible()
})


test('功能菜单使用统一界面字重，日期范围两端完整显示且适应浅深主题四尺寸', async ({ page }, testInfo) => {
  await prepare(page)
  for (const theme of ['light', 'dark']) {
    await page.evaluate(theme => { document.documentElement.dataset.theme = theme }, theme)
    for (const width of [320, 768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 900 })
      if (width < 768) await page.getByRole('button', { name: '打开导航', exact: true }).click()
      else if (await page.getByRole('button', { name: '打开侧边栏', exact: true }).isVisible()) {
        await page.getByRole('button', { name: '打开侧边栏', exact: true }).click()
      }
      const menu = page.getByRole('navigation', { name: '工作区功能' })
      for (const name of ['记忆管理', '技能库', '自动化', '更多']) {
        await expect(menu.getByRole('button', { name, exact: true })).toHaveCSS('font-weight', '400')
      }
      await expect(menu.getByRole('button', { name: '自动化', exact: true })).toHaveAttribute('aria-current', 'page')
      await page.screenshot({ path: testInfo.outputPath(`menu-${theme}-${width}.png`) })
      if (width < 768) await page.getByRole('button', { name: '关闭导航', exact: true }).click()
      await expect(page.getByRole('heading', { name: '9月7日 – 9月13日', exact: true })).toBeVisible()
      await page.getByRole('button', { name: '上一周', exact: true }).click()
      await expect(page.getByRole('heading', { name: '8月31日 – 9月6日', exact: true })).toBeVisible()
      await page.getByRole('button', { name: '上一周', exact: true }).click()
      await expect(page.getByRole('heading', { name: '8月24日 – 8月30日', exact: true })).toBeVisible()
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)
      expect(overflow).toBe(false)
      await page.screenshot({ path: testInfo.outputPath(`dates-${theme}-${width}.png`) })
      await page.getByRole('button', { name: '回到本周', exact: true }).click()
    }
  }
})
