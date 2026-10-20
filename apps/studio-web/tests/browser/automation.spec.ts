import { installProjectScope } from './fixtures/projects'
import { installNotificationStream } from './fixtures/notifications'
import { expect, test, type Locator, type Page } from '@playwright/test'
import { createAutomationFixture, runCalendarFixture, runFixture, taskFixture } from '../../src/test/automationFixtures'
import type { AutomationDraft, AutomationRunRecord } from '../../src/features/automation/model'
import { measureBounds } from './support/geometry'

const user = { user_id: 1, username: 'automation-preview', avatar_url: null, roles: [], disabled: false }
const taskPrompt = '整理今天的公开新闻，核对每条信息的来源与发布时间，归纳人工智能产品、技术研究和行业政策的重要变化，保留原始链接，并生成结构清晰、便于继续阅读和追问的每日简报'

const menuAppearance = (menu: Locator) => Promise.all([
  menu.evaluate(element => {
    const style = getComputedStyle(element)
    return [style.backgroundColor, style.borderRadius, style.boxShadow, style.padding]
  }),
  menu.getByRole('option').first().evaluate(element => {
    const style = getComputedStyle(element)
    return [style.fontSize, style.lineHeight, style.minHeight, style.padding, style.borderRadius, style.backgroundColor, style.color]
  }),
])

async function dialogMeasurements(dialog: Locator, titleName: string, actionName: string) {
  const frame = (await dialog.boundingBox())!
  const border = await dialog.evaluate(element => {
    const style = getComputedStyle(element)
    return { top: parseFloat(style.borderTopWidth), left: parseFloat(style.borderLeftWidth),
      right: parseFloat(style.borderRightWidth), bottom: parseFloat(style.borderBottomWidth) }
  })
  const title = (await dialog.getByRole('heading', { name: titleName, exact: true }).boundingBox())!
  const close = (await dialog.getByRole('button', { name: '关闭对话框', exact: true }).boundingBox())!
  const action = dialog.getByRole('button', { name: actionName, exact: true })
  const actionBox = (await action.boundingBox())!
  return {
    titleLeft: title.x - frame.x - border.left,
    closeRight: frame.x + frame.width - border.right - close.x - close.width,
    closeTop: close.y - frame.y - border.top,
    closeSize: [close.width, close.height],
    actionRight: frame.x + frame.width - border.right - actionBox.x - actionBox.width,
    actionBottom: frame.y + frame.height - border.bottom - actionBox.y - actionBox.height,
    actionHeight: actionBox.height,
    actionStyle: await action.evaluate(element => {
      const style = getComputedStyle(element)
      return [style.fontSize, style.lineHeight, style.paddingInline, style.borderRadius]
    }),
  }
}

async function prepare(page: Page, language = 'zh-CN') {
  const requests: string[] = []
  const state = createAutomationFixture()
  state.tasks[0] = { ...state.tasks[0], prompt: taskPrompt }
  const operations = new Map<string, unknown>()
  const submissions = new Map<string, string>()
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
    else if (path === '/api/models') data = { items: [{ modelId: 'main', displayName: '主模型',connectionId: 'test-provider', connectionDisplayName: '测试提供方', reasoningEnabled: true, isDefault: true }], defaultModelId: 'main' }
    else if (path === '/api/conversation/config') data = { dayRanges: [7, 30] }
    else if (path === '/api/skills/installations') data = []
    else if (path === '/api/conversation/history') data = { items: [], nextCursor: null }
    else if (path === '/api/automation/tasks' && method === 'GET') data = { items: matchingTasks.filter(task => !status || task.enabled === (status === 'enabled')), nextCursor: null }
    else if (/^\/api\/automation\/tasks\/[^/]+\/submission$/.test(path) && method === 'GET') {
      const runId = submissions.get(url.searchParams.get('requestId') ?? '')
      data = state.runs.find(run => run.id === runId && run.taskId === path.split('/')[4]) ?? null
    }
    else if (path === '/api/automation/tasks' && method === 'POST') {
      const command: { requestId: string; configuration: AutomationDraft } = route.request().postDataJSON()
      expect(command.configuration).not.toHaveProperty('accessMode')
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
        submissions.set(command.requestId, run.id)
      } else { task.enabled = operation === 'enable'; task.revision += 1; data = task; operations.set(command.requestId, data) }
    } else if (path.startsWith('/api/automation/tasks/') && method === 'PUT') {
      const command: { configuration: AutomationDraft } = route.request().postDataJSON()
      const task = state.tasks.find(task => task.id === path.split('/').at(-1))!
      Object.assign(task, command.configuration, { revision: task.revision + 1 }); data = task
    } else if (path === '/api/automation/runs/active') data = { items: state.runs.filter(run => ['queued', 'running', 'cancel_requested'].includes(run.status)), nextCursor: null }
    else if (/^\/api\/automation\/runs\/[^/]+\/status$/.test(path)) data = state.runs.find(run => run.id === path.split('/').at(-2))
    else if (path === '/api/automation/runs') data = { items: matchingRuns.filter(run => !status || run.status === status), nextCursor: null }
    else if (path === '/api/automation/runs/calendar') data = runCalendarFixture(matchingRuns.filter(run => !status || run.status === status), url.searchParams.get('weekStart')!)
    else if (path.startsWith('/api/automation/runs/')) {
      const run = state.runs.find(run => run.id === path.split('/').at(-1))!
      data = { ...run, threadId: `thread-${run.id}`, runId: `run-${run.id}`, messages: [], outputFiles: [], resultAvailable: true }
    } else throw new Error(`Unexpected request: ${method} ${path}`)
    await route.fulfill({ json: { code: 0, message: 'success', data } })
  })
  await installNotificationStream(page, false)
  await installProjectScope(page)
  await page.goto('/')
  await expect(page.getByRole('textbox', { name: language === 'en' ? 'Message input' : '消息输入', exact: true })).toBeVisible()
  if ((page.viewportSize()?.width ?? 1440) < 768) {
    await page.getByRole('button', { name: language === 'en' ? 'Open navigation' : '打开导航', exact: true }).click()
  }
  await page.getByRole('button', { name: language === 'en' ? 'Scheduled tasks' : '定时任务', exact: true }).click()
  await expect(page.getByRole('tab', { name: language === 'en' ? 'Tasks' : '任务', exact: true })).toHaveAttribute('aria-selected', 'true')
  await expect(page).toHaveTitle(language === 'en' ? 'TinkerFin - Scheduled tasks' : 'TinkerFin - 定时任务')
  return requests
}

test('当前历史页重进定时任务菜单回到任务并清除搜索、状态和批量选择', async ({ page }) => {
  await prepare(page)
  await page.getByRole('button', { name: '批量选择', exact: true }).click()
  await page.getByRole('checkbox', { name: '选择 每日 AI 新闻简报', exact: true }).check()
  await page.getByRole('tab', { name: '历史', exact: true }).click()
  await page.getByRole('button', { name: '筛选状态', exact: true }).click()
  await page.getByRole('option', { name: '运行失败', exact: true }).click()
  await page.getByRole('button', { name: '搜索任务或运行历史', exact: true }).click()
  await page.getByRole('searchbox', { name: '搜索任务或运行历史', exact: true }).fill('不存在')
  await expect(page.getByRole('button', { name: /^查看运行：/ })).toHaveCount(0)
  await page.getByRole('navigation', { name: '工作区功能' }).getByRole('button', { name: '定时任务', exact: true }).click()
  await expect(page.getByRole('tab', { name: '任务', exact: true })).toHaveAttribute('aria-selected', 'true')
  await expect(page.getByRole('searchbox', { name: '搜索任务或运行历史', exact: true })).toHaveCount(0)
  await expect(page.getByRole('article', { name: '每日 AI 新闻简报', exact: true })).toBeVisible()
  await expect(page.getByRole('article', { name: '竞品产品动态追踪', exact: true })).toBeVisible()
  await expect(page.getByRole('checkbox', { name: '全选当前任务', exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '批量选择', exact: true })).toHaveAttribute('aria-pressed', 'false')
  await page.getByRole('button', { name: '筛选状态', exact: true }).click()
  await expect(page.getByRole('option', { selected: true })).toHaveText('全部状态')
})

for (const language of ['zh-CN', 'en']) {
  test(`定时任务加载与空态使用当前名称 ${language}`, async ({ page }) => {
    await prepare(page, language)
    let release!: () => void
    const loading = new Promise<void>(resolve => { release = resolve })
    await page.route('**/api/automation/tasks?**', async route => {
      await loading
      await route.fulfill({ json: { code: 0, message: 'success', data: { items: [], nextCursor: null } } })
    })
    try {
      await page.getByRole('button', { name: language === 'en' ? 'Scheduled tasks' : '定时任务', exact: true }).click()
      const panel = page.getByRole('tabpanel', { name: language === 'en' ? 'Tasks' : '任务', exact: true })
      await expect(panel.getByRole('status')).toHaveText(language === 'en' ? 'Loading scheduled tasks' : '正在加载定时任务')
      release()
      await expect(panel.getByRole('heading', { name: language === 'en' ? 'No scheduled tasks yet' : '还没有定时任务', exact: true })).toBeVisible()
      await expect(panel).toHaveAttribute('aria-busy', 'false')
    } finally { release() }
  })
}

test('自动化通过任务接口保存和运行，历史只读且删除保留历史', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  const requests = await prepare(page)
  await page.getByRole('tab', { name: '历史', exact: true }).click()
  await page.getByRole('button', { name: /查看运行：每日 AI 新闻简报，2026-09-09 09:00/ }).click()
  const result = page.getByRole('dialog', { name: '每日 AI 新闻简报' })
  await expect(result.getByText('资讯来源暂时无法访问，本次未生成完整结果')).toBeVisible()
  await expect(result.getByRole('button', { name: /执行|审批/ })).toHaveCount(0)
  await result.getByRole('button', { name: '关闭对话框' }).click()
  await page.getByRole('tab', { name: '任务', exact: true }).click()
  await page.getByRole('article', { name: '每日 AI 新闻简报', exact: true }).hover()
  await page.getByRole('button', { name: '执行 每日 AI 新闻简报', exact: true }).click()
  await expect(page.getByText('已加入运行队列', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: '新建定时任务', exact: true }).click()
  await page.getByRole('textbox', { name: '任务名称' }).fill('浏览器任务')
  await page.getByRole('textbox', { name: '任务指令' }).fill('整理今天的公开新闻')
  await expect(page.getByText('更多设置', { exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '选择访问权限' })).toHaveCount(0)
  await page.getByRole('button', { name: '有效期 长期有效', exact: true }).click()
  await expect(page.getByRole('button', { name: '完成', exact: true })).toHaveCount(0)
  await page.getByRole('button', { name: '开始日期', exact: true }).click()
  await page.getByRole('application', { name: '选择日期' }).locator('[data-date-value="2026-09-15"]').click()
  await page.getByRole('button', { name: '结束日期', exact: true }).click()
  await page.getByRole('application', { name: '选择日期' }).locator('[data-date-value="2026-09-30"]').click()
  await page.getByRole('button', { name: '清除开始日期', exact: true }).click()
  await expect(page.getByRole('button', { name: '开始日期', exact: true })).toBeFocused()
  await expect(page.getByRole('button', { name: '清除开始日期', exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '结束日期', exact: true })).toContainText('2026/09/30')
  await page.getByRole('button', { name: '开始日期', exact: true }).click()
  await page.getByRole('application', { name: '选择日期' }).locator('[data-date-value="2026-09-15"]').click()
  await page.getByRole('button', { name: '清除结束日期', exact: true }).click()
  await expect(page.getByRole('button', { name: '结束日期', exact: true })).toBeFocused()
  await expect(page.getByRole('button', { name: '清除结束日期', exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '开始日期', exact: true })).toContainText('2026/09/15')
  await page.getByRole('button', { name: /执行频率/ }).click()
  await page.getByRole('button', { name: '重复', exact: true }).click()
  await page.getByRole('option', { name: '每周', exact: true }).click()
  await page.getByRole('button', { name: '执行时间', exact: true }).click()
  await page.getByRole('listbox', { name: '小时' }).getByRole('option', { name: '10', exact: true }).click()
  await page.getByRole('listbox', { name: '分钟' }).getByRole('option', { name: '30', exact: true }).click()
  const saved = page.waitForRequest(request => new URL(request.url()).pathname === '/api/automation/tasks' && request.method() === 'POST')
  await page.getByRole('button', { name: '创建任务', exact: true }).click()
  expect((await saved).postDataJSON().configuration).toMatchObject({ startsOn: '2026-09-15', endsOn: null, schedule: { kind: 'weekly', time: '10:30' } })
  await expect(page.getByRole('button', { name: '浏览器任务', exact: true })).toBeVisible()
  await page.getByRole('button', { name: '批量选择' }).click()
  await page.getByRole('checkbox', { name: '全选当前任务' }).check()
  await page.getByRole('button', { name: '删除', exact: true }).click()
  await expect(page.getByRole('dialog', { name: '删除所选任务' })).toBeVisible()
  await page.getByRole('button', { name: '删除任务', exact: true }).click()
  await expect(page.getByRole('heading', { name: '还没有定时任务' })).toBeVisible()
  await expect(page.getByRole('button', { name: '撤销删除' })).toHaveCount(0)
  await page.getByRole('tab', { name: '历史', exact: true }).click()
  await expect(page.getByRole('button', { name: /查看运行：每日 AI 新闻简报/ }).first()).toBeVisible()
  expect(requests).toContain('POST /api/automation/tasks')
  expect(requests.some(value => value.includes('/api/conversation/chat'))).toBe(false)
  expect(errors).toEqual([])
})

test('定时任务读取失败可重试，Toast 避开顶部并在离开页面后恢复位置', async ({ page }, testInfo) => {
  await page.clock.install({ time: new Date('2026-09-10T07:00:00Z') })
  await prepare(page)
  await page.getByRole('tab', { name: '历史', exact: true }).click()
  await page.clock.pauseAt(new Date('2026-09-10T07:00:01Z'))
  const taskFailure = '**/api/automation/tasks?**'
  await page.route(taskFailure, route => route.fulfill({ status: 503, json: { code: 1001007004, message: 'unavailable', data: null } }))
  await page.getByRole('tab', { name: '任务', exact: true }).click()
  const taskAlert = page.getByRole('tabpanel', { name: '任务' }).getByRole('alert')
  const toasts = page.getByRole('list', { name: '系统提示' })
  await expect(taskAlert).toContainText('定时任务数据加载失败')
  await expect(toasts).toContainText('定时任务数据加载失败')
  let ordinaryToastGap = 0
  const toastPositions: unknown[] = []
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
      const [header, toast] = await measureBounds(page.locator('.chat-header'), toasts)
      expect(toast.y).toBeGreaterThanOrEqual(header.y + header.height)
      if (width >= 768) ordinaryToastGap = toast.y - header.y - header.height
      toastPositions.push({ theme, width, header, toast })
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

  const historyFailure = '**/api/automation/runs/calendar?**'
  await page.route(historyFailure, route => route.fulfill({ status: 503, json: { code: 1001007004, message: 'unavailable', data: null } }))
  await page.getByRole('tab', { name: '历史', exact: true }).click()
  const historyAlert = page.getByRole('tabpanel', { name: '历史' }).getByRole('alert')
  await expect(historyAlert).toContainText('定时任务数据加载失败')
  await expect(page.getByRole('button', { name: '上一周' })).toBeVisible()
  await expect(page.getByText('暂无记录')).toHaveCount(0)
  await page.setViewportSize({ width: 320, height: 900 })
  await page.screenshot({ path: testInfo.outputPath('automation-history-failure-light-320.png') })
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.unroute(historyFailure)
  await historyAlert.getByRole('button', { name: '重新加载' }).click()
  await expect(historyAlert).toHaveCount(0)

  const runFailure = '**/api/automation/runs/run-*'
  await page.route(runFailure, route => route.fulfill({ status: 503, json: { code: 1001007004, message: 'unavailable', data: null } }))
  await page.getByRole('button', { name: /查看运行：每日 AI 新闻简报/ }).first().click()
  const dialog = page.getByRole('dialog', { name: '每日 AI 新闻简报' })
  const resultAlert = dialog.getByRole('alert')
  await expect(resultAlert).toContainText('运行结果加载失败')
  await expect(page.getByRole('list', { name: '系统提示' })).toContainText('运行结果加载失败')
  await page.screenshot({ path: testInfo.outputPath('automation-run-result-failure.png') })
  await page.unroute(runFailure)
  await resultAlert.getByRole('button', { name: '重新加载' }).click()
  await expect(resultAlert).toHaveCount(0)
  await dialog.getByRole('button', { name: '关闭对话框', exact: true }).click()
  await page.setViewportSize({ width: 320, height: 900 })
  const [taskHeader, taskToast] = await measureBounds(page.locator('.chat-header'), toasts)
  expect(taskToast.y).toBeGreaterThanOrEqual(taskHeader.y + taskHeader.height)
  await page.getByRole('button', { name: '打开导航', exact: true }).click()
  await page.locator('[aria-keyshortcuts~="Meta+K"]').click()
  await page.getByRole('button', { name: '关闭导航', exact: true }).click()
  await expect(page.getByRole('textbox', { name: '消息输入', exact: true })).toBeVisible()
  await expect(toasts).toContainText('运行结果加载失败')
  const [conversationHeader, conversationToast] = await measureBounds(page.locator('.chat-header'), toasts)
  expect(conversationHeader.height).toBe(64)
  expect(conversationToast.y).toBeLessThan(taskToast.y)
  expect(conversationToast.y - conversationHeader.y - conversationHeader.height).toBe(ordinaryToastGap)
  toastPositions.push({ page: 'conversation', width: 320, header: conversationHeader, toast: conversationToast })
  await page.screenshot({ path: testInfo.outputPath('conversation-toast-light-320.png') })
  await testInfo.attach('toast-header-measurements', { body: JSON.stringify(toastPositions, null, 2), contentType: 'application/json' })
})

test('任务卡片悬浮和键盘聚焦呈现层次，减少动态时保留反馈', async ({ page }, testInfo) => {
  await prepare(page)
  const card = page.getByRole('article', { name: '每日 AI 新闻简报', exact: true })
  const settledBounds = () => card.evaluate(element => {
    element.getAnimations({ subtree: true }).forEach(animation => animation.finish())
    const { x, y, width, height } = element.getBoundingClientRect()
    return { x, y, width, height }
  })
  for (const theme of ['light', 'dark']) {
    await page.evaluate(theme => { document.documentElement.dataset.theme = theme }, theme)
    await page.emulateMedia({ reducedMotion: 'no-preference' })
    await page.mouse.move(0, 0)
    await page.getByRole('tab', { name: '任务', exact: true }).focus()
    const rest = await settledBounds()
    await card.hover()
    expect((await settledBounds()).y).toBeLessThan(rest.y)
    await page.screenshot({ path: testInfo.outputPath(`automation-card-hover-${theme}.png`) })
    await page.emulateMedia({ reducedMotion: 'reduce' })
    expect((await settledBounds()).y).toBeCloseTo(rest.y, 1)
    await page.screenshot({ path: testInfo.outputPath(`automation-card-reduced-${theme}.png`) })
    await page.mouse.move(0, 0)
    await page.emulateMedia({ reducedMotion: 'no-preference' })
    const edit = card.getByRole('button', { name: '每日 AI 新闻简报', exact: true })
    await edit.focus()
    await page.keyboard.press('Tab')
    await page.keyboard.press('Shift+Tab')
    await expect(edit).toBeFocused()
    expect((await settledBounds()).y).toBeLessThan(rest.y)
    await page.screenshot({ path: testInfo.outputPath(`automation-card-keyboard-${theme}.png`) })
    await page.getByRole('tab', { name: '任务', exact: true }).focus()
  }
})

test('自动化沿用会话主题与四尺寸布局，共享表单弹层可使用', async ({ page }, testInfo) => {
  await prepare(page)
  for (const theme of ['light', 'dark']) {
    await page.evaluate((theme) => { document.documentElement.dataset.theme = theme }, theme)
    for (const width of [320, 768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 1000 })
      await page.getByRole('tab', { name: '任务', exact: true }).click()
      const header = page.locator('.chat-header')
      await expect(header).toHaveCSS('height', width <= 440 ? '104px' : '64px')
      const card = page.getByRole('article', { name: '每日 AI 新闻简报', exact: true })
      await expect(card.getByRole('heading', { name: '每日 AI 新闻简报', exact: true })).toBeVisible()
      await expect(card.getByText('每天 09:00', { exact: true })).toBeVisible()
      await expect(card.getByText('9月11日 09:00运行', { exact: true })).toBeVisible()
      const cards = await page.getByRole('article').evaluateAll(elements => elements.map(element => {
        const { x, y, width, height } = element.getBoundingClientRect()
        return { x, y, width, height }
      }))
      for (const [index, box] of cards.entries()) {
        expect(box.x).toBeGreaterThanOrEqual(0)
        expect(box.x + box.width).toBeLessThanOrEqual(width)
        expect(box.width).toBeCloseTo(cards[0].width, 1)
        for (const other of cards.slice(index + 1)) {
          const separated = box.x + box.width <= other.x || other.x + other.width <= box.x
            || box.y + box.height <= other.y || other.y + other.height <= box.y
          expect(separated).toBe(true)
        }
      }
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
      const prompt = await card.getByText(taskPrompt, { exact: true }).evaluate(element => ({
        height: element.getBoundingClientRect().height, lineHeight: parseFloat(getComputedStyle(element).lineHeight),
        fullHeight: element.scrollHeight, visibleHeight: element.clientHeight,
      }))
      expect(prompt.height).toBeCloseTo(prompt.lineHeight * 2, 1)
      expect(prompt.fullHeight).toBeGreaterThan(prompt.visibleHeight)
      const searchBox = (await page.getByRole('button', { name: '搜索任务或运行历史', exact: true }).boundingBox())!
      const selectionBox = (await page.getByRole('button', { name: '批量选择', exact: true }).boundingBox())!
      const filterBox = (await page.getByRole('button', { name: '筛选状态', exact: true }).boundingBox())!
      expect(selectionBox.x).toBeGreaterThanOrEqual(searchBox.x + searchBox.width)
      expect(selectionBox.x + selectionBox.width).toBeLessThanOrEqual(filterBox.x)
      expect(selectionBox.y + selectionBox.height / 2).toBeCloseTo(searchBox.y + searchBox.height / 2, 1)
      await page.screenshot({ path: testInfo.outputPath(`automation-tasks-${theme}-${width}.png`) })
      await page.getByRole('tab', { name: '历史', exact: true }).click()
      await expect(page.getByRole('button', { name: '批量选择', exact: true })).toHaveCount(0)
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
  await page.getByRole('button', { name: '新建定时任务', exact: true }).click()
  await page.getByRole('button', { name: /执行频率/ }).click()
  await page.getByRole('button', { name: '重复', exact: true }).click()
  await page.getByRole('option', { name: '每周', exact: true }).click()
  await page.getByRole('button', { name: '执行时间', exact: true }).click()
  await page.keyboard.press('Escape')
  await expect(page.getByRole('dialog', { name: '新建定时任务' })).toBeVisible()
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
  await expect(page.getByRole('dialog', { name: '新建定时任务' })).toBeVisible()
  await page.getByRole('button', { name: /有效期/ }).click()
  await page.screenshot({ path: testInfo.outputPath('automation-editor.png') })
  await page.setViewportSize({ width: 320, height: 1000 })
  await page.getByRole('button', { name: /执行频率/ }).click()
  await page.getByRole('button', { name: '重复', exact: true }).click()
  await expect(page.getByRole('option', { name: '单次', exact: true })).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('automation-editor-mobile.png') })
})

test('自动化英文界面保留用户任务名称', async ({ page }) => {
  await prepare(page, 'en')
  await expect(page.getByRole('tablist', { name: 'Scheduled task views', exact: true })).toBeVisible()
  await expect(page.getByRole('article', { name: '每日 AI 新闻简报', exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'New scheduled task', exact: true }).click()
  const editor = page.getByRole('dialog', { name: 'New scheduled task', exact: true })
  await expect(editor.getByRole('textbox', { name: 'Task name', exact: true })).toBeVisible()
  await editor.getByRole('button', { name: 'Cancel', exact: true }).click()
  await page.getByRole('button', { name: '每日 AI 新闻简报', exact: true }).click()
  await expect(page.getByRole('dialog', { name: 'Edit scheduled task', exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Cancel', exact: true }).click()
  await page.getByRole('tab', { name: 'History', exact: true }).click()
  await expect(page.getByRole('tab', { name: 'Week', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: /View run: 每日 AI 新闻简报/ }).first()).toBeVisible()
})

test('触屏长任务名在任务与历史中保持可读且不产生横向溢出', async ({ browser }) => {
  const context = await browser.newContext({ viewport: { width: 320, height: 1000 }, hasTouch: true })
  const page = await context.newPage()
  try {
    await prepare(page)
    await page.getByRole('tab', { name: '任务', exact: true }).click()
    await page.getByRole('button', { name: '新建定时任务', exact: true }).click()
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
    await page.getByRole('button', { name: '新建定时任务', exact: true }).click()
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
  await page.getByRole('tab', { name: '历史', exact: true }).click()
  await page.getByRole('tab', { name: '列表', exact: true }).click()
  for (const theme of ['light', 'dark']) {
    await page.evaluate((value) => { document.documentElement.dataset.theme = value }, theme)
    for (const width of [320, 440, 600, 768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 900 })
      await page.getByRole('button', { name: '新建定时任务', exact: true }).click()
      const editor = page.getByRole('dialog', { name: '新建定时任务', exact: true })
      const title = await editor.getByRole('heading', { name: '新建定时任务', exact: true }).boundingBox()
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
      const result = page.getByRole('dialog', { name: '投资组合收盘复盘', exact: true })
      const resultTitle = await result.getByRole('heading', { name: '投资组合收盘复盘', exact: true }).boundingBox()
      const resultContent = await result.getByRole('heading', { name: '结果', exact: true }).boundingBox()
      expect(Math.abs(resultTitle!.x - resultContent!.x)).toBeLessThanOrEqual(1)
      await result.getByRole('button', { name: '关闭对话框', exact: true }).click()
    }
  }
})

test('历史日期范围按内容占宽，浅深主题四尺寸均不拆开日期', async ({ page }, testInfo) => {
  await prepare(page)
  await page.clock.setFixedTime(new Date('2026-10-16T07:00:00Z'))
  await page.getByRole('tab', { name: '历史', exact: true }).click()
  const title = page.getByRole('heading', { level: 2, name: '10月12日 – 10月18日', exact: true })
  for (const theme of ['light', 'dark']) {
    await page.evaluate(value => { document.documentElement.dataset.theme = value }, theme)
    for (const width of [320, 768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 900 })
      await expect(title).toBeVisible()
      const lineCount = await title.evaluate(element => {
        const range = document.createRange()
        range.selectNodeContents(element)
        return range.getClientRects().length
      })
      expect(lineCount).toBe(1)
      const [previous, range, next, tabs] = await measureBounds(
        page.getByRole('button', { name: '上一周', exact: true }), title,
        page.getByRole('button', { name: '下一周', exact: true }),
        page.getByRole('tablist', { name: '运行历史显示方式' }),
      )
      expect(previous.x + previous.width).toBeLessThanOrEqual(range.x)
      expect(range.x + range.width).toBeLessThanOrEqual(next.x)
      expect(next.x + next.width <= tabs.x || next.y + next.height <= tabs.y).toBe(true)
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
      await page.screenshot({ path: testInfo.outputPath(`date-range-${theme}-${width}.png`) })
    }
  }
})

test('手机周历初始对齐今天，尺寸变化保留阅读位置且切换周次重新定位', async ({ browser }) => {
  const context = await browser.newContext({ viewport: { width: 320, height: 900 }, hasTouch: true })
  const page = await context.newPage()
  try {
    await prepare(page)
    await page.getByRole('tab', { name: '历史', exact: true }).click()
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
    expect(previousBox!.x + previousBox!.width <= tabsBox!.x || previousBox!.y + previousBox!.height <= tabsBox!.y).toBe(true)
    await expect.poll(() => week.evaluate((node) => node.scrollLeft)).toBe(0)
    await page.getByRole('button', { name: '回到本周', exact: true }).click()
    await expectTodayAligned()
    for (let previous = 0; previous < 36; previous += 1) {
      await page.getByRole('button', { name: '上一周', exact: true }).click()
    }
    const range = page.getByRole('heading', { level: 2, name: '2025年12月29日 – 2026年1月4日' })
    await expect(range).toBeVisible()
    const dateLines = await range.evaluate(element => {
      const content = element.textContent!
      return content.split(' – ').map(date => {
        const start = content.indexOf(date)
        const end = start + date.length
        const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT)
        const lineTops = new Set<number>()
        let offset = 0
        for (let node = walker.nextNode(); node; node = walker.nextNode()) {
          const length = node.textContent!.length
          if (offset < end && offset + length > start) {
            const selection = document.createRange()
            selection.setStart(node, Math.max(0, start - offset))
            selection.setEnd(node, Math.min(length, end - offset))
            for (const rect of selection.getClientRects()) lineTops.add(Math.round(rect.top))
          }
          offset += length
        }
        return lineTops.size
      })
    })
    expect(dateLines).toEqual([1, 1])
    const [rangeBox, nextBox, viewBox] = await Promise.all([
      range.boundingBox(),
      page.getByRole('button', { name: '下一周', exact: true }).boundingBox(),
      page.getByRole('tablist', { name: '运行历史显示方式' }).boundingBox(),
    ])
    expect(rangeBox!.x + rangeBox!.width).toBeLessThanOrEqual(nextBox!.x)
    expect(nextBox!.x + nextBox!.width <= viewBox!.x || nextBox!.y + nextBox!.height <= viewBox!.y).toBe(true)
  } finally {
    await context.close()
  }
})

test('时间选项键盘移动和退出保持弹窗焦点，不隐式保存时间', async ({ page }) => {
  await prepare(page)
  for (const width of [320, 1440]) {
    await page.setViewportSize({ width, height: 900 })
    await page.getByRole('button', { name: '新建定时任务', exact: true }).click()
    const editor = page.getByRole('dialog', { name: '新建定时任务', exact: true })
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
    await expect(editor.getByRole('button', { name: '取消', exact: true })).toBeFocused()
    await trigger.click()
    await expect(hours.getByRole('option', { name: '09', exact: true })).toBeFocused()
    await page.keyboard.press('Shift+Tab')
    await expect(trigger).toBeFocused()
    await page.keyboard.press('Escape')
    await expect(editor).toHaveCount(0)
    await expect(page.getByRole('button', { name: '新建定时任务', exact: true })).toBeFocused()
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

test('任务卡标题、指令和底部可编辑，执行、开关与选择不触发编辑', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await prepare(page)
  const card = page.getByRole('article', { name: '每日 AI 新闻简报', exact: true })
  const edit = card.getByRole('button', { name: '每日 AI 新闻简报', exact: true })
  const dialog = page.getByRole('dialog', { name: '编辑定时任务', exact: true })
  await edit.click()
  await expect(dialog).toBeVisible()
  await dialog.getByRole('button', { name: '取消', exact: true }).click()
  for (const content of [card.getByText(taskPrompt, { exact: true }), card.getByText('每天 09:00', { exact: true }), card.getByText('9月11日 09:00运行', { exact: true })]) {
    const box = (await content.boundingBox())!
    await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2)
    await expect(dialog).toBeVisible()
    await dialog.getByRole('button', { name: '取消', exact: true }).click()
    await expect(edit).toBeFocused()
  }
  await page.keyboard.press('Enter')
  await expect(dialog).toBeVisible()
  await dialog.getByRole('button', { name: '取消', exact: true }).click()
  await page.getByRole('button', { name: '执行 每日 AI 新闻简报', exact: true }).click()
  await expect(page.getByText('已加入运行队列', { exact: true })).toBeVisible()
  await expect(dialog).toHaveCount(0)
  await page.getByRole('switch', { name: '暂停 每日 AI 新闻简报', exact: true }).click()
  await expect(page.getByRole('switch', { name: '启用 每日 AI 新闻简报', exact: true })).toHaveAttribute('aria-checked', 'false')
  await expect(dialog).toHaveCount(0)
  await page.getByRole('button', { name: '批量选择', exact: true }).click()
  await card.getByRole('checkbox', { name: '选择 每日 AI 新闻简报', exact: true }).check()
  await expect(dialog).toHaveCount(0)
  await expect(page.getByText('已选 1 项', { exact: true })).toBeVisible()
})

test('提交结果待确认时执行入口持续显示并支持重试确认', async ({ page }) => {
  await prepare(page)
  const failed = '**/api/automation/tasks/news/run?*'
  await page.route(failed, route => route.fulfill({ status: 503, json: { code: 1001007004, message: 'unavailable', data: null } }))
  const card = page.getByRole('article', { name: '每日 AI 新闻简报', exact: true })
  await card.hover()
  await card.getByRole('button', { name: '执行 每日 AI 新闻简报', exact: true }).click()
  await expect(card.getByRole('status')).toHaveText('提交结果待确认')
  await page.getByRole('tab', { name: '任务', exact: true }).focus()
  await page.mouse.move(0, 0)
  const retry = card.getByRole('button', { name: '重试确认 每日 AI 新闻简报', exact: true })
  await expect(retry).toHaveCSS('opacity', '1')
  await expect(retry).toBeEnabled()
  await page.unroute(failed)
  await retry.click()
  await expect(card.getByRole('status')).toHaveText('排队中')
  await expect(card.getByRole('button', { name: '执行 每日 AI 新闻简报', exact: true })).toHaveAttribute('aria-busy', 'true')
})

for (const theme of ['light', 'dark']) for (const width of [320, 768, 1024, 1440]) {
  test(`展开搜索保持适中字号和紧凑间距，关闭后恢复入口 ${theme} ${width}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await prepare(page)
    await page.getByRole('tab', { name: '历史', exact: true }).click()
    const archiveIcon = (await page.getByRole('button', { name: '已归档会话', exact: true }).locator('svg').boundingBox())!
    const sidebarSearchIcon = (await page.getByRole('button', { name: '搜索会话', exact: true }).locator('svg').boundingBox())!
    const referenceGap = sidebarSearchIcon.x - archiveIcon.x - archiveIcon.width
    await page.setViewportSize({ width, height: 900 })
    await page.evaluate(value => { document.documentElement.dataset.theme = value }, theme)
    const trigger = page.getByRole('button', { name: '搜索任务或运行历史', exact: true })
    const before = (await trigger.locator('svg').boundingBox())!
    const filterIcon = page.getByRole('button', { name: '筛选状态', exact: true }).locator('svg')
    const closedFilter = (await filterIcon.boundingBox())!
    expect(closedFilter.x - before.x - before.width).toBeCloseTo(referenceGap, 4)
    expect(closedFilter.width).toBe(sidebarSearchIcon.width)
    const create = page.getByRole('button', { name: '新建定时任务', exact: true })
    const createBox = (await create.boundingBox())!
    expect(createBox.x - closedFilter.x - closedFilter.width).toBeCloseTo(referenceGap, 4)
    await trigger.click()
    const input = page.getByRole('searchbox', { name: '搜索任务或运行历史', exact: true })
    await expect(input).toBeFocused()
    const icon = page.locator('label').filter({ has: input }).locator(':scope > svg')
    await expect(icon).toHaveCount(0)
    const inputBox = (await input.boundingBox())!
    const closeBox = (await page.getByRole('button', { name: '关闭搜索', exact: true }).boundingBox())!
    const closeIcon = (await page.getByRole('button', { name: '关闭搜索', exact: true }).locator('svg').boundingBox())!
    const openFilter = (await filterIcon.boundingBox())!
    await expect(input).toHaveCSS('font-size', '13px')
    expect(inputBox.width).toBeGreaterThan(0)
    expect(inputBox.x + inputBox.width).toBeLessThanOrEqual(closeBox.x)
    expect(closeBox.x - inputBox.x - inputBox.width).toBeLessThanOrEqual(4)
    expect(openFilter.x - closeIcon.x - closeIcon.width).toBeCloseTo(referenceGap, 4)
    expect(createBox.x - openFilter.x - openFilter.width).toBeCloseTo(referenceGap, 4)
    expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(0)

    await page.screenshot({ path: testInfo.outputPath(`automation-search-${theme}-${width}.png`) })
    await input.press('Escape')
    await expect(trigger).toBeFocused()
    const collapsed = (await trigger.locator('svg').boundingBox())!
    expect({ width: collapsed.width, height: collapsed.height }).toEqual({ width: before.width, height: before.height })
    const filter = page.getByRole('button', { name: '筛选状态', exact: true })
    await expect(filter).toBeVisible()
    await filter.click()
    const menu = page.getByRole('listbox', { name: '筛选状态', exact: true })
    await page.mouse.move(0, 0)
    const filterAppearance = await menuAppearance(menu)
    const menuBox = (await menu.boundingBox())!
    expect(menuBox.x).toBeGreaterThanOrEqual(0)
    expect(menuBox.x + menuBox.width).toBeLessThanOrEqual(width)
    await menu.press('End')
    const last = menu.getByRole('option', { name: '结果待确认', exact: true })
    await expect(menu).toHaveAttribute('aria-activedescendant', (await last.getAttribute('id'))!)
    await expect(last).toBeInViewport()
    await menu.press('Home')
    await page.screenshot({ path: testInfo.outputPath(`filter-${theme}-${width}.png`) })
    await menu.press('Escape')
    await expect(filter).toBeFocused()
    await create.click()
    await page.getByRole('dialog', { name: '新建定时任务', exact: true }).getByRole('button', { name: '选择模型', exact: true }).click()
    await page.mouse.move(0, 0)
    expect(filterAppearance).toEqual(await menuAppearance(page.getByRole('listbox', { name: '模型选项', exact: true })))
  })
}

test('状态筛选独立于搜索，关闭搜索保留筛选且切换视图重置状态', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  const requests = await prepare(page)
  await page.getByRole('tab', { name: '任务', exact: true }).click()
  const searchTrigger = page.getByRole('button', { name: '搜索任务或运行历史', exact: true })
  const search = page.getByRole('searchbox', { name: '搜索任务或运行历史', exact: true })
  const filter = page.getByRole('button', { name: '筛选状态', exact: true })
  await expect(search).toHaveCount(0)
  const searchBox = (await searchTrigger.boundingBox())!
  const filterBox = (await filter.boundingBox())!
  expect(filterBox.x).toBeGreaterThanOrEqual(searchBox.x + searchBox.width)
  await filter.click()
  const menu = page.getByRole('listbox', { name: '筛选状态', exact: true })
  await expect(menu.getByRole('option')).toHaveText(['全部状态', '已启用', '已暂停'])
  await menu.getByRole('option', { name: '已暂停', exact: true }).click()
  await expect(filter).toBeFocused()
  await expect(page.getByRole('button', { name: '每日 AI 新闻简报', exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '竞品产品动态追踪', exact: true })).toBeVisible()
  await searchTrigger.click()
  await expect(search).toBeFocused()
  await search.fill('不存在')
  await expect(page.getByRole('button', { name: '竞品产品动态追踪', exact: true })).toHaveCount(0)
  await search.press('Escape')
  await expect(searchTrigger).toBeFocused()
  await expect(search).toHaveCount(0)
  await expect(filter).toBeVisible()
  await expect(page.getByRole('button', { name: '竞品产品动态追踪', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: '每日 AI 新闻简报', exact: true })).toHaveCount(0)
  await filter.click()
  await expect(menu.getByRole('option', { selected: true })).toHaveText('已暂停')
  await menu.press('Home')
  await menu.press('Enter')
  await expect(filter).toBeFocused()
  await expect(page.getByRole('button', { name: '每日 AI 新闻简报', exact: true })).toBeVisible()
  await page.getByRole('tab', { name: '历史', exact: true }).click()
  await filter.click()
  await expect(menu.getByRole('option', { selected: true })).toHaveText('全部状态')
  await expect(menu.getByRole('option')).toHaveText(['全部状态', '排队中', '运行中', '等待人工处理', '正在取消', '运行完成', '运行失败', '运行超时', '已取消', '结果待确认'])
  await menu.getByRole('option', { name: '运行失败', exact: true }).click()
  await searchTrigger.click()
  await page.getByRole('button', { name: '关闭搜索', exact: true }).click()
  await filter.click()
  await expect(menu.getByRole('option', { selected: true })).toHaveText('运行失败')
  await menu.press('Escape')
  await page.getByRole('tab', { name: '任务', exact: true }).click()
  await filter.click()
  await expect(menu.getByRole('option', { selected: true })).toHaveText('全部状态')
  expect(requests.filter(request => /\/automation\/(tasks|runs)\/counts/.test(request))).toEqual([])
})

test('普通会话默认完全访问，权限选择沿用模型样式', async ({ page }, testInfo) => {
  await prepare(page)
  await page.locator('[aria-keyshortcuts~="Meta+K"]').click()
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


test('触控环境保留普通会话权限选择，自动化创建与详情操作达到44像素', async ({ browser, baseURL }) => {
  const context = await browser.newContext({ baseURL, hasTouch: true, viewport: { width: 390, height: 844 } })
  try {
    const page = await context.newPage()
    await prepare(page)
    await page.getByRole('button', { name: '打开导航', exact: true }).click()
    await page.locator('[aria-keyshortcuts~="Meta+K"]').click()
    await page.getByRole('button', { name: '关闭导航', exact: true }).click()
    const access = page.getByRole('button', { name: '选择访问权限' })
    const box = await access.boundingBox()
    expect(box!.width).toBeGreaterThanOrEqual(44)
    expect(box!.height).toBeGreaterThanOrEqual(44)
    await access.tap()
    for (const option of await page.getByRole('option').all()) {
      expect((await option.boundingBox())!.height).toBeGreaterThanOrEqual(44)
    }
    await page.getByRole('option', { name: '完全访问' }).tap()
    await page.getByRole('button', { name: '打开导航', exact: true }).click()
    await page.getByRole('button', { name: '定时任务', exact: true }).click()
    const card = page.getByRole('article', { name: '每日 AI 新闻简报', exact: true })
    const execute = card.getByRole('button', { name: '执行 每日 AI 新闻简报', exact: true })
    await expect(execute).toHaveCSS('opacity', '1')
    const executeBox = (await execute.boundingBox())!
    expect(executeBox.width).toBeGreaterThanOrEqual(44)
    expect(executeBox.height).toBeGreaterThanOrEqual(44)
    const icon = (await execute.locator('svg').boundingBox())!
    expect([icon.width, icon.height]).toEqual([16, 16])
    const promptBox = (await card.getByText(taskPrompt, { exact: true }).boundingBox())!
    await page.touchscreen.tap(promptBox.x + promptBox.width / 2, promptBox.y + promptBox.height / 2)
    await expect(page.getByRole('dialog', { name: '编辑定时任务', exact: true })).toBeVisible()
    await page.getByRole('button', { name: '取消', exact: true }).tap()
    await page.getByRole('button', { name: '新建定时任务', exact: true }).click()
    const editor = page.getByRole('dialog', { name: '新建定时任务', exact: true })
    await expect(editor.getByRole('button', { name: '选择访问权限' })).toHaveCount(0)
    for (const name of ['创建任务', '关闭对话框']) {
      const box = await editor.getByRole('button', { name, exact: true }).boundingBox()
      expect(box!.width).toBeGreaterThanOrEqual(44)
      expect(box!.height).toBeGreaterThanOrEqual(44)
    }
    await editor.getByRole('button', { name: '关闭对话框', exact: true }).tap()
    await page.getByRole('tab', { name: '历史', exact: true }).tap()
    await page.getByRole('button', { name: /查看运行：每日 AI 新闻简报/ }).first().tap()
    const detail = page.getByRole('dialog', { name: '每日 AI 新闻简报', exact: true })
    for (const name of ['查看关联会话', '关闭对话框']) {
      const box = await detail.getByRole('button', { name, exact: true }).boundingBox()
      expect(box!.width).toBeGreaterThanOrEqual(44)
      expect(box!.height).toBeGreaterThanOrEqual(44)
    }
  } finally { await context.close() }
})

test('自动化刷新保留页面，新会话的标志、输入框和提示位于视觉中线上方', async ({ page }, testInfo) => {
  await prepare(page)
  await expect(page).toHaveURL(url => url.searchParams.get('page') === 'automation' && url.searchParams.get('project') === 'project-1')
  await page.reload()
  await expect(page).toHaveURL(url => url.searchParams.get('page') === 'automation' && url.searchParams.get('project') === 'project-1')
  await expect(page.getByRole('tab', { name: '任务', exact: true })).toHaveAttribute('aria-selected', 'true')
  await page.locator('[aria-keyshortcuts~="Meta+K"]').click()
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
  await page.getByRole('tab', { name: '历史', exact: true }).click()
  for (const theme of ['light', 'dark']) {
    await page.evaluate(theme => { document.documentElement.dataset.theme = theme }, theme)
    for (const width of [320, 768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 900 })
      if (width < 768) await page.getByRole('button', { name: '打开导航', exact: true }).click()
      else if (await page.getByRole('button', { name: '打开侧边栏', exact: true }).isVisible()) {
        await page.getByRole('button', { name: '打开侧边栏', exact: true }).click()
      }
      const menu = page.getByRole('navigation', { name: '工作区功能' })
      for (const name of ['技能库', '记忆管理', '定时任务']) {
        await expect(menu.getByRole('button', { name, exact: true })).toHaveCSS('font-weight', '400')
      }
      await expect(menu.getByRole('button', { name: '定时任务', exact: true })).toHaveAttribute('aria-current', 'page')
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


test('历史周历只查询一次，空闲无请求，任务变化不刷新历史，搜索合并输入', async ({ page }) => {
  await page.clock.install({ time: new Date('2026-09-10T07:00:00Z') })
  const requests = await prepare(page)
  await expect(page.getByRole('tabpanel', { name: '任务', exact: true })).toHaveAttribute('aria-busy', 'false')
  const initialTasks = requests.filter(path => path === 'GET /api/automation/tasks').length
  await page.getByRole('tab', { name: '历史', exact: true }).click()
  const history = page.getByRole('tabpanel', { name: '历史', exact: true })
  await expect(history).toHaveAttribute('aria-busy', 'false')
  const calendars = () => requests.filter(path => path === 'GET /api/automation/runs/calendar').length
  expect(calendars()).toBe(1)
  expect(requests.filter(path => path === 'GET /api/automation/runs')).toEqual([])
  await page.clock.pauseAt(await page.evaluate(() => Date.now() + 1000))
  const idle = requests.length
  await page.clock.fastForward(90_000)
  expect(requests).toHaveLength(idle)
  await page.evaluate(() => window.emitResourceChange('automation.task.changed', 'task'))
  await page.clock.runFor(1000)
  expect(calendars()).toBe(1)
  await page.evaluate(() => window.emitResourceChange('automation.execution.changed', 'run'))
  await expect.poll(calendars).toBe(2)
  await expect(history).toHaveAttribute('aria-busy', 'false')
  await page.getByRole('button', { name: '搜索任务或运行历史', exact: true }).click()
  const input = page.getByRole('searchbox', { name: '搜索任务或运行历史', exact: true })
  await input.fill('每'); await input.fill('每日'); await input.fill('每日 AI')
  expect(calendars()).toBe(2)
  await page.clock.runFor(250)
  await expect(history).toHaveAttribute('aria-busy', 'false')
  expect(calendars()).toBe(3)
  await expect(page.getByRole('button', { name: /查看运行：每日 AI/ }).first()).toBeVisible()
  await page.getByRole('tab', { name: '任务', exact: true }).click()
  await expect(page.getByRole('tabpanel', { name: '任务', exact: true })).toHaveAttribute('aria-busy', 'false')
  await page.evaluate(() => window.emitResourceChange('automation.execution.changed', 'run'))
  await page.clock.fastForward(90_000)
  expect(calendars()).toBe(3)
  expect(requests.filter(path => path === 'GET /api/automation/tasks')).toHaveLength(initialTasks + 1)
})

test('运行详情区分完整回答和交付物，复用附件预览并在会话删除时保留详情', async ({ page }, info) => {
  await page.clock.install({ time: new Date('2026-09-10T07:00:00Z') })
  await prepare(page)
  await page.getByRole('tab', { name: '历史', exact: true }).click()
  await page.clock.pauseAt(new Date('2026-09-10T07:00:01Z'))
  const run = createAutomationFixture().runs.find(item => item.id === 'run-2')!
  const files = [
    { id: 'detail-report', name: '研究报告.md', mime_type: 'text/markdown', size_bytes: 32 },
    { id: 'detail-image', name: '趋势图.svg', mime_type: 'image/svg+xml', size_bytes: 220 },
  ]
  const answer = '## 本次研究结果\n\n完整回答与交付物分别呈现。\n\n- 已核对运行数据\n- 可继续在关联会话讨论'
  let deleted = false
  await page.route('**/api/automation/runs/run-2?*', route => route.fulfill({ json: { code: 0, message: 'success', data: {
    ...run, conversation: { state: 'available', projectId: 'project-1', threadId: 'detail-thread' },
    threadId: 'detail-thread', runId: 'detail-run', resultAvailable: true, messages: [{ id: 'answer', role: 'assistant', content: answer }], outputFiles: files,
  } } }))
  await page.route('**/api/automation/runs/run-2/status?*', route => route.fulfill({ json: { code: 0, message: 'success', data: {
    ...run, conversation: deleted ? { state: 'deleted' } : { state: 'available', projectId: 'project-1', threadId: 'detail-thread' },
  } } }))
  await page.route('**/api/attachments/detail-*/download-url?*', route => {
    const id = new URL(route.request().url()).pathname.split('/')[3]
    return route.fulfill({ json: { code: 0, message: 'success', data: { url: `https://files.example/${id}`, expires_in: 60 } } })
  })
  await page.route('https://files.example/*', route => route.fulfill({
    headers: { 'Access-Control-Allow-Origin': '*' },
    contentType: route.request().url().endsWith('image') ? 'image/svg+xml' : 'text/markdown',
    body: route.request().url().endsWith('image') ? '<svg xmlns="http://www.w3.org/2000/svg" width="320" height="180"><rect width="320" height="180" fill="#f6f7f8"/><path d="M20 150L80 110L150 120L220 50L300 30" fill="none" stroke="#326d67" stroke-width="4"/></svg>' : '# 研究报告\n\n交付文件正文\n\n```mermaid\ngraph LR; A[开始] --> B[完成]\n```',
  }))
  await page.context().grantPermissions(['clipboard-read', 'clipboard-write'])
  await page.getByRole('button', { name: /查看运行：每日 AI 新闻简报，2026-09-10 09:00/ }).click()
  const dialog = page.getByRole('dialog', { name: run.name })
  await expect(dialog.getByRole('heading', { name: '结果', exact: true })).toBeVisible()
  await expect(dialog.getByRole('heading', { name: '交付物', exact: true })).toHaveText('交付物')
  await expect(dialog.getByText('完整回答', { exact: true })).toHaveCount(0)
  await expect(dialog.getByText('可在关联会话中继续追问', { exact: true })).toHaveCount(0)
  await dialog.getByRole('button', { name: '复制完整回答', exact: true }).click()
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(answer)
  await expect(dialog.getByRole('button', { name: '已复制', exact: true })).toBeVisible()
  const preview = dialog.getByRole('button', { name: '预览文档：研究报告.md', exact: true })
  await preview.click()
  await expect(page.getByRole('dialog')).toHaveCount(1)
  await expect(page.getByRole('dialog', { name: '研究报告.md', exact: true }).getByText('交付文件正文')).toBeVisible()
  await page.getByRole('button', { name: '放大图表', exact: true }).click()
  await expect(page.getByRole('dialog', { name: '图表', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: '放大图表', exact: true })).toHaveCount(0)
  await page.keyboard.press('Escape')
  await expect(page.getByRole('dialog', { name: '研究报告.md', exact: true }).getByRole('button', { name: '放大图表', exact: true })).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(preview).toBeFocused()
  const image = dialog.getByRole('button', { name: '放大图片：趋势图.svg', exact: true })
  await image.click()
  await expect(page.getByRole('dialog')).toHaveCount(1)
  await expect(page.getByRole('dialog', { name: '趋势图.svg', exact: true })).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(image).toBeFocused()
  const measurements: unknown[] = []
  for (const theme of ['light', 'dark']) for (const width of [320, 768, 1024, 1440]) {
    await page.setViewportSize({ width, height: 900 })
    await page.evaluate(value => { document.documentElement.dataset.theme = value }, theme)
    await expect(dialog.getByRole('button', { name: '查看关联会话', exact: true })).toBeInViewport()
    const detailSize = await dialogMeasurements(dialog, run.name, '查看关联会话')
    const answerPadding = await dialog.getByRole('region', { name: '结果', exact: true }).evaluate(element => getComputedStyle(element).paddingInline)
    const filesPadding = await dialog.getByRole('region', { name: '交付物', exact: true }).evaluate(element => getComputedStyle(element).paddingInline)
    const [answerIcon, copyIcon, filesIcon, answerRegion, filesRegion] = await measureBounds(
      dialog.getByRole('heading', { name: '结果', exact: true }).locator('svg'),
      dialog.getByRole('button', { name: /^(复制完整回答|已复制)$/, exact: true }).locator('svg'),
      dialog.getByRole('heading', { name: '交付物', exact: true }).locator('svg'),
      dialog.getByRole('region', { name: '结果', exact: true }),
      dialog.getByRole('region', { name: '交付物', exact: true }),
    )
    for (const icon of [answerIcon, copyIcon, filesIcon]) expect([icon.width, icon.height]).toEqual([17, 17])
    expect(answerIcon.y + answerIcon.height / 2).toBeCloseTo(copyIcon.y + copyIcon.height / 2, 1)
    expect(answerPadding).toBe(`${detailSize.titleLeft}px`)
    expect(filesPadding).toBe(`${detailSize.titleLeft}px`)
    if (width < 768) {
      const sections = dialog.getByRole('navigation', { name: '详情分区' })
      expect(await sections.evaluate(element => getComputedStyle(element).paddingInline)).toBe(answerPadding)
      await expect(sections.getByRole('button', { name: '交付物', exact: true })).toHaveText('交付物')
      expect(filesRegion.y).toBeGreaterThanOrEqual(answerRegion.y + answerRegion.height)
      expect(filesIcon.x).toBeCloseTo(answerIcon.x, 1)
    } else {
      expect(filesRegion.x).toBeGreaterThanOrEqual(answerRegion.x + answerRegion.width)
      expect(answerIcon.y + answerIcon.height / 2).toBeCloseTo(filesIcon.y + filesIcon.height / 2, 1)
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)).toBe(false)
    await page.screenshot({ path: info.outputPath(`run-detail-${theme}-${width}.png`) })
    await page.evaluate(() => {
      Object.defineProperty(navigator.clipboard, 'writeText', { configurable: true,
        value: async () => { throw new DOMException('Clipboard permission denied', 'NotAllowedError') } })
    })
    let failedCopyIcon: { x: number; y: number; width: number; height: number }
    try {
      await dialog.getByRole('button', { name: /^(复制完整回答|已复制)$/, exact: true }).click()
      const failedCopy = dialog.getByRole('button', { name: '复制失败', exact: true })
      await expect(failedCopy).toBeVisible()
      const [resultIcon, failedIcon, packageIcon] = await measureBounds(
        dialog.getByRole('heading', { name: '结果', exact: true }).locator('svg'),
        failedCopy.locator('svg'),
        dialog.getByRole('heading', { name: '交付物', exact: true }).locator('svg'),
      )
      for (const icon of [resultIcon, failedIcon, packageIcon]) expect([icon.width, icon.height]).toEqual([17, 17])
      expect(resultIcon.y + resultIcon.height / 2).toBeCloseTo(failedIcon.y + failedIcon.height / 2, 1)
      if (width >= 768) expect(resultIcon.y + resultIcon.height / 2).toBeCloseTo(packageIcon.y + packageIcon.height / 2, 1)
      failedCopyIcon = failedIcon
    } finally {
      await page.evaluate(() => { Reflect.deleteProperty(navigator.clipboard, 'writeText') })
    }
    await dialog.getByRole('button', { name: '关闭对话框', exact: true }).click()
    await page.getByRole('button', { name: '新建定时任务', exact: true }).click()
    const editor = page.getByRole('dialog', { name: '新建定时任务', exact: true })
    const editorSize = await dialogMeasurements(editor, '新建定时任务', '创建任务')
    expect(detailSize).toEqual(editorSize)
    measurements.push({ theme, width, detail: detailSize, editor: editorSize, answerIcon, copyIcon, failedCopyIcon, filesIcon, answerRegion, filesRegion })
    await page.screenshot({ path: info.outputPath(`automation-editor-${theme}-${width}.png`) })
    await editor.getByRole('button', { name: '关闭对话框', exact: true }).click()
    await page.getByRole('button', { name: /查看运行：每日 AI 新闻简报，2026-09-10 09:00/ }).click()
    await expect(preview).toBeVisible()
  }
  await info.attach('dialog-measurements', { body: JSON.stringify(measurements, null, 2), contentType: 'application/json' })
  deleted = true
  await dialog.getByRole('button', { name: '关闭对话框', exact: true }).click()
  await page.unroute('**/api/automation/runs/run-2?*')
  await page.route('**/api/automation/runs/run-2?*', route => route.fulfill({ json: { code: 0, message: 'success', data: {
    ...run, conversation: { state: 'deleted' }, threadId: 'detail-thread', runId: 'detail-run', resultAvailable: true,
    messages: [{ id: 'answer', role: 'assistant', content: answer }], outputFiles: files,
  } } }))
  await page.getByRole('button', { name: /查看运行：每日 AI 新闻简报，2026-09-10 09:00/ }).click()
  const url = page.url()
  await expect(dialog.getByRole('button', { name: '查看关联会话', exact: true })).toBeEnabled()
  await expect(dialog.getByRole('button', { name: /^引用附件：/ })).toHaveCount(0)
  await dialog.getByRole('button', { name: '查看关联会话', exact: true }).click()
  await expect(page.getByRole('list', { name: '系统提示' })).toContainText('关联会话已删除')
  await expect(dialog).toBeVisible()
  expect(page.url()).toBe(url)
  const download = page.waitForEvent('download')
  await preview.hover()
  await dialog.getByRole('button', { name: '下载附件：研究报告.md', exact: true }).click()
  expect((await download).suggestedFilename()).toBe('研究报告.md')
})

for (const state of ['pending', 'not_created'] as const) {
  test(`关联会话 ${state} 时禁止跳转和引用，交付物仍可预览下载`, async ({ page }) => {
    await page.clock.install({ time: new Date('2026-09-10T07:00:00Z') })
    await prepare(page)
    await page.getByRole('tab', { name: '历史', exact: true }).click()
    await page.clock.pauseAt(new Date('2026-09-10T07:00:01Z'))
    await page.clock.setSystemTime(new Date('2026-09-10T07:00:00Z'))
    const run = createAutomationFixture().runs.find(item => item.id === 'run-2')!
    let conversation: AutomationRunRecord['conversation'] = { state }
    const file = { id: 'pending-file', name: '交付报告.md', mime_type: 'text/markdown', size_bytes: 32 }
    let detailReads = 0
    await page.route('**/api/automation/runs/run-2?*', route => {
      detailReads += 1
      return route.fulfill({ json: { code: 0, message: 'success', data: {
        ...run, conversation, threadId: 'pending-thread', runId: 'pending-run', resultAvailable: true,
        messages: [{ id: 'answer', role: 'assistant', content: '任务结果已保留' }], outputFiles: [file],
      } } })
    })
    await page.route('**/api/attachments/pending-file/download-url?*', route => route.fulfill({
      json: { code: 0, message: 'success', data: { url: 'https://files.example/pending-file', expires_in: 60 } },
    }))
    await page.route('https://files.example/pending-file', route => route.fulfill({
      headers: { 'Access-Control-Allow-Origin': '*' }, contentType: 'text/markdown', body: '# 交付报告\n\n待公开时仍可阅读和下载',
    }))
    await page.getByRole('button', { name: /查看运行：每日 AI 新闻简报，2026-09-10 09:00/ }).click()
    const dialog = page.getByRole('dialog', { name: run.name })
    const open = dialog.getByRole('button', { name: '查看关联会话', exact: true })
    await expect(open).toBeDisabled()
    await expect(dialog.getByText('运行完成', { exact: true })).toBeVisible()
    await expect(dialog.getByRole('button', { name: /^引用附件：/ })).toHaveCount(0)
    const preview = dialog.getByRole('button', { name: '预览文档：交付报告.md', exact: true })
    await preview.click()
    await expect(page.getByRole('dialog', { name: '交付报告.md', exact: true }).getByText('待公开时仍可阅读和下载')).toBeVisible()
    await expect(page.getByRole('button', { name: '引用附件', exact: true })).toHaveCount(0)
    await page.keyboard.press('Escape')
    await expect(preview).toBeFocused()
    const download = page.waitForEvent('download')
    await preview.hover()
    await dialog.getByRole('button', { name: '下载附件：交付报告.md', exact: true }).click()
    expect((await download).suggestedFilename()).toBe(file.name)
    const before = detailReads
    await page.clock.fastForward(30_000)
    if (state === 'pending') await expect.poll(() => detailReads).toBe(before + 1)
    else expect(detailReads).toBe(before)
    if (state === 'not_created') return
    conversation = { state: 'available', projectId: 'project-1', threadId: 'pending-thread' }
    await page.evaluate(() => window.emitResourceChange('studio.conversation.changed', 'pending-thread', {
      scope: { namespace: 'ns_1', owner_id: null }, details: { automation_execution_id: 'run-2', automation_project_id: 'project-1' },
    }))
    await page.clock.runFor(2000)
    await expect(open).toBeEnabled()
    await expect(dialog.getByRole('button', { name: '引用附件：交付报告.md', exact: true })).toHaveCount(1)
  })
}

test('卡片执行支持悬停和键盘，权威终态立即提示且迟到回执不重复通知', async ({ page }) => {
  await page.clock.install({ time: new Date('2026-09-10T07:00:00Z') })
  await prepare(page)
  await page.clock.pauseAt(new Date('2026-09-10T07:00:01Z'))
  let run: AutomationRunRecord = runFixture({ id: 'online-run', status: 'queued', conversation: { state: 'pending' }, finishedAt: null })
  const queued = run
  let requestId: string | null = null
  let receiptAvailable = false
  let statusReads = 0
  let releasePost!: () => void
  const response = new Promise<void>(resolve => { releasePost = resolve })
  await page.route('**/api/automation/tasks/news/run?*', async route => {
    requestId = route.request().postDataJSON().requestId
    await response
    await route.fulfill({ json: { code: 0, message: 'success', data: queued } })
  })
  await page.route('**/api/automation/tasks/news/submission?*', route => {
    const query = new URL(route.request().url()).searchParams
    expect(route.request().method()).toBe('GET')
    expect(query.get('projectId')).toBe('project-1')
    return route.fulfill({ json: { code: 0, message: 'success', data: receiptAvailable && query.get('requestId') === requestId ? run : null } })
  })
  await page.route('**/api/automation/runs/online-run/status?*', route => {
    statusReads += 1
    return route.fulfill({ json: { code: 0, message: 'success', data: run } })
  })
  await page.getByRole('tab', { name: '任务', exact: true }).click()
  const card = page.getByRole('article', { name: '每日 AI 新闻简报', exact: true })
  const execute = card.getByRole('button', { name: '执行 每日 AI 新闻简报', exact: true })
  await expect(execute).toHaveCSS('opacity', '0')
  await card.hover()
  await expect(execute).toHaveCSS('opacity', '1')
  const icon = (await execute.locator('svg').boundingBox())!
  expect([icon.width, icon.height]).toEqual([16, 16])
  await page.mouse.move(0, 0)
  await expect(execute).toHaveCSS('opacity', '0')
  await card.getByRole('button', { name: '每日 AI 新闻简报', exact: true }).focus()
  await expect(execute).toHaveCSS('opacity', '1')
  await page.keyboard.press('Tab')
  await expect(execute).toBeFocused()
  await execute.click()
  await expect(execute).toBeDisabled()
  await expect(execute).toHaveAttribute('aria-busy', 'true')
  await page.getByRole('tab', { name: '任务', exact: true }).focus()
  await page.mouse.move(0, 0)
  expect(Number(await execute.evaluate(element => getComputedStyle(element).opacity))).toBeGreaterThan(0)
  await expect(execute.locator('svg')).toHaveCSS('width', '16px')
  await expect(execute.locator('svg')).toHaveCSS('height', '16px')
  await expect.poll(() => requestId).not.toBeNull()
  run = { ...run, status: 'succeeded', updatedAt: '2026-09-10T07:00:01Z', finishedAt: '2026-09-10T07:00:01Z' }
  receiptAvailable = true
  await page.evaluate(() => window.emitResourceChange('automation.execution.changed', 'online-run', {
    scope: { namespace: 'studio_automation', owner_id: '1:project-1' }, details: {},
  }))
  await expect(execute).toBeEnabled()
  const toasts = page.getByRole('list', { name: '系统提示' })
  await expect(toasts.getByText('每日 AI 新闻简报：运行完成', { exact: true })).toHaveCount(1)
  await expect(toasts.getByRole('button', { name: '查看关联会话', exact: true })).toHaveCount(0)
  const replied = page.waitForResponse(response => new URL(response.url()).pathname === '/api/automation/tasks/news/run')
  releasePost()
  await replied
  await expect(execute).toBeEnabled()
  await expect(toasts.getByText('已加入运行队列', { exact: true })).toHaveCount(0)
  await expect(toasts.getByText('每日 AI 新闻简报：运行完成', { exact: true })).toHaveCount(1)
  const readsBeforePublication = statusReads
  run = { ...run, conversation: { state: 'available', projectId: 'project-1', threadId: 'online-thread' } }
  await page.evaluate(() => window.emitResourceChange('studio.conversation.changed', 'online-thread', {
    scope: { namespace: 'ns_1', owner_id: null }, details: { automation_execution_id: 'online-run', automation_project_id: 'project-1' },
  }))
  await page.evaluate(() => window.emitResourceChange('studio.conversation.changed', 'online-thread', {
    scope: { namespace: 'ns_1', owner_id: null }, details: { automation_execution_id: 'online-run', automation_project_id: 'project-1' },
  }))
  await page.clock.runFor(2000)
  expect(statusReads).toBe(readsBeforePublication)
  await expect(toasts.getByText('每日 AI 新闻简报：运行完成', { exact: true })).toHaveCount(1)
  await expect(toasts.getByRole('button', { name: '查看关联会话', exact: true })).toHaveCount(0)
})
