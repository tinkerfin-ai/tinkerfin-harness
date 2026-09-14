import { installLiveRun } from './fixtures/liveRun'
import { expect, test, type Page, type Route } from '@playwright/test'
import { resolve } from 'node:path'

import type {
  ConversationHistoryDetail,
  TraceMessage,
} from '../../src/api/conversation/history'
import type { TraceGraphNode, TraceGraphTurn } from '../../src/api/conversation/traceGraph'
import type { TaskTraceSnapshot, TodoGroup } from '../../src/api/conversation/taskTrace'
import { traceGraphNode, traceGraphWithNodes } from '../../src/test/traceFixtures'

const THREAD_ID = 'todo-trace-browser-thread'
const RUN_ID = 'todo-trace-browser-run'
const BASE_TIME = Date.UTC(2026, 7, 31, 12)
const EVIDENCE_DIR = resolve(
  process.cwd(),
  '../../../.agents/evidence/20260828014138-trace-persistence-studio-authority/implementation/browser',
)
const user = {
  user_id: 17,
  username: 'todo-browser-user',
  display_name: '任务轨迹用户',
  avatar_url: null,
  roles: [],
  disabled: false,
}

const success = (data: unknown) => ({ code: 0, message: 'success', data })
const fulfillJson = (route: Route, data: unknown) => route.fulfill({
  status: 200,
  contentType: 'application/json',
  body: JSON.stringify(success(data)),
})

const makeGroups = (
  count: number,
  running = false,
  todosPerGroup = 2,
): TodoGroup[] => (
  Array.from({ length: count }, (_, index) => ({
    id: `todo-group:run-${index}`,
    userMessageId: `todo-user-message-${index}`,
    userMessagePreview: index === 0 ? '整理当前交付清单' : `历史任务轨迹 ${index + 1}`,
    groupToolCallId: `todo-tool-call-${index}`,
    createdAt: new Date(BASE_TIME - (index * 1_000)).toISOString(),
    status: running && index === 0 ? 'running' : 'completed',
    todos: Array.from({ length: todosPerGroup }, (_, todoIndex) => ({
      id: `todo-${index}-${todoIndex}`,
      content: todoIndex === 0
        ? index === 0
          ? '检查实现与验收证据'
          : `完成历史任务 ${index + 1}`
        : `保存可复核结果 ${todoIndex}`,
      status: running && index === 0 && todoIndex === 0
        ? 'running'
        : 'completed',
    })),
  }))
)

const traceEntities = (groups: readonly TodoGroup[], visibleGroups: readonly TodoGroup[]) => {
  const messages: TraceMessage[] = []
  const nodes: TraceGraphNode[] = []
  const turns: TraceGraphTurn[] = []
  const visibleIds = new Set(visibleGroups.map((group) => group.id))
  for (const [position, group] of [...groups].reverse().entries()) {
    if (!visibleIds.has(group.id)) continue
    const sequence = (position * 2) + 1
    const turnId = `turn:${group.id}`
    turns.push({ id: turnId, ordinal: position + 1, startedAt: group.createdAt })
    messages.push({
      agui: { kind: 'message', messageId: `public-${group.userMessageId}` },
      id: group.userMessageId,
      traceSeq: sequence,
      sourceId: group.userMessageId,
      graphNamespace: [],
      runId: group.id.slice('todo-group:'.length),
      role: 'user',
      content: group.userMessagePreview,
      contentOmitted: false,
      status: 'completed',
      createdAt: group.createdAt,
      completedAt: group.createdAt,
    })
    nodes.push(traceGraphNode({
      id: group.groupToolCallId,
      turnId,
      startedSeq: sequence + 1,
      updatedSeq: sequence + 1,
      name: 'write_todos',
      runId: group.id.slice('todo-group:'.length),
      sourceId: group.groupToolCallId,
      request: null,
      requestOmitted: true,
      result: null,
      resultOmitted: true,
      status: group.status === 'running' ? 'running' : 'succeeded',
      startedAt: group.createdAt,
      completedAt: group.status === 'running' ? null : group.createdAt,
    }))
  }
  return { messages, nodes, turns }
}

const detail = ({
  groups,
  visibleGroups = groups,
  taskTraceGroups = groups,
  includeTaskTrace,
  answer,
  historyCursor = null,
}: {
  groups: readonly TodoGroup[]
  visibleGroups?: readonly TodoGroup[]
  taskTraceGroups?: readonly TodoGroup[]
  includeTaskTrace: boolean
  answer?: string
  historyCursor?: string | null
}): ConversationHistoryDetail => {
  const entities = traceEntities(groups, visibleGroups)
  if (answer) entities.messages.push({
    agui: null, id: 'spacing-answer', traceSeq: groups.length * 2 + 1,
    sourceId: 'spacing-answer', graphNamespace: [], runId: RUN_ID,
    role: 'assistant', content: answer, contentOmitted: false, status: 'completed',
    createdAt: new Date(BASE_TIME).toISOString(), completedAt: new Date(BASE_TIME).toISOString(),
  })
  const taskTrace: TaskTraceSnapshot | null = includeTaskTrace
    ? { status: 'ready', todoGroups: [...taskTraceGroups] }
    : null
  return { accessMode: 'write_approval',
    id: 1,
    threadId: THREAD_ID,
    title: '任务轨迹浏览器会话',
    titleSource: 'default',
    titleGenerationStatus: 'idle',
    titleSeq: 0,
    lastModel: 'GPT-5.5',
    pinned: false,
    asOfSeq: Math.max(1, (groups.length * 2) + 1),
    generation: `browser-generation:${THREAD_ID}`,
    observedAt: '2026-09-05T00:00:00.000000Z',
    headRunId: RUN_ID,
    runFailures: [],
    availableHeads: [RUN_ID],
    historyCursor,
    messageCount: groups.length + (answer ? 1 : 0),
    toolCallCount: groups.length,
    messages: entities.messages,
    reasoning: [],
    graph: {
      ...traceGraphWithNodes(entities.nodes, Math.max(1, (groups.length * 2) + 1)),
      turns: entities.turns,
    },
    state: { root: {}, subgraphs: {} },
    interactions: [],
    status: {
      execution: groups[0]?.status === 'running' ? 'running' : 'succeeded',
      headRunId: RUN_ID,
    },
    completeness: {
      missingPrefix: false,
      missingTail: false,
      payloadOmitted: false,
    },
    taskTrace,
    createdAt: new Date(BASE_TIME).toISOString(),
    updatedAt: new Date(BASE_TIME).toISOString(),
  }
}

async function mockTodoTraceStudio(page: Page, {
  groups,
  answer,
  taskTraceGroups = groups,
  visibleGroups = groups,
  olderGroups,
  historyCursor = null,
}: {
  groups: TodoGroup[]
  answer?: string
  taskTraceGroups?: TodoGroup[]
  visibleGroups?: TodoGroup[]
  olderGroups?: TodoGroup[]
  historyCursor?: string | null
}) {
  const pageErrors: string[] = []
  let olderRequests = 0
  page.on('pageerror', (error) => pageErrors.push(error.message))
  page.on('console', (message) => {
    if (message.type() === 'error') pageErrors.push(message.text())
  })
  await page.addInitScript(({ session }) => {
    window.localStorage.setItem('tinkerfin.auth.session', JSON.stringify(session))
  }, {
    session: {
      token: 'todo-browser-token',
      tokenType: 'Bearer',
      expiresAt: '2099-01-01T00:00:00.000Z',
      user,
    },
  })
  await page.route('**/api/**', async (route) => {
    const url = new URL(route.request().url())
    if (url.pathname === '/api/auth/me') {
      await fulfillJson(route, { expires_at: '2099-01-01T00:00:00.000Z', user })
      return
    }
    if (url.pathname === '/api/models') {
      await fulfillJson(route, {
        items: [{
          modelId: 'GPT-5.5',
          displayName: 'GPT-5.5',
          reasoningEnabled: false,
          isDefault: true,
        }],
        defaultModelId: 'GPT-5.5',
      })
      return
    }
    if (url.pathname === '/api/conversation/config') {
      await fulfillJson(route, { dayRanges: [7, 30] })
      return
    }
    if (url.pathname === '/api/conversation/history') {
      await fulfillJson(route, {
        items: [{ accessMode: 'full',
          id: 1,
          threadId: THREAD_ID,
          title: '任务轨迹浏览器会话',
          status: visibleGroups[0]?.status === 'running' ? 'running' : 'idle',
          lastRunId: RUN_ID,
          lastModel: 'GPT-5.5',
          messageCount: groups.length + (answer ? 1 : 0),
          toolCallCount: groups.length,
          hasPendingInterrupt: false,
          pendingInteractionKind: null,
          pinned: false,
          createdAt: new Date(BASE_TIME).toISOString(),
          updatedAt: new Date(BASE_TIME).toISOString(),
        }],
        nextCursor: null,
      })
      return
    }
    if (url.pathname === `/api/conversation/${THREAD_ID}/history`) {
      const includeTaskTrace = url.searchParams.get('includeTaskTrace') !== 'false'
      const cursor = url.searchParams.get('historyCursor')
      if (cursor && olderGroups) {
        olderRequests += 1
        await fulfillJson(route, detail({
          groups,
          answer,
          taskTraceGroups,
          visibleGroups: [...visibleGroups, ...olderGroups],
          includeTaskTrace: false,
          historyCursor: null,
        }))
        return
      }
      await fulfillJson(route, detail({
        groups,
        answer,
        taskTraceGroups,
        visibleGroups,
        includeTaskTrace,
        historyCursor,
      }))
      return
    }
    if (url.pathname === `/api/conversation/${THREAD_ID}/trace`) {
      const includeTaskTrace = url.searchParams.get('includeTaskTrace') !== 'false'
      await route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body: `event: trace\ndata: ${JSON.stringify({
          type: 'snapshot',
          snapshot: detail({
            groups,
            answer,
            taskTraceGroups,
            visibleGroups,
            includeTaskTrace,
            historyCursor,
          }),
        })}\n\n`,
      })
      return
    }
    await route.fulfill({ status: 404, contentType: 'application/json', body: '{}' })
  })
  await installLiveRun(page, detail({ groups, answer, taskTraceGroups, visibleGroups, includeTaskTrace: true, historyCursor }))
  await page.goto(`/?thread=${THREAD_ID}`)
  if (taskTraceGroups.length > 0) {
    await expect(page.getByRole('button', { name: `任务轨迹 ${taskTraceGroups.length}`, exact: true }))
      .toBeVisible()
  } else {
    await expect(page.getByRole('textbox', { name: '消息输入' })).toBeVisible()
  }
  return { olderRequestCount: () => olderRequests, pageErrors }
}

test('统一抽屉展示、独立展开、定位和 overlay 焦点恢复可真实交互', async ({ page }) => {
  await page.setViewportSize({ width: 768, height: 900 })
  const groups = makeGroups(3, true)
  const evidence = await mockTodoTraceStudio(page, { groups })
  const launcher = page.getByRole('button', { name: '任务轨迹 3', exact: true })

  await expect(launcher).toHaveCSS('border-top-width', '0px')
  await launcher.hover()
  await expect(launcher).toHaveCSS('border-top-width', '0px')
  await launcher.click()
  const drawer = page.getByRole('complementary', { name: '任务轨迹' })
  await expect(drawer).toBeVisible()
  await expect(page.locator('.todo-trace-floating-launcher')).toHaveCount(0)
  await page.getByRole('button', { name: '关闭任务轨迹' }).click()
  await expect(drawer).toBeHidden()
  await expect(launcher).toBeFocused()
  await launcher.click()
  await expect(drawer).toBeVisible()
  await expect(page.getByRole('button', { name: '关闭任务轨迹' })).toHaveCount(1)
  await expect(drawer.getByRole('heading', { name: '任务轨迹' })).toHaveCount(1)
  await expect(drawer.getByText('Trace', { exact: true })).toHaveCount(0)
  await expect(drawer.getByRole('tooltip')).toHaveCount(0)
  await expect(page.getByText('Todos', { exact: true }).first()).toBeVisible()
  const drawerRegionStyles = await drawer.locator('.todo-trace-drawer-region').evaluate((element) => {
    const style = getComputedStyle(element)
    return {
      borderTopWidth: style.borderTopWidth,
      borderRadius: style.borderRadius,
      backgroundColor: style.backgroundColor,
    }
  })
  expect(drawerRegionStyles).toEqual({
    borderTopWidth: '0px',
    borderRadius: '0px',
    backgroundColor: 'rgba(0, 0, 0, 0)',
  })
  const completedMarkStyles = await drawer.locator('.todo-trace-completed-mark').first()
    .evaluate((element) => {
      const wrapper = element.parentElement
      if (!wrapper) return null
      const wrapperStyle = getComputedStyle(wrapper)
      const markStyle = getComputedStyle(element)
      return {
        wrapperBorderWidth: wrapperStyle.borderWidth,
        wrapperBackground: wrapperStyle.backgroundColor,
        wrapperBoxShadow: wrapperStyle.boxShadow,
        markFilter: markStyle.filter,
        circleCount: element.querySelectorAll('circle').length,
      }
    })
  expect(completedMarkStyles).toEqual({
    wrapperBorderWidth: '0px',
    wrapperBackground: 'rgba(0, 0, 0, 0)',
    wrapperBoxShadow: 'none',
    markFilter: 'none',
    circleCount: 1,
  })
  await expect(drawer.locator('.todo-trace-group-state')).toHaveCount(0)
  const latest = page.getByRole('button', { name: '收起任务组：整理当前交付清单' })
  await expect(latest)
    .toHaveAttribute('aria-expanded', 'true')
  await latest.click()
  const collapsedLatest = page.getByRole('button', { name: '展开任务组：整理当前交付清单' })
  await expect(collapsedLatest).toHaveAttribute('aria-expanded', 'false')
  await expect(drawer.getByText('检查实现与验收证据')).toHaveCount(0)
  await collapsedLatest.click()
  await expect(latest).toHaveAttribute('aria-expanded', 'true')
  await expect(drawer.getByText('检查实现与验收证据')).toBeVisible()
  await page.getByRole('button', { name: '展开任务组：历史任务轨迹 2' }).click()
  await expect(drawer.getByText('完成历史任务 2')).toBeVisible()
  await expect(drawer.getByText('检查实现与验收证据')).toBeVisible()
  const historyHeadingAlignment = await drawer.getByRole('button', {
    name: '收起任务组：历史任务轨迹 2',
  }).evaluate((element) => {
    const title = element.querySelector('.todo-trace-group-title')
    const progress = element.querySelector('.todo-trace-group-progress')
    if (!title || !progress) return null
    const titleRect = title.getBoundingClientRect()
    const progressRect = progress.getBoundingClientRect()
    return {
      centerDelta: Math.abs(
        (titleRect.top + (titleRect.height / 2))
        - (progressRect.top + (progressRect.height / 2)),
      ),
      gap: progressRect.left - titleRect.right,
    }
  })
  expect(historyHeadingAlignment).not.toBeNull()
  expect(historyHeadingAlignment?.centerDelta).toBeLessThanOrEqual(0.5)
  expect(historyHeadingAlignment?.gap).toBeGreaterThanOrEqual(8)

  await page.getByRole('button', { name: '定位到对话：历史任务轨迹 2' }).click()
  await expect(drawer).toBeHidden()
  await expect(page.locator('#public-todo-user-message-1')).toBeFocused()
  await expect(page.locator('#public-todo-user-message-1')).toHaveClass(/todo-trace-locate-target/)
  await expect(page.locator('#public-todo-user-message-1')).toHaveCSS('outline-style', 'none')
  await expect(page.locator('#public-todo-user-message-1')).toHaveCSS('border-width', '0px')
  await expect(page.locator('#public-todo-user-message-1 .message-markdown')).toHaveCSS('outline-style', 'none')

  await launcher.click()
  await expect(drawer).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(drawer).toBeHidden()
  await expect(launcher).toBeFocused()
  expect(evidence.pageErrors).toEqual([])
})

test('未水化消息通过可取消的旧 Trace 分页后定位', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  const groups = makeGroups(3)
  const older = groups.slice(2)
  const evidence = await mockTodoTraceStudio(page, {
    groups,
    visibleGroups: groups.slice(0, 2),
    olderGroups: older,
    historyCursor: 'older-page',
  })

  const desktopLauncher = page.getByRole('button', { name: '任务轨迹 3', exact: true })
  await desktopLauncher.click()
  await expect(desktopLauncher).toHaveCSS('border-top-width', '0px')
  await expect(desktopLauncher).not.toHaveCSS('box-shadow', 'none')
  if (process.env.TINKERFIN_VISUAL_QA_DIR) {
    await desktopLauncher.screenshot({
      path: resolve(process.env.TINKERFIN_VISUAL_QA_DIR, 'tinkerfin-task-trace-launcher.png'),
    })
  }
  await page.getByRole('button', { name: '展开任务组：历史任务轨迹 3' }).click()
  await page.getByRole('button', { name: '定位到对话：历史任务轨迹 3' }).click()

  await expect.poll(evidence.olderRequestCount).toBe(1)
  await expect(page.locator('#public-todo-user-message-2')).toBeFocused()
  await expect(page.locator('#public-todo-user-message-2')).toHaveClass(/todo-trace-locate-target/)
  expect(evidence.pageErrors).toEqual([])
})

test('任务对应的用户消息缺失时关闭抽屉并说明原因', async ({ page }) => {
  await page.setViewportSize({ width: 768, height: 900 })
  const groups = makeGroups(1)
  const missing = {
    ...makeGroups(1)[0]!,
    id: 'todo-group:missing',
    userMessageId: 'missing-user-message',
    userMessagePreview: '缺失的用户消息',
  }
  await mockTodoTraceStudio(page, { groups, taskTraceGroups: [...groups, missing] })

  await page.getByRole('button', { name: '任务轨迹 2', exact: true }).click()
  await page.getByRole('button', { name: '展开任务组：缺失的用户消息', exact: true }).click()
  await page.getByRole('button', { name: '定位到对话：缺失的用户消息', exact: true }).click()

  await expect(page.getByRole('complementary', { name: '任务轨迹' })).toBeHidden()
  await expect(page.getByText('未找到任务对应的用户消息', { exact: true })).toBeVisible()
})

test('已结束但未确认完成的旧清单保持 3/4 且不继续旋转', async ({ page }) => {
  const groups = makeGroups(2, false, 4)
  groups[1]!.status = 'incomplete'
  groups[1]!.todos[3]!.status = 'incomplete'
  const evidence = await mockTodoTraceStudio(page, { groups })
  await page.getByRole('button', { name: '任务轨迹 2', exact: true }).click()
  const drawer = page.getByRole('complementary', { name: '任务轨迹' })
  const older = drawer.getByRole('button', { name: '展开任务组：历史任务轨迹 2' })
  await older.click()
  for (const theme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: theme, reducedMotion: 'reduce' })
    for (const width of [320, 768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 900 })
      await expect(drawer.getByRole('button', { name: '收起任务组：历史任务轨迹 2' }))
        .toContainText(/未确认完成\s*3\s*\/\s*4/)
      await expect(drawer.getByRole('button', { name: /任务组：整理当前交付清单/ }))
        .toContainText(/已完成\s*4\s*\/\s*4/)
      await expect(drawer.getByRole('list', { name: '任务列表' }).getByText('未确认完成'))
        .toBeVisible()
      await expect(drawer.locator('.todo-trace-spin')).toHaveCount(0)
    }
  }
  expect(evidence.pageErrors).toEqual([])
})

test('320px 深色高对比与 reduced-motion 下保持全宽和静态状态反馈', async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 800 })
  await page.emulateMedia({
    colorScheme: 'dark',
    reducedMotion: 'reduce',
    forcedColors: 'active',
  })
  const groups = makeGroups(1, true)
  const evidence = await mockTodoTraceStudio(page, { groups })

  const launcher = page.getByRole('button', { name: '任务轨迹 1', exact: true })
  await launcher.click()
  const drawer = page.getByRole('complementary', { name: '任务轨迹' })
  await expect(drawer).toBeVisible()
  await expect(drawer.getByRole('heading', { name: '任务轨迹' })).toBeVisible()
  await expect(page.locator('.todo-trace-floating-launcher')).toHaveCount(0)
  const headerLayout = await page.evaluate(() => {
    const header = document.querySelector<HTMLElement>('#todo-trace-drawer .ui-drawer-header')
    const headingBlock = document.querySelector<HTMLElement>('#todo-trace-drawer .ui-drawer-header__heading')
    const heading = document.querySelector<HTMLElement>('#todo-trace-drawer h2')
    const close = document.querySelector<HTMLElement>(
      '#todo-trace-drawer button[aria-label="关闭任务轨迹"]',
    )
    if (!header || !headingBlock || !heading || !close) return null
    const headerRect = header.getBoundingClientRect()
    const headingBlockRect = headingBlock.getBoundingClientRect()
    const headingRect = heading.getBoundingClientRect()
    const closeRect = close.getBoundingClientRect()
    return {
      headingRight: headingRect.right,
      headingCenterY: headingRect.top + (headingRect.height / 2),
      closeLeft: closeRect.left,
      closeCenterY: closeRect.top + (closeRect.height / 2),
      topGap: headingBlockRect.top - headerRect.top,
    }
  })
  expect(headerLayout).not.toBeNull()
  expect(headerLayout?.headingRight).toBeLessThanOrEqual(headerLayout?.closeLeft ?? 0)
  expect(Math.abs(
    (headerLayout?.headingCenterY ?? 0) - (headerLayout?.closeCenterY ?? 0),
  )).toBeLessThanOrEqual(0.5)
  expect(headerLayout?.topGap ?? 0).toBeGreaterThanOrEqual(8)
  const layout = await drawer.evaluate((element) => {
    const style = getComputedStyle(element)
    return {
      width: element.getBoundingClientRect().width,
      transitionDuration: style.transitionDuration,
      overflow: Math.max(
        document.documentElement.scrollWidth - document.documentElement.clientWidth,
        document.body.scrollWidth - document.body.clientWidth,
      ),
    }
  })
  expect(layout.width).toBe(320)
  expect(layout.transitionDuration.split(',').every((value) => value.trim() === '0s')).toBe(true)
  expect(layout.overflow).toBeLessThanOrEqual(0)
  await expect(page.locator('.todo-trace-spin').first()).toHaveCSS('animation-name', 'none')
  await page.screenshot({ path: resolve(EVIDENCE_DIR, '320-forced-colors.png') })
  expect(evidence.pageErrors).toEqual([])
})

test('对话 Todos 卡片在浅深色和各视口保持布局与完成图标契约', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' })
  const evidence = await mockTodoTraceStudio(page, { groups: makeGroups(1, false, 3) })
  const card = page.locator('details').filter({ has: page.getByText('Todos', { exact: true }) }).first()
  await card.locator('summary').click()
  const mark = card.locator('.todo-trace-completed-mark').first()
  await expect(mark).toBeVisible()
  const treeMetrics = await card.evaluate((element) => {
    const summary = element.querySelector(':scope > summary')
    const list = element.querySelector('.todo-trace-todo-list')
    const items = [...element.querySelectorAll('.todo-trace-todo')]
    if (!summary || !list || items.length < 2) throw new Error('Todos 卡片缺少布局测量目标')
    const centerY = (target: Element) => {
      const rect = target.getBoundingClientRect()
      return rect.top + (rect.height / 2)
    }
    const listRect = list.getBoundingClientRect()
    const treeStyle = getComputedStyle(list, '::before')
    return {
      firstInterval: centerY(items[0]) - centerY(summary),
      itemInterval: centerY(items[1]) - centerY(items[0]),
      treeEnd: listRect.bottom - Number.parseFloat(treeStyle.bottom),
      lastItemCenter: centerY(items.at(-1)!),
    }
  })
  expect(treeMetrics.firstInterval).toBe(treeMetrics.itemInterval)
  expect(treeMetrics.treeEnd).toBe(treeMetrics.lastItemCenter)
  for (const theme of ['light', 'dark']) {
    await page.evaluate((value) => {
      document.documentElement.dataset.theme = value
      document.documentElement.dataset.themePreference = value
      document.documentElement.style.colorScheme = value
    }, theme)
    for (const width of [320, 768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 900 })
      await card.scrollIntoViewIfNeeded()
      await expect(mark).toHaveAttribute('width', '16')
      await expect(mark).toHaveAttribute('stroke-width', '1.5')
      await expect(mark.locator('circle')).toHaveCount(1)
      const metrics = await mark.evaluate((element) => {
        const probe = document.createElement('span')
        probe.style.color = 'var(--color-text-secondary)'
        element.parentElement!.append(probe)
        const expectedColor = getComputedStyle(probe).color
        probe.remove()
        return {
          color: getComputedStyle(element).color,
          expectedColor,
          overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
        }
      })
      expect(metrics.color).toBe(metrics.expectedColor)
      expect(metrics.overflow).toBeLessThanOrEqual(0)
      if (process.env.TINKERFIN_VISUAL_QA_DIR) {
        await page.screenshot({ path: resolve(process.env.TINKERFIN_VISUAL_QA_DIR, `todo-card-${theme}-${width}.png`) })
      }
    }
  }
  await card.locator('summary').click()
  await expect(mark).toBeHidden()
  expect(evidence.pageErrors).toEqual([])
})


for (const hasTouch of [false, true]) {
  test(`有无任务轨迹时复制按钮到输入框的间距一致（触控：${hasTouch}）`, async ({ browser, baseURL }, testInfo) => {
    const context = await browser.newContext({ hasTouch, baseURL })
    const page = await context.newPage()
    try {
      const groups = makeGroups(1)
      const taskTraceGroups: TodoGroup[] = []
      await page.emulateMedia({ reducedMotion: 'reduce' })
      await mockTodoTraceStudio(page, {
        groups, taskTraceGroups,
        answer: Array.from({ length: 40 }, (_, i) => `验证段落 ${i + 1}：用于保持回答内容超过当前视口。`).join('\n\n'),
      })
      for (const theme of ['light', 'dark']) {
        for (const width of [320, 768, 1024, 1440]) {
          const gaps: number[] = []
          await page.setViewportSize({ width, height: 900 })
          for (const withTasks of [false, true]) {
            taskTraceGroups.splice(0, taskTraceGroups.length, ...(withTasks ? groups : []))
            await page.evaluate(theme => localStorage.setItem('tinkerfin:theme', theme), theme)
            await page.reload()
            await expect(page.locator('html')).toHaveAttribute('data-theme', theme)
            const copy = page.getByRole('button', { name: '复制回答', exact: true }).last()
            await expect(copy).toBeAttached()
            await expect(page.getByRole('button', { name: '任务轨迹 1', exact: true })).toHaveCount(withTasks ? 1 : 0)
            await expect.poll(() => page.evaluate(() => {
              const dock = document.querySelector('.composer-dock')!
              const measured = parseFloat(getComputedStyle(document.querySelector('.message-list')!).paddingBottom)
              return Math.abs(dock.getBoundingClientRect().height - measured)
            })).toBeLessThanOrEqual(1)
            await page.evaluate(() => document.fonts.ready)
            const gap = await page.getByRole('region', { name: '对话内容', exact: true }).evaluate(el => {
              el.scrollTop = el.scrollHeight
              const copy = el.querySelectorAll<HTMLButtonElement>('button[aria-label="复制回答"]')
              const copyBox = copy[copy.length - 1].getBoundingClientRect()
              const composerBox = document.querySelector('.composer')!.getBoundingClientRect()
              return composerBox.top - copyBox.bottom
            })
            await expect(copy).toBeInViewport()
            expect(gap).toBeGreaterThan(40)
            gaps.push(gap)
            if (width === 320 || width === 1440) await page.screenshot({ path: testInfo.outputPath(`spacing-${theme}-${width}-${withTasks}.png`) })
          }
          console.log(JSON.stringify({ hasTouch, theme, width, gaps }))
          expect(Math.abs(gaps[0] - gaps[1])).toBeLessThanOrEqual(1)
        }
      }
    } finally { await context.close() }
  })
}
