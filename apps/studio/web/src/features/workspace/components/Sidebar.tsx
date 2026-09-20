import {
  BookOpenCheck,
  AlarmClock,
  BrainCircuit,
  CircleEllipsis,
  Ellipsis,
  LogOut,
  PanelRight,
  Pencil,
  Pin,
  PinOff,
  Search,
  Settings2,
  SquarePen,
  Trash2,
  X,
} from 'lucide-react'
import {
  Fragment,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import type { KeyboardEvent as ReactKeyboardEvent, MouseEvent } from 'react'

import type { AuthUser } from '../../../api/auth/types'
import { BrandLogo, Button, IconButton, OverlayScrollbar, UserAvatar } from '../../../components/ui'
import { useI18n } from '../../../i18n'
import { isConversationRunning } from '../../../lib/workspace'
import type { Conversation, WorkspaceState } from '../../../types'
import { groupConversationHistory } from '../historyGroups'
import type { SidebarMode } from '../useWorkspaceNavigation'
import type { PendingConversation } from '../usePendingConversations'
import { OverflowMarquee } from './OverflowMarquee'

// 提前一段可滚动距离发起分页，让 300ms 防刷等待尽量落在用户持续浏览期间
const HISTORY_PAGE_PRELOAD_DISTANCE_PX = 320
// 连续 wheel 通常逐帧到达，短静默即可区分下一次独立滑动
const HISTORY_SCROLL_BURST_IDLE_MS = 120

export function ConversationItem({
  conversation,
  isActive,
  isMenuOpen,
  onSelect,
  onToggleMenu,
  onRemove,
  pendingRunId,
}: {
  conversation: Conversation
  isActive: boolean
  isMenuOpen: boolean
  onSelect: () => void
  pendingRunId?: string
  onRemove?: () => void
  onToggleMenu?: (event: MouseEvent<HTMLButtonElement>) => void
}) {
  const { t } = useI18n()
  const requiresAttention = conversation.runStatus === 'waiting_approval'
    || conversation.pendingInteractionKind != null
    || Boolean(conversation.approval && !conversation.approval.submitted)
    || Boolean(conversation.planInteraction && !conversation.planInteraction.submitted)
  // 已水化交互优先，历史摘要保证首次渲染使用同一业务类型
  const attentionTone = conversation.planInteraction && !conversation.planInteraction.submitted
    ? conversation.planInteraction.kind === 'review' ? 'approval' : 'plan'
    : conversation.approval && !conversation.approval.submitted
      ? 'approval'
      : conversation.pendingInteractionKind === 'plan_clarification'
        ? 'plan'
        : 'approval'
  const generating = !requiresAttention && isConversationRunning(conversation)
  const openLabel = t('打开会话：{title}', { title: conversation.title })
  return (
    <div
      className={`conversation-item overflow-marquee-trigger ${conversation.pinned ? 'is-pinned' : 'is-recent'} ${isActive ? 'is-active' : ''}`}
      data-history-thread-id={pendingRunId ? `pending:${pendingRunId}` : conversation.threadId}
    >
      <button
        type="button"
        className="conversation-main"
        aria-label={requiresAttention ? `${openLabel}，${t('等待处理')}` : generating ? `${openLabel}，${t('正在生成')}` : openLabel}
        aria-busy={generating || undefined}
        onClick={onSelect}
      >
        <span className="conversation-status-slot" aria-hidden="true">
          {generating && <span className="conversation-loading"><i /><i /><i /><i /></span>}
          {requiresAttention && <span className={`conversation-attention-dot is-${attentionTone}`} />}
        </span>
        <OverflowMarquee className="conversation-title-marquee" endRevealInset={12}>{`${conversation.title}\u200b`}</OverflowMarquee>
      </button>
      {onRemove && <IconButton className="conversation-more" size="sm"
        label={t('移除未发送会话：{title}', { title: conversation.title })}
        icon={<Trash2 size={16} />} onClick={onRemove} />}
      {onToggleMenu && (
        <IconButton
          className="conversation-more"
          size="sm"
          label={t('管理会话：{title}', { title: conversation.title })}
          icon={<Ellipsis size={16} />}
          selected={isMenuOpen}
          aria-expanded={isMenuOpen}
          onClick={onToggleMenu}
        />
      )}
    </div>
  )
}

const NO_PENDING_CONVERSATIONS: PendingConversation[] = []

export interface SidebarProps {
  pendingConversations?: PendingConversation[]
  selectedPendingRunId?: string | null
  onSelectPending?: (runId: string) => void
  onRemovePending?: (runId: string) => void
  newSubmission?: string | null
  workspace: WorkspaceState
  historyConversations: Conversation[]
  historyDayRanges: number[]
  historyQuery: string
  onHistoryQueryChange: (query: string) => void
  isHistorySearchActive: boolean
  isHistorySearching: boolean
  mode: SidebarMode
  settledMode: SidebarMode
  overlayOpen: boolean
  wideInteractive: boolean
  railInteractive: boolean
  onToggleMode: () => void
  onRequestExpanded: () => void
  onCloseOverlay: (restoreFocus?: boolean) => void
  onOpenAutomation: () => void
  automationActive: boolean
  onNew: () => void
  onSelect: (threadId: string) => void
  onPin: (threadId: string) => void
  pinPendingThreadIds?: ReadonlySet<string>
  onRename: (threadId: string, restoreFocusTo?: HTMLElement | null) => void
  onDelete: (threadId: string, restoreFocusTo?: HTMLElement | null) => void
  hasMore: boolean
  onLoadMore: () => void
  isLoadingMore?: boolean
  loadMoreError?: string
  onRetryLoadMore?: () => void
  user: AuthUser
  onOpenSettings: (restoreFocusTo?: HTMLElement | null) => void
  onLogout: () => void
  backgroundInert?: boolean
}

export function Sidebar({
  pendingConversations = NO_PENDING_CONVERSATIONS,
  selectedPendingRunId,
  onSelectPending,
  onRemovePending,
  newSubmission,
  workspace,
  historyConversations,
  historyDayRanges,
  historyQuery,
  onHistoryQueryChange,
  isHistorySearchActive,
  isHistorySearching,
  mode,
  settledMode,
  overlayOpen,
  wideInteractive,
  railInteractive,
  onToggleMode,
  onRequestExpanded,
  onCloseOverlay,
  onOpenAutomation,
  automationActive,
  onNew,
  onSelect,
  onPin,
  pinPendingThreadIds = new Set(),
  onRename,
  onDelete,
  hasMore,
  onLoadMore,
  isLoadingMore = false,
  loadMoreError,
  onRetryLoadMore,
  user,
  onOpenSettings,
  onLogout,
  backgroundInert = false,
}: SidebarProps) {
  const { locale, t } = useI18n()
  const locatedSubmission = useRef<string | null>(null)
  const programmaticScroll = useRef(false)
  const [isSearchOpen, setSearchOpen] = useState(false)
  const [userMenu, setUserMenu] = useState(false)
  const [stickyHistoryTitle, setStickyHistoryTitle] = useState('')
  const [openMenu, setOpenMenu] = useState<{
    threadId: string
    top: number
    left: number
    trigger: HTMLButtonElement
  } | null>(null)
  const rootRef = useRef<HTMLElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const userMenuRef = useRef<HTMLDivElement>(null)
  const userMenuPopupRef = useRef<HTMLDivElement>(null)
  const userMenuButtonRef = useRef<HTMLButtonElement>(null)
  const historyScrollRef = useRef<HTMLDivElement>(null)
  const searchInputRef = useRef<HTMLInputElement>(null)
  const searchTriggerRef = useRef<HTMLButtonElement>(null)
  const overlayCloseButtonRef = useRef<HTMLButtonElement>(null)
  const pendingSearchFocusRef = useRef(false)
  const lockedHistoryScrollTop = useRef(0)
  const historyLoadSentinelRef = useRef<HTMLDivElement>(null)
  const paginationArmedRef = useRef(true)
  const paginationRequestedInScrollBurstRef = useRef(false)
  const paginationScrollBurstTimerRef = useRef<number | null>(null)
  const paginationAnchorRef = useRef<{
    threadId?: string
    offset: number
    scrollTop: number
  } | null>(null)
  const paginationStateRef = useRef({
    hasMore,
    isLoadingMore,
    loadMoreError,
    onLoadMore,
    onRetryLoadMore,
  })
  paginationStateRef.current = {
    hasMore,
    isLoadingMore,
    loadMoreError,
    onLoadMore,
    onRetryLoadMore,
  }
  const groups = useMemo(
    () => groupConversationHistory([...historyConversations, ...pendingConversations.filter(item => !isHistorySearchActive || item.conversation.title.toLocaleLowerCase().includes(historyQuery.toLocaleLowerCase())).map(item => item.conversation)], historyDayRanges, new Date(), locale),
    [historyConversations, pendingConversations, historyDayRanges, locale, isHistorySearchActive, historyQuery],
  )
  const menuConversation = openMenu
    ? workspace.conversations.find((item) => item.threadId === openMenu.threadId)
    : undefined
  const isOverlayHidden = mode === 'overlay' && !overlayOpen
  const capturePaginationAnchor = useCallback(() => {
    const root = historyScrollRef.current
    if (!root) return
    const rootBounds = root.getBoundingClientRect()
    const firstVisible = Array.from(
      root.querySelectorAll<HTMLElement>('[data-history-thread-id]'),
    ).find((item) => {
      const bounds = item.getBoundingClientRect()
      return bounds.bottom > rootBounds.top && bounds.top < rootBounds.bottom
    })
    paginationAnchorRef.current = {
      threadId: firstVisible?.dataset.historyThreadId,
      offset: firstVisible
        ? firstVisible.getBoundingClientRect().top - rootBounds.top
        : 0,
      scrollTop: root.scrollTop,
    }
  }, [])

  const requestHistoryPage = useCallback((allowRetry = false) => {
    const state = paginationStateRef.current
    if (
      !paginationArmedRef.current
      || !state.hasMore
      || state.isLoadingMore
      || (state.loadMoreError && !allowRetry)
    ) return
    paginationArmedRef.current = false
    capturePaginationAnchor()
    const load = state.loadMoreError
      ? state.onRetryLoadMore ?? state.onLoadMore
      : state.onLoadMore
    load()
  }, [capturePaginationAnchor])

  const sustainPaginationScrollBurst = useCallback(() => {
    if (paginationScrollBurstTimerRef.current != null) {
      window.clearTimeout(paginationScrollBurstTimerRef.current)
    }
    paginationScrollBurstTimerRef.current = window.setTimeout(() => {
      paginationRequestedInScrollBurstRef.current = false
      paginationScrollBurstTimerRef.current = null
    }, HISTORY_SCROLL_BURST_IDLE_MS)
  }, [])

  const requestHistoryPageFromViewport = useCallback((element: HTMLElement) => {
    const nearBottom = (
      element.scrollTop + element.clientHeight
      >= element.scrollHeight - HISTORY_PAGE_PRELOAD_DISTANCE_PX
    )
    if (!nearBottom) {
      paginationArmedRef.current = true
      return
    }
    if (paginationRequestedInScrollBurstRef.current) return
    paginationRequestedInScrollBurstRef.current = true
    paginationArmedRef.current = true
    requestHistoryPage(true)
  }, [requestHistoryPage])

  const updateStickyHistoryTitle = useCallback((scrollElement: HTMLElement) => {
    const groupsElement = scrollElement.querySelector<HTMLElement>('.conversation-groups')
    const groupsOffset = groupsElement?.offsetTop ?? 0
    const scrollPosition = scrollElement.scrollTop + 1
    let activeTitle = ''
    for (const heading of scrollElement.querySelectorAll<HTMLElement>('[data-history-group-label]')) {
      if (groupsOffset + heading.offsetTop > scrollPosition) break
      activeTitle = heading.dataset.historyGroupLabel ?? ''
    }
    setStickyHistoryTitle((current) => current === activeTitle ? current : activeTitle)
  }, [])

  const closeMenu = (restoreFocus = false) => {
    if (restoreFocus) openMenu?.trigger.focus()
    setOpenMenu(null)
  }

  const toggleMenu = (conversation: Conversation, anchor: HTMLButtonElement) => {
    const rect = anchor.getBoundingClientRect()
    setOpenMenu((current) => {
      if (current?.threadId === conversation.threadId) return null
      lockedHistoryScrollTop.current = historyScrollRef.current?.scrollTop ?? 0
      return {
        threadId: conversation.threadId,
        top: Math.min(rect.bottom + 4, window.innerHeight - 164),
        left: Math.max(8, rect.right - 144),
        trigger: anchor,
      }
    })
  }

  const focusSearch = () => {
    window.requestAnimationFrame(() => searchInputRef.current?.focus())
  }

  const openSearch = (fromRail = false) => {
    setSearchOpen(true)
    if (fromRail) {
      pendingSearchFocusRef.current = true
      onRequestExpanded()
    } else {
      focusSearch()
    }
  }

  const clearAndCloseSearch = (restoreFocus = true) => {
    onHistoryQueryChange('')
    setSearchOpen(false)
    pendingSearchFocusRef.current = false
    if (restoreFocus) window.requestAnimationFrame(() => searchTriggerRef.current?.focus())
  }

  useEffect(() => {
    if (
      pendingSearchFocusRef.current
      && isSearchOpen
      && settledMode === 'expanded'
    ) {
      pendingSearchFocusRef.current = false
      focusSearch()
    }
  }, [isSearchOpen, settledMode])

  useEffect(() => {
    if (!isSearchOpen) return
    const handleOutsidePointerDown = (event: PointerEvent) => {
      const target = event.target
      if (target instanceof Node && rootRef.current?.contains(target)) return
      if (historyQuery.trim()) {
        searchInputRef.current?.blur()
      } else {
        setSearchOpen(false)
      }
    }
    document.addEventListener('pointerdown', handleOutsidePointerDown)
    return () => document.removeEventListener('pointerdown', handleOutsidePointerDown)
  }, [historyQuery, isSearchOpen])

  useEffect(() => {
    if (wideInteractive) return
    setOpenMenu(null)
    setUserMenu(false)
  }, [wideInteractive])

  useEffect(() => {
    if (mode !== 'overlay' || !overlayOpen || !wideInteractive) return
    window.requestAnimationFrame(() => overlayCloseButtonRef.current?.focus())
    const handleEscape = (event: KeyboardEvent) => {
      if (event.defaultPrevented || backgroundInert || event.key !== 'Escape') return
      event.preventDefault()
      onCloseOverlay()
    }
    document.addEventListener('keydown', handleEscape)
    return () => document.removeEventListener('keydown', handleEscape)
  }, [backgroundInert, mode, onCloseOverlay, overlayOpen, wideInteractive])

  useEffect(() => {
    if (!openMenu) return
    const handleOutsidePointerDown = (event: PointerEvent) => {
      const target = event.target
      if (target instanceof Element && (menuRef.current?.contains(target) || target.closest('.conversation-more'))) return
      setOpenMenu(null)
    }
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.key !== 'Escape') return
      event.preventDefault()
      openMenu.trigger.focus()
      setOpenMenu(null)
    }
    document.addEventListener('pointerdown', handleOutsidePointerDown)
    document.addEventListener('keydown', handleKeyDown)
    return () => {
      document.removeEventListener('pointerdown', handleOutsidePointerDown)
      document.removeEventListener('keydown', handleKeyDown)
    }
  }, [openMenu])

  useLayoutEffect(() => {
    if (!openMenu) return
    menuRef.current?.querySelector<HTMLButtonElement>('button:not([disabled])')?.focus()
  }, [openMenu])

  useLayoutEffect(() => {
    const scrollElement = historyScrollRef.current
    if (scrollElement) updateStickyHistoryTitle(scrollElement)
  }, [groups, updateStickyHistoryTitle])

  useLayoutEffect(() => {
    const anchor = paginationAnchorRef.current
    const root = historyScrollRef.current
    if (!anchor || !root) return
    const anchoredItem = anchor.threadId
      ? Array.from(root.querySelectorAll<HTMLElement>('[data-history-thread-id]'))
          .find((item) => item.dataset.historyThreadId === anchor.threadId)
      : undefined
    if (anchoredItem) {
      const nextOffset = anchoredItem.getBoundingClientRect().top - root.getBoundingClientRect().top
      root.scrollTop += nextOffset - anchor.offset
    } else {
      root.scrollTop = anchor.scrollTop
    }
    paginationAnchorRef.current = null
    updateStickyHistoryTitle(root)
  }, [groups, updateStickyHistoryTitle])

  useLayoutEffect(() => {
    if (!newSubmission || locatedSubmission.current === newSubmission || !wideInteractive || isHistorySearchActive) return
    const root = historyScrollRef.current
    const heading = root?.querySelector<HTMLElement>('#conversation-group-today')
    if (!root || !heading) return
    locatedSubmission.current = newSubmission
    paginationAnchorRef.current = null
    programmaticScroll.current = true
    root.scrollTop += heading.getBoundingClientRect().top - root.getBoundingClientRect().top
    updateStickyHistoryTitle(root)
  }, [newSubmission, groups, wideInteractive, isHistorySearchActive, updateStickyHistoryTitle])

  useEffect(() => {
    paginationArmedRef.current = true
    paginationAnchorRef.current = null
    const sentinel = historyLoadSentinelRef.current
    const root = historyScrollRef.current
    if (!sentinel || !root || typeof IntersectionObserver === 'undefined') return
    const observer = new IntersectionObserver((entries) => {
      const visible = entries.some((entry) => entry.isIntersecting)
      if (!visible) {
        paginationArmedRef.current = true
        return
      }
      // 同一滑动批次由 wheel 和 scroll 共用请求所有权，哨兵仅处理无滚动的布局变化
      if (paginationRequestedInScrollBurstRef.current) return
      requestHistoryPage()
    }, {
      root,
      rootMargin: `0px 0px ${HISTORY_PAGE_PRELOAD_DISTANCE_PX}px`,
      threshold: 0.01,
    })
    observer.observe(sentinel)
    return () => observer.disconnect()
  }, [historyQuery, requestHistoryPage])

  useLayoutEffect(() => {
    const root = historyScrollRef.current
    if (
      !root
      || root.clientHeight <= 0
      || isLoadingMore
      || loadMoreError
      || !hasMore
      || root.scrollHeight > root.clientHeight + HISTORY_PAGE_PRELOAD_DISTANCE_PX
    ) return
    paginationArmedRef.current = true
    const frame = window.requestAnimationFrame(() => requestHistoryPage())
    return () => window.cancelAnimationFrame(frame)
  }, [groups, hasMore, isLoadingMore, loadMoreError, requestHistoryPage])

  useEffect(() => {
    if (loadMoreError) paginationAnchorRef.current = null
  }, [loadMoreError])

  useEffect(() => {
    if (!userMenu) return
    const handleOutsidePointerDown = (event: PointerEvent) => {
      const target = event.target
      if (target instanceof Node && userMenuRef.current?.contains(target)) return
      setUserMenu(false)
    }
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented || backgroundInert || event.key !== 'Escape') return
      event.preventDefault()
      setUserMenu(false)
      userMenuButtonRef.current?.focus()
    }
    document.addEventListener('pointerdown', handleOutsidePointerDown)
    document.addEventListener('keydown', handleKeyDown)
    return () => {
      document.removeEventListener('pointerdown', handleOutsidePointerDown)
      document.removeEventListener('keydown', handleKeyDown)
    }
  }, [backgroundInert, userMenu])

  useLayoutEffect(() => {
    if (!userMenu) return
    // 账户菜单打开后直接进入首项，让键盘用户无需额外 Tab 即可开始操作
    userMenuPopupRef.current?.querySelector<HTMLElement>('[role="menuitem"]')?.focus()
  }, [userMenu])

  const handleUserMenuKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    // 方向键只在账户菜单内循环，Tab 仍沿页面自然顺序离开并触发菜单收起
    const items = Array.from(
      userMenuPopupRef.current?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]') ?? [],
    )
    if (items.length === 0) return
    const currentIndex = Math.max(0, items.indexOf(document.activeElement as HTMLButtonElement))
    let nextIndex: number | undefined
    if (event.key === 'ArrowDown') nextIndex = (currentIndex + 1) % items.length
    else if (event.key === 'ArrowUp') nextIndex = (currentIndex - 1 + items.length) % items.length
    else if (event.key === 'Home') nextIndex = 0
    else if (event.key === 'End') nextIndex = items.length - 1
    else if (event.key === 'Escape') {
      event.preventDefault()
      setUserMenu(false)
      userMenuButtonRef.current?.focus()
      return
    }
    if (nextIndex == null) return
    event.preventDefault()
    items[nextIndex]?.focus()
  }

  useEffect(() => () => {
    if (paginationScrollBurstTimerRef.current != null) {
      window.clearTimeout(paginationScrollBurstTimerRef.current)
    }
  }, [])

  useEffect(() => {
    const handleNewConversationShortcut = (event: KeyboardEvent) => {
      if (
        event.defaultPrevented
        || backgroundInert
        || !event.metaKey
        || event.key.toLocaleLowerCase('en-US') !== 'k'
      ) return
      event.preventDefault()
      onNew()
    }
    document.addEventListener('keydown', handleNewConversationShortcut)
    return () => document.removeEventListener('keydown', handleNewConversationShortcut)
  }, [backgroundInert, onNew])

  const selectConversation = (threadId: string) => {
    closeMenu()
    onSelect(threadId)
    if (mode === 'overlay') onCloseOverlay(false)
  }

  const renderItems = (items: Conversation[]) => items.map((conversation) => {
    const pending = pendingConversations.find(item => item.conversation === conversation)
    return <ConversationItem
      key={pending ? `pending:${pending.runId}` : conversation.threadId}
      conversation={conversation}
      pendingRunId={pending?.runId}
      isActive={!automationActive && (pending
        ? selectedPendingRunId === pending.runId
        : conversation.threadId === workspace.currentThreadId)}
      isMenuOpen={!pending && openMenu?.threadId === conversation.threadId}
      onSelect={() => {
        if (pending) {
          closeMenu()
          onSelectPending?.(pending.runId)
          if (mode === 'overlay') onCloseOverlay(false)
        } else selectConversation(conversation.threadId)
      }}
      onRemove={pending && conversation.runStatus === 'error'
        ? () => onRemovePending?.(pending.runId) : undefined}
      onToggleMenu={!pending && conversation.threadId
        ? (event) => toggleMenu(conversation, event.currentTarget)
        : undefined}
    />
  })

  return (
    <>
      {mode === 'overlay' && overlayOpen && (
        <button type="button" className="sidebar-scrim" aria-label={t('关闭导航遮罩')} onClick={() => onCloseOverlay()} />
      )}
      <aside
        ref={rootRef}
        data-workspace-layout-target="sidebar"
        id="workspace-sidebar"
        className={`workspace-sidebar is-${mode}${overlayOpen ? ' is-overlay-open' : ''}`}
        data-sidebar-mode={mode}
        aria-label={t('会话导航')}
        aria-hidden={isOverlayHidden || backgroundInert || undefined}
        inert={isOverlayHidden || backgroundInert || undefined}
      >
        <div
          className="sidebar-wide"
          aria-hidden={!wideInteractive || undefined}
          inert={!wideInteractive || undefined}
        >
          <div className={`sidebar-head${isSearchOpen ? ' is-search-open' : ''}`}>
            <button type="button" className="brand" aria-label={t('新会话')} onClick={onNew}>
              <BrandLogo size="md" />
            </button>
            <div className="sidebar-head-actions">
              <IconButton
                ref={searchTriggerRef}
                size="sm"
                label={t('搜索会话')}
                tooltip={t('搜索会话')}
                icon={<Search size={17} />}
                selected={isSearchOpen || isHistorySearchActive}
                aria-expanded={isSearchOpen}
                aria-controls="sidebar-search"
                onClick={() => openSearch(false)}
              />
              {mode === 'overlay' ? (
                <IconButton ref={overlayCloseButtonRef} label={t('关闭导航')} icon={<X size={18} />} onClick={() => onCloseOverlay()} />
              ) : (
                <IconButton
                  className="sidebar-mode-toggle"
                  size="sm"
                  label={t('收起侧边栏')}
                  tooltip={t('收起侧边栏')}
                  icon={<PanelRight size={17} />}
                  aria-controls="workspace-sidebar"
                  aria-expanded="true"
                  onClick={onToggleMode}
                />
              )}
            </div>
            <label id="sidebar-search" className="sidebar-search">
              <Search size={16} aria-hidden="true" />
              <input
                ref={searchInputRef}
                aria-label={t('搜索会话')}
                value={historyQuery}
                onChange={(event) => onHistoryQueryChange(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key !== 'Escape') return
                  event.preventDefault()
                  clearAndCloseSearch()
                }}
                placeholder={t('搜索对话')}
              />
              <IconButton label={t('清除搜索')} icon={<X size={16} />} onClick={() => clearAndCloseSearch()} />
            </label>
          </div>

          <div className="new-chat-wrap">
            <Button
              className="new-chat"
              size="lg"
              variant="ghost"
              leadingIcon={<SquarePen size={18} />}
              trailingIcon={<kbd className="new-chat-shortcut">⌘ K</kbd>}
              aria-keyshortcuts="Meta+K"
              onClick={onNew}
            >
              {t('新会话')}
            </Button>
          </div>

          <div className="conversation-history">
            <div
              className={`conversation-sticky-title${stickyHistoryTitle ? ' is-visible' : ''}`}
              aria-hidden="true"
            >
              {stickyHistoryTitle}
            </div>
            <div
              ref={historyScrollRef}
              className={`conversation-scroll ui-scrollbar${openMenu ? ' is-scroll-locked' : ''}`}
              role="region"
              aria-label={t('最近对话')}
              tabIndex={0}
              onScroll={(event) => {
                const element = event.currentTarget
                updateStickyHistoryTitle(element)
                if (programmaticScroll.current) {
                  programmaticScroll.current = false
                  return
                }
                if (paginationAnchorRef.current) capturePaginationAnchor()
                sustainPaginationScrollBurst()
                if (openMenu) {
                  element.scrollTop = lockedHistoryScrollTop.current
                  return
                }
                requestHistoryPageFromViewport(element)
              }}
              onWheel={(event) => {
                programmaticScroll.current = false
                sustainPaginationScrollBurst()
                if (openMenu || event.deltaY <= 0) return
                requestHistoryPageFromViewport(event.currentTarget)
              }}
            >
              <nav className="primary-nav" aria-label={t('工作区功能')}>
                <Button size="sm" variant="ghost" leadingIcon={<BrainCircuit size={18} />} disabled>{t('记忆管理')}</Button>
                <Button size="sm" variant="ghost" leadingIcon={<BookOpenCheck size={18} />} disabled>{t('技能库')}</Button>
                <Button type="button" size="sm" variant="ghost" leadingIcon={<AlarmClock size={18} />} selected={automationActive}
                  aria-current={automationActive ? 'page' : undefined} onClick={onOpenAutomation}>{t('自动化')}</Button>
                <Button size="sm" variant="ghost" leadingIcon={<CircleEllipsis size={18} />} disabled>{t('更多')}</Button>
              </nav>
              <div className="conversation-groups">
                {groups.map((group) => (
                  <Fragment key={group.key}>
                    <h2
                      id={`conversation-group-${group.key}`}
                      className="conversation-group-title"
                      data-history-group-label={group.label}
                    >
                      {group.label}
                    </h2>
                    <section className="conversation-group-items" aria-labelledby={`conversation-group-${group.key}`}>
                      {renderItems(group.items)}
                    </section>
                  </Fragment>
                ))}
              {isHistorySearching && (
                <div className="history-skeleton-list history-search-skeletons" aria-label={t('正在搜索会话')}>
                  {Array.from({ length: 5 }, (_, index) => (
                    <div key={index} className="history-skeleton" data-testid="history-search-skeleton" aria-hidden="true"><span /></div>
                  ))}
                </div>
              )}
              {!isHistorySearching && groups.length === 0 && !loadMoreError && (
                <p className="no-search-result">{historyQuery.trim() ? t('没有匹配的对话') : t('暂无最近对话')}</p>
              )}
              {/* 尾部槽位在可分页期间保持固定高度，loading 切换不再改变原生滚动条几何 */}
              {(hasMore || isLoadingMore || loadMoreError) && (
                <div className="history-pagination-slot">
                  {isLoadingMore ? (
                    <div className="history-pagination-status" role="status">
                      {t('正在加载更多历史会话')}
                    </div>
                  ) : loadMoreError ? (
                    <div className="history-pagination-status">
                      {onRetryLoadMore && (
                        <Button size="sm" variant="text" onClick={onRetryLoadMore}>
                          {t('重试加载历史')}
                        </Button>
                      )}
                    </div>
                  ) : null}
                </div>
              )}
              <div ref={historyLoadSentinelRef} className="history-load-sentinel" aria-hidden="true" />
              </div>
            </div>
            <OverlayScrollbar viewportRef={historyScrollRef} />
          </div>

          <div ref={userMenuRef} className="user-account">
            <button
              ref={userMenuButtonRef}
              type="button"
              className="user-card"
              aria-label={userMenu ? t('关闭用户菜单') : t('打开用户菜单')}
              aria-expanded={userMenu}
              aria-haspopup="menu"
              aria-controls="user-account-menu"
              onClick={() => setUserMenu((value) => !value)}
            >
              <UserAvatar
                avatarUrl={user.avatar_url}
                displayName={user.display_name}
                username={user.username}
              />
              <strong className="user-name">{user.display_name.trim() || user.username}</strong>
            </button>
            {userMenu && (
              <div
                ref={userMenuPopupRef}
                id="user-account-menu"
                className="user-menu"
                role="menu"
                tabIndex={-1}
                aria-label={t('账户')}
                onKeyDown={handleUserMenuKeyDown}
                onBlur={(event) => {
                  if (event.relatedTarget instanceof Node && userMenuRef.current?.contains(event.relatedTarget)) return
                  setUserMenu(false)
                }}
              >
                <Button
                  role="menuitem"
                  tabIndex={-1}
                  variant="ghost"
                  leadingIcon={<Settings2 size={16} />}
                  onClick={() => {
                    setUserMenu(false)
                    onOpenSettings(userMenuButtonRef.current)
                  }}
                >
                  {t('设置')}
                </Button>
                <Button role="menuitem" tabIndex={-1} variant="ghost" leadingIcon={<LogOut size={16} />} onClick={onLogout}>{t('退出登录')}</Button>
              </div>
            )}
          </div>
        </div>

        <div
          className="sidebar-rail"
          aria-hidden={!railInteractive || undefined}
          inert={!railInteractive || undefined}
        >
          {/* 侧栏形态切换期间 Rail 仍会保留，非交互形态必须主动退出键盘路径 */}
          <IconButton
            className="rail-brand-toggle"
            label={t('打开侧边栏')}
            tooltip={t('打开侧边栏')}
            icon={<PanelRight size={17} />}
            tabIndex={railInteractive ? 0 : -1}
            aria-controls="workspace-sidebar"
            aria-expanded="false"
            onClick={onToggleMode}
          />
          <IconButton label={t('新会话')} icon={<SquarePen size={18} />} tabIndex={railInteractive ? 0 : -1} onClick={onNew} />
          <IconButton
            label={historyQuery ? t('搜索会话，当前查询：{query}', { query: historyQuery }) : t('搜索会话')}
            tooltip={t('搜索会话')}
            icon={<Search size={18} />}
            selected={isSearchOpen || isHistorySearchActive}
            tabIndex={railInteractive ? 0 : -1}
            aria-expanded={isSearchOpen}
            aria-controls="sidebar-search"
            onClick={() => openSearch(true)}
          />
          <IconButton label={t('记忆管理')} tooltip={t('记忆管理')} icon={<BrainCircuit size={18} />} tabIndex={railInteractive ? 0 : -1} disabled />
          <span className="rail-spacer" />
          <IconButton
            label={t('展开侧边栏以查看账户')}
            tooltip={t('账户')}
            icon={(
              <UserAvatar
                avatarUrl={user.avatar_url}
                displayName={user.display_name}
                username={user.username}
              />
            )}
            tabIndex={railInteractive ? 0 : -1}
            onClick={onRequestExpanded}
          />
        </div>

        {menuConversation && openMenu && wideInteractive && (
          <div ref={menuRef} className="conversation-menu conversation-menu-floating" style={{ top: openMenu.top, left: openMenu.left }}>
            <Button loading={pinPendingThreadIds.has(menuConversation.threadId)} disabled={pinPendingThreadIds.has(menuConversation.threadId)} variant="ghost" leadingIcon={menuConversation.pinned ? <PinOff size={15} /> : <Pin size={15} />} onClick={() => { onPin(menuConversation.threadId); closeMenu(true) }}>
              {menuConversation.pinned ? t('取消置顶') : t('置顶')}
            </Button>
            <Button variant="ghost" leadingIcon={<Pencil size={15} />} onClick={() => { onRename(menuConversation.threadId, openMenu.trigger); closeMenu() }}>{t('重命名')}</Button>
            <Button variant="ghost" className="conversation-menu-danger" leadingIcon={<Trash2 size={15} />} onClick={() => { onDelete(menuConversation.threadId, openMenu.trigger); closeMenu() }}>{t('删除')}</Button>
          </div>
        )}
      </aside>
    </>
  )
}
