import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { Conversation, WorkspaceState } from '../../../types'
import { Sidebar } from './Sidebar'

const workspace: WorkspaceState = {
  currentThreadId: 'recent',
  conversations: [
    {projectId: 'project-1', archived: false,  accessMode: 'write_approval',
      threadId: 'pinned',
      historySynchronized: false,
      title: '置顶会话',
      pinned: true,
      updatedAt: '2026-08-08T08:00:00Z',
      model: 'GPT-5.5',
      mode: 'default',
      messages: [],
      todos: [],
      taskTrace: { phase: 'unloaded' },
      runStatus: 'idle',
    },
    {projectId: 'project-1', archived: false,  accessMode: 'write_approval',
      threadId: 'recent',
      historySynchronized: false,
      title: '最近会话',
      pinned: false,
      updatedAt: '2026-08-08T09:00:00Z',
      model: 'GPT-5.5',
      mode: 'default',
      messages: [],
      todos: [],
      taskTrace: { phase: 'unloaded' },
      runStatus: 'idle',
    },
  ],
}

const baseProps = {
  projectSelector: null, onChooseProject: vi.fn(), onOpenConversation: vi.fn(),
  onOpenMemories: vi.fn(), memoriesActive: false, archivedHistory: false, onArchivedHistoryChange: vi.fn(),
  onArchive: vi.fn(), onMove: vi.fn(),
  workspace,
  historyConversations: workspace.conversations,
  historyDayRanges: [7, 30],
  searchOpen: false,
  onOpenSearch: vi.fn(),
  mode: 'expanded' as const,
  overlayOpen: false,
  wideInteractive: true,
  railInteractive: false,
  onToggleMode: vi.fn(),
  onRequestExpanded: vi.fn(),
  onCloseOverlay: vi.fn(),
  onOpenAutomation: vi.fn(),
  onOpenSkills: vi.fn(),
  skillsActive: false,
  automationActive: false,
  onNew: vi.fn(),
  onSelect: vi.fn(),
  onPin: vi.fn(),
  onRename: vi.fn(),
  onDelete: vi.fn(),
  hasMore: true,
  onLoadMore: vi.fn(),
  onRetryLoadMore: vi.fn(),
  user: {
    user_id: 7,
    username: 'yunsan',
    display_name: '云杉',
    avatar_url: null,
    roles: [],
    disabled: false,
  },
  onOpenSettings: vi.fn(),
  onLogout: vi.fn(),
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(new Date('2026-09-28T12:00:00Z'))
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

describe('Sidebar', () => {
  it('renders the authenticated display name and performs real logout', () => {
    const onLogout = vi.fn()
    render(<Sidebar {...baseProps} onLogout={onLogout} />)

    expect(screen.getByText('云杉')).toBeInTheDocument()
    expect(screen.queryByText('Yunsan')).not.toBeInTheDocument()
    expect(screen.queryByText('Pro 工作区')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '打开用户菜单' }))
    fireEvent.click(screen.getByRole('menuitem', { name: '退出登录' }))

    expect(onLogout).toHaveBeenCalledOnce()
  })

  it('用户菜单展开时保持无边框的头像与名称栏，并同步触发器语义', () => {
    render(<Sidebar {...baseProps} />)

    const trigger = screen.getByRole('button', { name: '打开用户菜单' })
    expect(trigger).toHaveAttribute('aria-expanded', 'false')

    fireEvent.click(trigger)

    const expandedTrigger = screen.getByRole('button', { name: '关闭用户菜单' })
    expect(expandedTrigger).toHaveAttribute('aria-expanded', 'true')

  })

  it('从用户菜单打开设置并把稳定的账户按钮作为焦点恢复目标', () => {
    const onOpenSettings = vi.fn()
    render(<Sidebar {...baseProps} onOpenSettings={onOpenSettings} />)
    const accountButton = screen.getByRole('button', { name: '打开用户菜单' })

    fireEvent.click(accountButton)
    fireEvent.click(screen.getByRole('menuitem', { name: '设置' }))

    expect(onOpenSettings).toHaveBeenCalledWith(accountButton)
    expect(screen.queryByRole('menuitem', { name: '退出登录' })).not.toBeInTheDocument()
  })

  it('用户菜单只在容器外部的指针操作后收起', () => {
    render(<Sidebar {...baseProps} />)

    fireEvent.click(screen.getByRole('button', { name: '打开用户菜单' }))
    fireEvent.pointerDown(screen.getByRole('menuitem', { name: '设置' }))
    expect(screen.getByRole('menuitem', { name: '退出登录' })).toBeInTheDocument()

    fireEvent.pointerDown(screen.getByRole('navigation', { name: '工作区功能' }))

    expect(screen.queryByRole('menuitem', { name: '退出登录' })).not.toBeInTheDocument()
    const collapsedTrigger = screen.getByRole('button', { name: '打开用户菜单' })
    expect(collapsedTrigger).toHaveAttribute('aria-expanded', 'false')

  })

  it('用户菜单通过 Escape 收起并把焦点还给触发器', () => {
    render(<Sidebar {...baseProps} />)

    fireEvent.click(screen.getByRole('button', { name: '打开用户菜单' }))
    fireEvent.keyDown(document, { key: 'Escape' })

    const trigger = screen.getByRole('button', { name: '打开用户菜单' })
    expect(screen.queryByRole('menuitem', { name: '退出登录' })).not.toBeInTheDocument()
    expect(trigger).toHaveFocus()
  })

  it('用户菜单进入第一项并支持方向键与 Tab 离开', async () => {
    const user = userEvent.setup()
    render(
      <>
        <Sidebar {...baseProps} />
        <button type="button" aria-label="打开任务抽屉" />
      </>,
    )

    await user.click(screen.getByRole('button', { name: '打开用户菜单' }))
    const settings = screen.getByRole('menuitem', { name: '设置' })
    const logout = screen.getByRole('menuitem', { name: '退出登录' })
    expect(settings).toHaveFocus()

    await user.keyboard('{ArrowDown}')
    expect(logout).toHaveFocus()
    await user.keyboard('{Home}')
    expect(settings).toHaveFocus()
    await user.tab()
    expect(screen.queryByRole('menu', { name: '账户' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '打开任务抽屉' })).toHaveFocus()
  })

  it('falls back to the username when the display name is empty', () => {
    render(<Sidebar {...baseProps} user={{ ...baseProps.user, display_name: '' }} />)

    expect(screen.getByText('yunsan')).toBeInTheDocument()
  })

  it('项目功能独立于会话记录滚动，记忆入口可用', () => {
    const onOpenMemories = vi.fn()
    render(<Sidebar {...baseProps} onOpenMemories={onOpenMemories} />)
    const history = screen.getByRole('region', { name: '最近对话' })
    const navigation = screen.getByRole('navigation', { name: '工作区功能' })
    expect(history).not.toContainElement(navigation)
    expect(within(navigation).getAllByRole('button').map(button => button.textContent)).toEqual(['技能库', '记忆管理', '自动化', '更多'])
    fireEvent.click(within(navigation).getByRole('button', { name: '记忆管理' }))
    expect(onOpenMemories).toHaveBeenCalledOnce()
    expect(within(navigation).getByRole('button', { name: '更多' })).toBeDisabled()
  })

  it('自动化入口可进入并体现当前页，其他入口保持原有状态', () => {
    const onOpenAutomation = vi.fn()
    render(<Sidebar {...baseProps} automationActive onOpenAutomation={onOpenAutomation} />)
    const button = screen.getByRole('button', { name: '自动化' })
    expect(button).toHaveAttribute('aria-current', 'page')
    fireEvent.click(button)
    expect(onOpenAutomation).toHaveBeenCalledOnce()
    expect(screen.queryByRole('button', { name: 'MCP管理' })).not.toBeInTheDocument()
  })

  it('品牌入口创建新会话，搜索在会话记录区提供', () => {
    const onNew = vi.fn()
    render(<Sidebar {...baseProps} onNew={onNew} />)

    const brand = within(screen.getByRole('button', { name: '收起侧边栏' }).closest('.sidebar-head') as HTMLElement)
      .getByRole('button', { name: '新会话' })
    const collapse = screen.getByRole('button', { name: '收起侧边栏' })
    const actions = collapse.closest('.sidebar-head-actions')

    expect(brand).not.toHaveTextContent('Plus')
    fireEvent.click(brand)
    expect(onNew).toHaveBeenCalledOnce()

    expect(within(actions as HTMLElement).getAllByRole('button').map((button) => button.getAttribute('aria-label')))
      .toEqual(['收起侧边栏'])
  })

  it.each(['metaKey', 'ctrlKey'] as const)('会话工具栏与%s加K提供新会话动作', modifier => {
    const onNew = vi.fn()
    render(<Sidebar {...baseProps} onNew={onNew} />)
    const action = screen.getAllByRole('button', { name: '新会话' }).find(button => button.hasAttribute('aria-keyshortcuts'))!
    expect(action).toBeEnabled()
    expect(screen.getByRole('region', { name: '最近对话' })).not.toContainElement(action)
    expect(action).toHaveAttribute('aria-keyshortcuts', 'Meta+K Control+K')
    fireEvent.keyDown(document, { key: 'k', [modifier]: true })
    expect(onNew).toHaveBeenCalledOnce()
  })

  it('功能菜单保留更多入口，新会话在会话工具栏提供', () => {
    render(<Sidebar {...baseProps} />)
    const nav = screen.getByRole('navigation', { name: '工作区功能' })
    expect(within(nav).queryByRole('button', { name: '新会话' })).not.toBeInTheDocument()
    expect(within(nav).getByRole('button', { name: '更多' })).toBeDisabled()
    expect(screen.getAllByRole('button', { name: '新会话' }).find(button => button.hasAttribute('aria-keyshortcuts'))).toBeEnabled()
  })

  it('modal 隔离期间不响应工作区全局快捷键', () => {
    const onNew = vi.fn()
    render(<Sidebar {...baseProps} backgroundInert onNew={onNew} />)

    fireEvent.keyDown(document, { key: 'k', metaKey: true })

    expect(onNew).not.toHaveBeenCalled()
  })

  it('新会话入口始终保持动作按钮语义，不显示选中态', () => {
    const { rerender } = render(<Sidebar {...baseProps} />)

    expect(screen.getAllByRole('button', { name: '新会话' }).find(button => button.hasAttribute('aria-keyshortcuts'))).not.toHaveAttribute('aria-pressed')

    rerender(<Sidebar {...baseProps} workspace={{ ...workspace, currentThreadId: '' }} />)

    expect(screen.getAllByRole('button', { name: '新会话' }).find(button => button.hasAttribute('aria-keyshortcuts'))).not.toHaveAttribute('aria-pressed')
    fireEvent.pointerMove(screen.getAllByRole('button', { name: '新会话' }).find(button => button.hasAttribute('aria-keyshortcuts'))!)
    expect(screen.getByRole('tooltip', { name: '新会话' })).toBeInTheDocument()
  })

  it('按置顶和本地自然日渲染历史分组，不保留固定标题或行内置顶图标', () => {
    const today = new Date().toISOString()
    const conversations = workspace.conversations.map((item) => ({ ...item, updatedAt: today }))
    render(<Sidebar {...baseProps} historyConversations={conversations} />)

    const historyList = screen.getByRole('region', { name: '最近对话' })
    const historyRegion = historyList.parentElement

    expect(historyList).toHaveAttribute('tabindex', '0')

    expect(within(historyList).getAllByRole('button', { name: /^打开会话：/ }).map(button => button.getAttribute('aria-label'))).toEqual(['打开会话：置顶会话', '打开会话：最近会话'])

    expect(screen.queryByText('最近对话')).not.toBeInTheDocument()
    const pinnedHeading = screen.getByRole('heading', { name: '置顶' })

    const todayHeading = screen.getByRole('heading', { name: '今天' })

    expect(screen.queryByText('已置顶')).not.toBeInTheDocument()
    expect(screen.queryByText('最近')).not.toBeInTheDocument()

    const recentButton = screen.getByRole('button', { name: '打开会话：最近会话' })
    expect(recentButton).not.toHaveAttribute('title')

    const groupsContainer = historyList.querySelector('.conversation-groups') as HTMLElement
    Object.defineProperty(groupsContainer, 'offsetTop', { configurable: true, value: 100 })
    Object.defineProperty(pinnedHeading, 'offsetTop', { configurable: true, value: 0 })
    Object.defineProperty(todayHeading, 'offsetTop', { configurable: true, value: 200 })
    historyList.scrollTop = 101
    const stickyTitle = historyRegion?.querySelector('.conversation-sticky-title')

    fireEvent.scroll(historyList)
    expect(stickyTitle).toHaveTextContent('置顶')

    historyList.scrollTop = 301
    fireEvent.scroll(historyList)
    expect(stickyTitle).toHaveTextContent('今天')
  })

  it('审批或 Plan 待处理状态同步可访问名称', () => {
    const waiting = workspace.conversations.map((item): Conversation => item.threadId === 'recent'
      ? { ...item, runStatus: 'waiting_approval' as const }
      : item)
    const { rerender } = render(
      <Sidebar
        {...baseProps}
        workspace={{ ...workspace, conversations: waiting }}
        historyConversations={waiting}
      />,
    )

    expect(screen.getByRole('button', { name: '打开会话：最近会话，等待处理' })).toBeVisible()

    const summarizedPlanWaiting = waiting.map((item): Conversation => item.threadId === 'recent'
      ? { ...item, pendingInteractionKind: 'plan_clarification' }
      : item)
    rerender(
      <Sidebar
        {...baseProps}
        workspace={{ ...workspace, conversations: summarizedPlanWaiting }}
        historyConversations={summarizedPlanWaiting}
      />,
    )
    expect(screen.getByRole('button', { name: '打开会话：最近会话，等待处理' })).toBeVisible()

    for (const pendingInteractionKind of ['tool_approval', 'plan_review'] as const) {
      const summarizedApprovalWaiting = waiting.map((item): Conversation => item.threadId === 'recent'
        ? { ...item, pendingInteractionKind }
        : item)
      rerender(
        <Sidebar
          {...baseProps}
          workspace={{ ...workspace, conversations: summarizedApprovalWaiting }}
          historyConversations={summarizedApprovalWaiting}
        />,
      )
    expect(screen.getByRole('button', { name: '打开会话：最近会话，等待处理' })).toBeVisible()

    }

    const pendingDespiteIdleStatus = workspace.conversations.map((item): Conversation => (
      item.threadId === 'recent'
        ? { ...item, runStatus: 'idle', pendingInteractionKind: 'plan_review' }
        : item
    ))
    rerender(
      <Sidebar
        {...baseProps}
        workspace={{ ...workspace, conversations: pendingDespiteIdleStatus }}
        historyConversations={pendingDespiteIdleStatus}
      />,
    )
    expect(screen.getByRole('button', { name: '打开会话：最近会话，等待处理' })).toBeVisible()

    const planWaiting = waiting.map((item): Conversation => item.threadId === 'recent'
      ? {
          ...item,
          planInteraction: {
            kind: 'questions',
            interruptId: 'plan-question',
            title: '确认范围',
            description: '确认回归范围',
            activeQuestionIndex: 0,
            form: {},
            questions: [{
              id: 'scope',
              answerType: 'text',
              prompt: '回归范围是什么？',
              required: true,
            }],
            submitted: false,
          },
        }
      : item)
    rerender(
      <Sidebar
        {...baseProps}
        workspace={{ ...workspace, conversations: planWaiting }}
        historyConversations={planWaiting}
      />,
    )
    expect(screen.getByRole('button', { name: '打开会话：最近会话，等待处理' })).toBeVisible()

    const reviewWaiting = planWaiting.map((item): Conversation => item.threadId === 'recent'
      ? {
          ...item,
          planInteraction: {
            kind: 'review',
            allowedActions: ['approve', 'reject', 'cancel'],
            interruptId: 'plan-review',
            revision: 1,
            submitted: false,
            draft: {
              revision: 1,
              contentSchema: {
                fingerprint: '0'.repeat(64),
                mediaType: 'text/markdown',
              },
              content: { description: '完成计划草稿并验证', markdown: '# 计划草稿' },
            },
          },
        }
      : item)
    rerender(
      <Sidebar
        {...baseProps}
        workspace={{ ...workspace, conversations: reviewWaiting }}
        historyConversations={reviewWaiting}
      />,
    )
    expect(screen.getByRole('button', { name: '打开会话：最近会话，等待处理' })).toBeVisible()

    rerender(<Sidebar {...baseProps} />)
    expect(screen.getByRole('button', { name: '打开会话：最近会话' })).toBeVisible()
    expect(screen.queryByRole('button', { name: '打开会话：最近会话，等待处理' })).not.toBeInTheDocument()

  })

  it('opens the existing management menu from a pinned conversation', () => {
    render(<Sidebar {...baseProps} />)

    const historyList = screen.getByRole('region', { name: '最近对话' })
    historyList.scrollTop = 48
    fireEvent.click(screen.getByRole('button', { name: '管理会话：置顶会话' }))

    historyList.scrollTop = 96
    fireEvent.scroll(historyList)
    expect(historyList).toHaveProperty('scrollTop', 48)
    expect(screen.getByRole('button', { name: /取消置顶/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /重命名/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /删除/ })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /取消置顶/ }))

    historyList.scrollTop = 96
    fireEvent.scroll(historyList)
    expect(historyList).toHaveProperty('scrollTop', 96)
  })

  it('opens the conversation menu into the keyboard path and restores focus on Escape', async () => {
    const user = userEvent.setup()
    render(<Sidebar {...baseProps} />)
    const trigger = screen.getByRole('button', { name: '管理会话：置顶会话' })
    act(() => trigger.focus())

    await user.keyboard('{Enter}')

    expect(screen.getByRole('button', { name: '取消置顶' })).toHaveFocus()
    await user.tab()
    expect(screen.getByRole('button', { name: '重命名' })).toHaveFocus()
    await user.keyboard('{Escape}')

    expect(screen.queryByRole('button', { name: '重命名' })).not.toBeInTheDocument()
    expect(trigger).toHaveFocus()
  })

  it('分页加载与失败保留当前记录，结束后不显示更多入口', () => {
    const { rerender } = render(<Sidebar {...baseProps} />)
    const scroll = screen.getByRole('region', { name: '最近对话' })
    expect(within(scroll).getByRole('button', { name: '打开会话：最近会话' })).toBeVisible()

    rerender(<Sidebar {...baseProps} isLoadingMore />)

    expect(screen.getByText('正在加载更多历史会话')).toHaveAttribute('role', 'status')
    expect(screen.queryByTestId('history-skeleton')).not.toBeInTheDocument()

    rerender(<Sidebar {...baseProps} loadMoreError="加载历史失败" />)

    expect(screen.getByRole('button', { name: '重试加载历史' })).toBeEnabled()
    expect(within(scroll).getByRole('button', { name: '打开会话：最近会话' })).toBeVisible()

    rerender(<Sidebar {...baseProps} hasMore={false} />)
    expect(screen.queryByRole('button', { name: '重试加载历史' })).not.toBeInTheDocument()
    expect(screen.queryByText('正在加载更多历史会话')).not.toBeInTheDocument()

  })

  it('coalesces repeated bottom scroll events into one request per idle-separated burst', () => {
    vi.useFakeTimers()
    const onLoadMore = vi.fn()
    render(<Sidebar {...baseProps} onLoadMore={onLoadMore} />)
    const scroll = screen.getByRole('region', { name: '最近对话' })
    Object.defineProperties(scroll, {
      scrollTop: { configurable: true, writable: true, value: 100 },
      clientHeight: { configurable: true, value: 200 },
      scrollHeight: { configurable: true, value: 300 },
    })

    fireEvent.scroll(scroll)
    fireEvent.scroll(scroll)
    expect(onLoadMore).toHaveBeenCalledOnce()

    act(() => vi.advanceTimersByTime(121))
    fireEvent.scroll(scroll)
    expect(onLoadMore).toHaveBeenCalledTimes(2)
  })

  it('prevents the sentinel from loading another page during the same scroll burst', () => {
    vi.useFakeTimers()
    let observerCallback: IntersectionObserverCallback | undefined
    let observerRootMargin = ''
    vi.stubGlobal('IntersectionObserver', class {
      constructor(callback: IntersectionObserverCallback, options?: IntersectionObserverInit) {
        observerCallback = callback
        observerRootMargin = options?.rootMargin ?? ''
      }
      observe() {}
      unobserve() {}
      disconnect() {}
      takeRecords() { return [] }
      readonly root = null
      readonly rootMargin = ''
      readonly thresholds = [0]
    })
    const onLoadMore = vi.fn()
    render(<Sidebar {...baseProps} onLoadMore={onLoadMore} />)
    const scroll = screen.getByRole('region', { name: '最近对话' })
    Object.defineProperties(scroll, {
      scrollTop: { configurable: true, writable: true, value: 100 },
      clientHeight: { configurable: true, value: 200 },
      scrollHeight: { configurable: true, value: 300 },
    })

    fireEvent.scroll(scroll)
    act(() => observerCallback?.([{ isIntersecting: true } as IntersectionObserverEntry], {} as IntersectionObserver))
    expect(onLoadMore).toHaveBeenCalledOnce()
    expect(observerRootMargin).toBe('0px 0px 320px')

    act(() => observerCallback?.([{ isIntersecting: false } as IntersectionObserverEntry], {} as IntersectionObserver))
    act(() => vi.advanceTimersByTime(100))
    fireEvent.wheel(scroll, { deltaY: 120 })
    act(() => vi.advanceTimersByTime(100))
    act(() => observerCallback?.([{ isIntersecting: true } as IntersectionObserverEntry], {} as IntersectionObserver))
    expect(onLoadMore).toHaveBeenCalledOnce()

    act(() => observerCallback?.([{ isIntersecting: false } as IntersectionObserverEntry], {} as IntersectionObserver))
    act(() => vi.advanceTimersByTime(121))
    act(() => observerCallback?.([{ isIntersecting: true } as IntersectionObserverEntry], {} as IntersectionObserver))
    expect(onLoadMore).toHaveBeenCalledTimes(2)
  })

  it('restores the first visible conversation offset after a page changes grouping', () => {
    let observerCallback: IntersectionObserverCallback | undefined
    vi.stubGlobal('IntersectionObserver', class {
      constructor(callback: IntersectionObserverCallback) { observerCallback = callback }
      observe() {}
      unobserve() {}
      disconnect() {}
      takeRecords() { return [] }
      readonly root = null
      readonly rootMargin = ''
      readonly thresholds = [0]
    })
    const { rerender } = render(<Sidebar {...baseProps} />)
    const scroll = screen.getByRole('region', { name: '最近对话' })
    let anchorTop = 20
    Object.defineProperties(scroll, {
      scrollTop: { configurable: true, writable: true, value: 100 },
      clientHeight: { configurable: true, value: 200 },
      scrollHeight: { configurable: true, value: 400 },
      getBoundingClientRect: { configurable: true, value: () => ({ top: 0, bottom: 200 }) },
    })
    const anchor = scroll.querySelector<HTMLElement>('[data-history-thread-id="recent"]')
    Object.defineProperty(anchor, 'getBoundingClientRect', {
      configurable: true,
      value: () => ({ top: anchorTop, bottom: anchorTop + 40 }),
    })

    act(() => observerCallback?.([{ isIntersecting: true } as IntersectionObserverEntry], {} as IntersectionObserver))
    scroll.scrollTop = 120
    anchorTop = 0
    fireEvent.scroll(scroll)
    anchorTop = 40
    rerender(<Sidebar {...baseProps} historyConversations={[...workspace.conversations]} />)

    expect(scroll.scrollTop).toBe(160)
  })

  it('renders a failed page with an explicit retry while retaining deliberate-scroll recovery', async () => {
    const user = userEvent.setup()
    const onLoadMore = vi.fn()
    const onRetryLoadMore = vi.fn()
    render(
      <Sidebar
        {...baseProps}
        onLoadMore={onLoadMore}
        loadMoreError="加载历史失败"
        onRetryLoadMore={onRetryLoadMore}
      />,
    )

    const scroll = screen.getByRole('region', { name: '最近对话' })
    Object.defineProperties(scroll, {
      scrollTop: { configurable: true, value: 100 },
      clientHeight: { configurable: true, value: 200 },
      scrollHeight: { configurable: true, value: 300 },
    })
    fireEvent.scroll(scroll)
    expect(onRetryLoadMore).toHaveBeenCalledOnce()
    expect(onLoadMore).not.toHaveBeenCalled()
    expect(screen.queryByText('加载历史失败')).not.toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent('更多历史加载失败')
    await user.click(screen.getByRole('button', { name: '重试加载历史' }))
    expect(onRetryLoadMore).toHaveBeenCalledTimes(2)
  })
})

describe('Sidebar rail and conversation search', () => {
  it('removes a closed mobile overlay from the accessibility tree and focus path', () => {
    render(
      <Sidebar
        {...baseProps}
        mode="overlay"
        overlayOpen={false}
        wideInteractive={false}
        railInteractive={false}
      />,
    )

    const sidebar = screen.getByLabelText('会话导航', { selector: 'aside' })
    expect(sidebar).toHaveAttribute('aria-hidden', 'true')
    expect(sidebar).toHaveAttribute('inert')
    expect(screen.queryByRole('button', { name: '新会话' })).not.toBeInTheDocument()
  })

  it('moves focus into an opened mobile overlay and Escape restores the opener path', async () => {
    const onCloseOverlay = vi.fn()
    render(
      <Sidebar
        {...baseProps}
        mode="overlay"
        overlayOpen
        wideInteractive
        railInteractive={false}
        onCloseOverlay={onCloseOverlay}
      />,
    )

    await waitFor(() => expect(screen.getByRole('button', { name: '关闭导航' })).toHaveFocus())
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onCloseOverlay).toHaveBeenCalledOnce()
  })

  it('Rail的搜索触发器打开弹框而不展开侧栏', async () => {
    const user = userEvent.setup()
    const onRequestExpanded = vi.fn(), onOpenSearch = vi.fn()
    render(
      <Sidebar
        {...baseProps}
        mode="rail"
        wideInteractive={false}
        railInteractive
        onRequestExpanded={onRequestExpanded}
        onOpenSearch={onOpenSearch}
      />,
    )

    expect(screen.getByRole('button', { name: '打开侧边栏' })).toHaveAttribute('aria-expanded', 'false')
    fireEvent.pointerMove(screen.getByRole('button', { name: '打开侧边栏' }))
    expect(screen.getByRole('tooltip', { name: '打开侧边栏' })).toBeInTheDocument()
    fireEvent.pointerLeave(screen.getByRole('button', { name: '打开侧边栏' }))
    fireEvent.pointerMove(screen.getByRole('button', { name: '展开侧边栏以查看账户' }))
    expect(screen.getByRole('tooltip', { name: '账户' })).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'TinkerFin 首页' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '搜索会话' }))
    expect(onRequestExpanded).not.toHaveBeenCalled()
    expect(onOpenSearch).toHaveBeenCalledWith(screen.getByRole('button', { name: '搜索会话' }))
  })

  it('展开侧栏的搜索入口交给宿主处理，并声明模态语义', () => {
    const onOpenSearch = vi.fn()
    const { rerender } = render(<Sidebar {...baseProps} onOpenSearch={onOpenSearch} />)
    const trigger = screen.getByRole('button', { name: '搜索会话' })
    fireEvent.click(trigger)
    expect(onOpenSearch).toHaveBeenCalledWith(trigger)
    expect(trigger).toHaveAttribute('aria-haspopup', 'dialog')
    expect(screen.queryByRole('textbox', { name: '搜索会话' })).not.toBeInTheDocument()
    rerender(<Sidebar {...baseProps} searchOpen onOpenSearch={onOpenSearch} />)
    expect(trigger).toHaveAttribute('aria-expanded', 'true')
  })

  it('Rail 按工作区顺序显示入口、提示和当前页状态', () => {
    const onNew = vi.fn()
    const onOpenSkills = vi.fn()
    const onOpenAutomation = vi.fn()
    render(
      <Sidebar {...baseProps} mode="rail" wideInteractive={false} railInteractive
        skillsActive onNew={onNew} onOpenSkills={onOpenSkills} onOpenAutomation={onOpenAutomation} />,
    )

    const rail = document.querySelector('.sidebar-rail') as HTMLElement
    const labels = ['打开侧边栏', '选择项目', '会话', '技能库', '记忆管理', '自动化', '已归档会话', '搜索会话', '新会话']
    expect(within(rail).getAllByRole('button').slice(0, labels.length).map(button => button.getAttribute('aria-label'))).toEqual(labels)
    for (const label of labels) {
      const button = within(rail).getByRole('button', { name: label })
      fireEvent.pointerMove(button)
      expect(screen.getByRole('tooltip', { name: label })).toBeInTheDocument()
      fireEvent.pointerLeave(button)
    }
    expect(within(rail).getByRole('button', { name: '记忆管理' })).toBeEnabled()
    expect(within(rail).getByRole('button', { name: '技能库' })).toHaveAttribute('aria-current', 'page')

    fireEvent.click(within(rail).getByRole('button', { name: '新会话' }))
    fireEvent.click(within(rail).getByRole('button', { name: '技能库' }))
    fireEvent.click(within(rail).getByRole('button', { name: '自动化' }))
    expect(onNew).toHaveBeenCalledOnce()
    expect(onOpenSkills).toHaveBeenCalledOnce()
    expect(onOpenAutomation).toHaveBeenCalledOnce()
  })

  it('在 Rail 底部复用真实用户头像并保持点击后仅展开侧边栏', () => {
    const onRequestExpanded = vi.fn()
    render(
      <Sidebar
        {...baseProps}
        mode="rail"
        wideInteractive={false}
        railInteractive
        user={{ ...baseProps.user, avatar_url: 'https://cdn.example.test/avatar.webp' }}
        onRequestExpanded={onRequestExpanded}
      />,
    )

    const accountTrigger = screen.getByRole('button', { name: '展开侧边栏以查看账户' })
    expect(accountTrigger.querySelector('img')).toHaveAttribute('src', 'https://cdn.example.test/avatar.webp')

    fireEvent.click(accountTrigger)
    expect(onRequestExpanded).toHaveBeenCalledOnce()
    expect(screen.queryByRole('menuitem', { name: '退出登录' })).not.toBeInTheDocument()
  })

  it('Rail 在空白新会话页显示新会话选中态', () => {
    render(
      <Sidebar
        {...baseProps}
        workspace={{ ...workspace, currentThreadId: '' }}
        mode="rail"
        wideInteractive={false}
        railInteractive
      />,
    )

    expect(screen.getByRole('button', { name: '新会话' })).toHaveAttribute('aria-current', 'page')
    expect(screen.getByRole('button', { name: '新会话' })).toHaveAttribute('aria-pressed', 'true')
  })

})

it('每次新提交只定位一次今天，受理与标题变化不会抢走滚动位置', () => {
  const today = { ...workspace.conversations[1]!, updatedAt: new Date().toISOString(), runStatus: 'streaming' as const }
  const props = { ...baseProps, historyConversations: [workspace.conversations[0]!, today], hasMore: false }
  const view = render(<Sidebar {...props} />)
  const heading = screen.getByRole('heading', { name: '今天' })
  const root = screen.getByRole('region', { name: '最近对话' })
  vi.spyOn(root, 'getBoundingClientRect').mockReturnValue({ top: 20, bottom: 420 } as DOMRect)
  vi.spyOn(heading, 'getBoundingClientRect').mockReturnValue({ top: 180, bottom: 200 } as DOMRect)
  root.scrollTop = 500
  view.rerender(<Sidebar {...props} newSubmission="one" />)
  expect(root.scrollTop).toBe(660)
  root.scrollTop = 900
  view.rerender(<Sidebar {...props} newSubmission="one" historyConversations={[{ ...today, title: '总结标题' }]} />)
  expect(root.scrollTop).toBe(900)
  expect(screen.getByRole('button', { name: '打开会话：总结标题，正在生成' })).toHaveAttribute('aria-busy', 'true')
  view.rerender(<Sidebar {...props} newSubmission="one" historyConversations={[{ ...today, title: '总结标题', runStatus: 'idle' }]} />)
  expect(screen.getByRole('button', { name: '打开会话：总结标题' })).not.toHaveAttribute('aria-busy')
})
