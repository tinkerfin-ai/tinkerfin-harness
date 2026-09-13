import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { Conversation, WorkspaceState } from '../../../types'
import { Sidebar } from './Sidebar'

const workspace: WorkspaceState = {
  currentThreadId: 'recent',
  conversations: [
    { accessMode: 'write_approval',
      threadId: 'pinned',
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
    { accessMode: 'write_approval',
      threadId: 'recent',
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
  workspace,
  historyConversations: workspace.conversations,
  historyDayRanges: [7, 30],
  historyQuery: '',
  onHistoryQueryChange: vi.fn(),
  isHistorySearchActive: false,
  isHistorySearching: false,
  mode: 'expanded' as const,
  settledMode: 'expanded' as const,
  overlayOpen: false,
  wideInteractive: true,
  railInteractive: false,
  onToggleMode: vi.fn(),
  onRequestExpanded: vi.fn(),
  onCloseOverlay: vi.fn(),
  onOpenAutomation: vi.fn(),
  automationActive: false,
  onNew: vi.fn(),
  onSelect: vi.fn(),
  onPin: vi.fn(),
  onRename: vi.fn(),
  onDelete: vi.fn(),
  hasMore: true,
  onLoadMore: vi.fn(),
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
    expect(trigger.parentElement).toHaveClass('user-account')
    expect(trigger).toHaveClass('user-card')
    expect(trigger.querySelector('.lucide-chevron-down')).not.toBeInTheDocument()

    fireEvent.click(trigger)

    const expandedTrigger = screen.getByRole('button', { name: '关闭用户菜单' })
    expect(expandedTrigger).toHaveAttribute('aria-expanded', 'true')
    expect(expandedTrigger.querySelector('.lucide-chevron-up')).not.toBeInTheDocument()
    expect(expandedTrigger.querySelector('.lucide-chevron-down')).not.toBeInTheDocument()
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
    expect(collapsedTrigger.querySelector('.lucide-chevron-down')).not.toBeInTheDocument()
    expect(collapsedTrigger.querySelector('.lucide-chevron-up')).not.toBeInTheDocument()
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

  it('仅渲染当前可用的产品导航，并禁用尚不可用的入口', () => {
    render(<Sidebar {...baseProps} />)

    const historyScroll = screen.getByRole('region', { name: '最近对话' })
    const productNavigation = screen.getByRole('navigation', { name: '工作区功能' })
    expect(historyScroll).toContainElement(productNavigation)
    expect(screen.getByRole('button', { name: '技能库' }).querySelector('.lucide-book-open-check')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '智能体' }).querySelector('.lucide-workflow')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '自动化' }).querySelector('.lucide-alarm-clock')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '自动化' })).toBeEnabled()
    const agentButton = screen.getByRole('button', { name: '智能体' })
    expect(agentButton).not.toHaveAttribute('aria-current')
    expect(agentButton).not.toHaveAttribute('aria-pressed')
    expect(agentButton).not.toHaveClass('is-selected')
    expect(agentButton).toBeDisabled()
    expect(screen.queryByRole('button', { name: '工作区' })).not.toBeInTheDocument()
    for (const label of ['技能库', '更多']) {
      expect(screen.getByRole('button', { name: label })).toBeDisabled()
    }

    fireEvent.click(screen.getByRole('button', { name: '打开用户菜单' }))
    expect(screen.getByRole('menuitem', { name: '设置' })).toBeEnabled()
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

  it('在展开侧栏中把搜索放到收起控件左侧并使用任务抽屉图标', () => {
    render(<Sidebar {...baseProps} />)

    const brand = screen.getByRole('link', { name: 'TinkerFin 首页' })
    const collapse = screen.getByRole('button', { name: '收起侧边栏' })
    const actions = collapse.closest('.sidebar-head-actions')
    expect(brand.querySelector('.brand-logo')).toHaveClass('brand-logo--md')
    expect(brand.querySelector('.brand-logo__mark')).toHaveAttribute('alt', '')
    expect(brand.querySelector('.brand-logo__wordmark')).toHaveAttribute('alt', '')
    expect(brand).not.toHaveTextContent('Plus')
    expect(collapse.querySelector('.lucide-panel-right')).toBeInTheDocument()
    expect(within(actions as HTMLElement).getAllByRole('button').map((button) => button.getAttribute('aria-label')))
      .toEqual(['搜索会话', '收起侧边栏'])
  })

  it('只固定居中的新会话胶囊，并提供可用的 Command K 快捷键', () => {
    const onNew = vi.fn()
    render(<Sidebar {...baseProps} onNew={onNew} />)

    const newChat = screen.getByRole('button', { name: '新会话' })
    const historyScroll = screen.getByRole('region', { name: '最近对话' })
    expect(newChat.parentElement).toHaveClass('new-chat-wrap')
    expect(historyScroll).not.toContainElement(newChat)
    expect(newChat).toHaveAttribute('aria-keyshortcuts', 'Meta+K')
    expect(newChat).not.toHaveAttribute('title')
    expect(newChat.querySelector('.new-chat-shortcut')).toHaveTextContent('⌘ K')

    fireEvent.keyDown(document, { key: 'k', metaKey: true })

    expect(onNew).toHaveBeenCalledOnce()
  })

  it('modal 隔离期间不响应工作区全局快捷键', () => {
    const onNew = vi.fn()
    render(<Sidebar {...baseProps} backgroundInert onNew={onNew} />)

    fireEvent.keyDown(document, { key: 'k', metaKey: true })

    expect(onNew).not.toHaveBeenCalled()
  })

  it('新会话入口始终保持动作按钮语义，不显示选中态', () => {
    const { rerender } = render(<Sidebar {...baseProps} />)

    expect(screen.getByRole('button', { name: '新会话' })).not.toHaveClass('is-selected')
    expect(screen.getByRole('button', { name: '新会话' })).not.toHaveAttribute('aria-pressed')

    rerender(<Sidebar {...baseProps} workspace={{ ...workspace, currentThreadId: '' }} />)

    expect(screen.getByRole('button', { name: '新会话' })).not.toHaveClass('is-selected')
    expect(screen.getByRole('button', { name: '新会话' })).not.toHaveAttribute('aria-pressed')
    expect(screen.queryByRole('tooltip', { name: '新会话' })).not.toBeInTheDocument()
  })

  it('按置顶和本地自然日渲染历史分组，不保留固定标题或行内置顶图标', () => {
    const today = new Date().toISOString()
    const conversations = workspace.conversations.map((item) => ({ ...item, updatedAt: today }))
    render(<Sidebar {...baseProps} historyConversations={conversations} />)

    const historyList = screen.getByRole('region', { name: '最近对话' })
    const historyRegion = historyList.parentElement
    expect(historyList).toHaveClass('ui-scrollbar')
    expect(historyList).toHaveAttribute('tabindex', '0')
    expect(historyRegion?.querySelector('.ui-scrollbar-overlay')).not.toBeInTheDocument()
    expect(historyRegion?.querySelector('.ui-overlay-scrollbar')).toHaveAttribute('data-visibility', 'transient')
    const pinnedItem = screen.getByRole('button', { name: '打开会话：置顶会话' }).closest('.conversation-item')
    expect(historyRegion).toHaveClass('conversation-history')
    expect(screen.queryByText('最近对话')).not.toBeInTheDocument()
    const pinnedHeading = screen.getByRole('heading', { name: '置顶' })
    expect(pinnedHeading).toHaveClass('conversation-group-title')
    expect(pinnedHeading.parentElement).toHaveClass('conversation-groups')
    expect(pinnedHeading.nextElementSibling).toHaveClass('conversation-group-items')
    const todayHeading = screen.getByRole('heading', { name: '今天' })
    expect(todayHeading).toHaveClass('conversation-group-title')
    expect(screen.queryByText('已置顶')).not.toBeInTheDocument()
    expect(screen.queryByText('最近')).not.toBeInTheDocument()
    expect(pinnedItem).toHaveClass('is-pinned')
    expect(pinnedItem?.querySelector('.conversation-pinned-indicator')).not.toBeInTheDocument()
    const recentButton = screen.getByRole('button', { name: '打开会话：最近会话' })
    expect(recentButton).not.toHaveAttribute('title')
    expect(recentButton.closest('.conversation-item')).toHaveClass('is-recent')
    expect(recentButton.closest('.conversation-item')).toHaveClass('overflow-marquee-trigger')
    expect(recentButton.querySelector('.conversation-title-marquee')).toHaveClass('overflow-marquee')
    expect(historyList.querySelector('.conversation-item')).toBe(pinnedItem)

    const groupsContainer = historyList.querySelector('.conversation-groups') as HTMLElement
    Object.defineProperty(groupsContainer, 'offsetTop', { configurable: true, value: 100 })
    Object.defineProperty(pinnedHeading, 'offsetTop', { configurable: true, value: 0 })
    Object.defineProperty(todayHeading, 'offsetTop', { configurable: true, value: 200 })
    historyList.scrollTop = 101
    const stickyTitle = historyRegion?.querySelector('.conversation-sticky-title')

    fireEvent.scroll(historyList)
    expect(stickyTitle).toHaveTextContent('置顶')
    expect(stickyTitle).toHaveClass('is-visible')

    historyList.scrollTop = 301
    fireEvent.scroll(historyList)
    expect(stickyTitle).toHaveTextContent('今天')
  })

  it('按审批或 Plan 状态绑定待处理颜色并同步可访问名称', () => {
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

    const waitingButton = screen.getByRole('button', { name: '打开会话：最近会话，等待处理' })
    expect(waitingButton.querySelector('.conversation-status-slot')).toBeInTheDocument()
    expect(waitingButton.querySelector('.conversation-attention-dot')).toHaveClass('is-approval')
    expect(screen.getByRole('button', { name: '打开会话：置顶会话' }).querySelector('.conversation-attention-dot')).toBeNull()

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
    expect(screen.getByRole('button', { name: '打开会话：最近会话，等待处理' })
      .querySelector('.conversation-attention-dot')).toHaveClass('is-plan')

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
      expect(screen.getByRole('button', { name: '打开会话：最近会话，等待处理' })
        .querySelector('.conversation-attention-dot')).toHaveClass('is-approval')
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
    expect(screen.getByRole('button', { name: '打开会话：最近会话，等待处理' })
      .querySelector('.conversation-attention-dot')).toHaveClass('is-approval')

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
    expect(screen.getByRole('button', { name: '打开会话：最近会话，等待处理' })
      .querySelector('.conversation-attention-dot')).toHaveClass('is-plan')

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
    expect(screen.getByRole('button', { name: '打开会话：最近会话，等待处理' })
      .querySelector('.conversation-attention-dot')).toHaveClass('is-approval')

    rerender(<Sidebar {...baseProps} />)
    expect(screen.getByRole('button', { name: '打开会话：最近会话' }).querySelector('.conversation-attention-dot')).toBeNull()
  })

  it('opens the existing management menu from a pinned conversation', () => {
    render(<Sidebar {...baseProps} />)

    const historyList = screen.getByRole('region', { name: '最近对话' })
    historyList.scrollTop = 48
    fireEvent.click(screen.getByRole('button', { name: '管理会话：置顶会话' }))

    expect(historyList).toHaveClass('is-scroll-locked')
    historyList.scrollTop = 96
    fireEvent.scroll(historyList)
    expect(historyList).toHaveProperty('scrollTop', 48)
    expect(screen.getByRole('button', { name: /取消置顶/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /重命名/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /删除/ })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /取消置顶/ }))
    expect(historyList).not.toHaveClass('is-scroll-locked')
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

  it('reserves one stable pagination slot while loading and error content changes', () => {
    const { rerender } = render(<Sidebar {...baseProps} />)
    const scroll = screen.getByRole('region', { name: '最近对话' })
    const slot = scroll.querySelector('.history-pagination-slot')
    expect(slot).toBeInTheDocument()
    expect(slot).toBeEmptyDOMElement()

    rerender(<Sidebar {...baseProps} isLoadingMore />)
    expect(scroll.querySelector('.history-pagination-slot')).toBe(slot)
    expect(screen.getByText('正在加载更多历史会话')).toHaveAttribute('role', 'status')
    expect(screen.queryByTestId('history-skeleton')).not.toBeInTheDocument()
    expect(scroll.querySelector('.history-load-sentinel')).toBeInTheDocument()

    rerender(<Sidebar {...baseProps} loadMoreError="加载历史失败" />)
    expect(scroll.querySelector('.history-pagination-slot')).toBe(slot)
    expect(screen.queryByText('加载历史失败')).not.toBeInTheDocument()

    rerender(<Sidebar {...baseProps} hasMore={false} />)
    expect(scroll.querySelector('.history-pagination-slot')).not.toBeInTheDocument()
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
    await user.click(screen.getByRole('button', { name: '重试加载历史' }))
    expect(onRetryLoadMore).toHaveBeenCalledTimes(2)
  })
})

describe('Sidebar rail and inline search', () => {
  it('removes a closed mobile overlay from the accessibility tree and focus path', () => {
    render(
      <Sidebar
        {...baseProps}
        mode="overlay"
        settledMode="overlay"
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
        settledMode="overlay"
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

  it('keeps only rail controls accessible and expands search with one action', async () => {
    const user = userEvent.setup()
    const onRequestExpanded = vi.fn()
    render(
      <Sidebar
        {...baseProps}
        mode="rail"
        settledMode="rail"
        wideInteractive={false}
        railInteractive
        onRequestExpanded={onRequestExpanded}
      />,
    )

    expect(screen.getByRole('button', { name: '打开侧边栏' })).toHaveAttribute('aria-expanded', 'false')
    expect(screen.getByRole('tooltip', { name: '打开侧边栏' })).toBeInTheDocument()
    expect(screen.getByRole('tooltip', { name: '账户' })).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'TinkerFin 首页' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '搜索会话' }))
    expect(onRequestExpanded).toHaveBeenCalledOnce()
  })

  it('把查询交给后端状态并让 Escape 清空、收起和恢复焦点', async () => {
    const user = userEvent.setup()
    const onHistoryQueryChange = vi.fn()
    const { rerender } = render(
      <Sidebar {...baseProps} onHistoryQueryChange={onHistoryQueryChange} />,
    )

    const trigger = screen.getByRole('button', { name: '搜索会话' })
    await user.click(trigger)
    const input = screen.getByRole('textbox', { name: '搜索会话' })
    expect(trigger).toHaveAttribute('aria-expanded', 'true')

    fireEvent.pointerDown(document.body)
    expect(trigger).toHaveAttribute('aria-expanded', 'false')
    await user.click(trigger)
    fireEvent.change(input, { target: { value: '置顶' } })
    expect(onHistoryQueryChange).toHaveBeenLastCalledWith('置顶')
    rerender(
      <Sidebar
        {...baseProps}
        historyConversations={[workspace.conversations[0]!]}
        historyQuery="置顶"
        onHistoryQueryChange={onHistoryQueryChange}
        isHistorySearchActive
      />,
    )
    expect(screen.getByRole('button', { name: '打开会话：置顶会话' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '打开会话：最近会话' })).not.toBeInTheDocument()

    fireEvent.pointerDown(document.body)
    expect(trigger).toHaveAttribute('aria-expanded', 'true')
    expect(input).not.toHaveFocus()
    await user.click(input)

    await user.keyboard('{Escape}')
    expect(trigger).toHaveAttribute('aria-expanded', 'false')
    expect(onHistoryQueryChange).toHaveBeenLastCalledWith('')
    await waitFor(() => expect(trigger).toHaveFocus())
    rerender(<Sidebar {...baseProps} onHistoryQueryChange={onHistoryQueryChange} />)
    expect(screen.getByRole('button', { name: '打开会话：最近会话' })).toBeInTheDocument()

    await user.click(trigger)
    rerender(
      <Sidebar
        {...baseProps}
        historyQuery="置顶"
        onHistoryQueryChange={onHistoryQueryChange}
        isHistorySearchActive
      />,
    )
    await user.click(screen.getByRole('button', { name: '清除搜索' }))
    expect(trigger).toHaveAttribute('aria-expanded', 'false')
    expect(onHistoryQueryChange).toHaveBeenLastCalledWith('')
  })

  it('preserves a non-empty query across rail and expanded presentation changes', async () => {
    const user = userEvent.setup()
    const onHistoryQueryChange = vi.fn()
    const { rerender } = render(
      <Sidebar {...baseProps} onHistoryQueryChange={onHistoryQueryChange} />,
    )
    await user.click(screen.getByRole('button', { name: '搜索会话' }))
    fireEvent.change(screen.getByRole('textbox', { name: '搜索会话' }), { target: { value: '置顶' } })

    rerender(
      <Sidebar
        {...baseProps}
        historyQuery="置顶"
        onHistoryQueryChange={onHistoryQueryChange}
        isHistorySearchActive
        mode="rail"
        settledMode="rail"
        wideInteractive={false}
        railInteractive
      />,
    )
    expect(screen.getByRole('button', { name: '搜索会话，当前查询：置顶' })).toBeInTheDocument()

    rerender(
      <Sidebar
        {...baseProps}
        historyConversations={[workspace.conversations[0]!]}
        historyQuery="置顶"
        onHistoryQueryChange={onHistoryQueryChange}
        isHistorySearchActive
      />,
    )
    expect(screen.getByRole('textbox', { name: '搜索会话' })).toHaveValue('置顶')
    expect(screen.queryByRole('button', { name: '打开会话：最近会话' })).not.toBeInTheDocument()
  })

  it('在 Rail 导航中也禁用智能体入口', () => {
    render(
      <Sidebar {...baseProps} mode="rail" settledMode="rail" wideInteractive={false} railInteractive />,
    )

    expect(screen.getByRole('button', { name: '智能体' })).toBeDisabled()
  })

  it('在 Rail 底部复用真实用户头像并保持点击后仅展开侧边栏', () => {
    const onRequestExpanded = vi.fn()
    render(
      <Sidebar
        {...baseProps}
        mode="rail"
        settledMode="rail"
        wideInteractive={false}
        railInteractive
        user={{ ...baseProps.user, avatar_url: 'https://cdn.example.test/avatar.webp' }}
        onRequestExpanded={onRequestExpanded}
      />,
    )

    const accountTrigger = screen.getByRole('button', { name: '展开侧边栏以查看账户' })
    expect(accountTrigger.querySelector('img')).toHaveAttribute('src', 'https://cdn.example.test/avatar.webp')
    expect(accountTrigger.querySelector('.user-avatar')).toBeInTheDocument()
    expect(accountTrigger.querySelector('.lucide-user-round')).not.toBeInTheDocument()

    fireEvent.click(accountTrigger)
    expect(onRequestExpanded).toHaveBeenCalledOnce()
    expect(screen.queryByRole('menuitem', { name: '退出登录' })).not.toBeInTheDocument()
  })

  it('Rail 新会话入口也保持无选中态', () => {
    render(
      <Sidebar
        {...baseProps}
        workspace={{ ...workspace, currentThreadId: '' }}
        mode="rail"
        settledMode="rail"
        wideInteractive={false}
        railInteractive
      />,
    )

    expect(screen.getByRole('button', { name: '新会话' })).not.toHaveClass('is-selected')
    expect(screen.getByRole('button', { name: '新会话' })).not.toHaveAttribute('aria-pressed')
  })

  it('focuses the input only after a rail expansion settles', async () => {
    const user = userEvent.setup()
    const { rerender } = render(
      <Sidebar {...baseProps} mode="rail" settledMode="rail" wideInteractive={false} railInteractive />,
    )
    await user.click(screen.getByRole('button', { name: '搜索会话' }))

    rerender(
      <Sidebar {...baseProps} mode="expanded" settledMode="rail" wideInteractive railInteractive={false} />,
    )
    const input = screen.getByRole('textbox', { name: '搜索会话' })
    expect(input).not.toHaveFocus()

    rerender(<Sidebar {...baseProps} />)
    await waitFor(() => expect(input).toHaveFocus())
  })
})
