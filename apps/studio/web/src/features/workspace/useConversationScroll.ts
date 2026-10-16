import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from 'react'

import type { Conversation } from '../../types'
import { MOTION_DURATION_MS } from '../../components/ui/motion'

const CONVERSATION_SCROLL_KEY_PREFIX = 'tinkerfin:conversation-scroll:'
const SCROLL_BUTTON_IDLE_MS = 1500
const SCROLL_BUTTON_HIDE_MS = SCROLL_BUTTON_IDLE_MS + MOTION_DURATION_MS.slow
const conversationScrollKey = (threadId: string) => `${CONVERSATION_SCROLL_KEY_PREFIX}${threadId}`

type ScrollButtonPhase = 'hidden' | 'visible' | 'fading'

type ConversationScrollState = {
  scrollTop: number
  followLatest: boolean
}

const readConversationScroll = (threadId: string): ConversationScrollState | null => {
  try {
    const storedValue = window.sessionStorage.getItem(conversationScrollKey(threadId))
    if (storedValue == null) return null
    const value: unknown = JSON.parse(storedValue)
    if (value == null || typeof value !== 'object' || Array.isArray(value)) return null
    const state = value as Record<string, unknown>
    return Number.isFinite(state.scrollTop)
      && typeof state.scrollTop === 'number'
      && state.scrollTop >= 0
      && typeof state.followLatest === 'boolean'
      ? { scrollTop: state.scrollTop, followLatest: state.followLatest }
      : null
  } catch {
    return null
  }
}

const writeConversationScroll = (
  threadId: string,
  state: ConversationScrollState,
): void => {
  try {
    window.sessionStorage.setItem(conversationScrollKey(threadId), JSON.stringify({
      scrollTop: Math.max(0, state.scrollTop),
      followLatest: state.followLatest,
    }))
  } catch {
    // 浏览器禁用会话存储时保留原有滚动行为
  }
}

export function useConversationScroll({
  conversation,
  isRunning,
  active = true,
}: {
  conversation: Conversation
  isRunning: boolean
  active?: boolean
}) {
  const [showScrollToBottom, setShowScrollToBottom] = useState(false)
  const [fadeScrollToBottom, setFadeScrollToBottom] = useState(false)
  const paneRef = useRef<HTMLElement>(null)
  const messageEndRef = useRef<HTMLDivElement>(null)
  const followLatest = useRef(true)
  const readingHistory = useRef(false)
  const pendingImmediateScroll = useRef(false)
  const scrollingToBottom = useRef(false)
  const followScrollFrame = useRef<number | null>(null)
  const scrollMeasureFrame = useRef<number | null>(null)
  const scrollPersistenceFrame = useRef<number | null>(null)
  const pendingScrollPersistence = useRef<{
    threadId: string
    state: ConversationScrollState
  } | null>(null)
  const scrollButtonFadeTimeout = useRef<number | null>(null)
  const scrollButtonHideTimeout = useRef<number | null>(null)
  const scrollButtonHovered = useRef(false)
  const scrollButtonFocused = useRef(false)
  const scrollButtonPhase = useRef<ScrollButtonPhase>('hidden')
  const pendingUserScrollIntent = useRef(false)
  const userHasScrolled = useRef(false)
  const resizedScroll = useRef<{ pane: HTMLElement; top: number } | null>(null)
  const pendingConversationScroll = useRef<{
    threadId: string
    state: ConversationScrollState | null
  } | null>(null)
  const lastForcedApprovalIdentity = useRef<string | null>(null)
  const previousActive = useRef(active)
  const pendingApprovalKey = conversation.approval && !conversation.approval.submitted
    ? conversation.approval.items.map((item) => item.interruptId).join('\u0000')
    : null

  const setScrollButtonPhase = useCallback((phase: ScrollButtonPhase) => {
    scrollButtonPhase.current = phase
    setShowScrollToBottom(phase !== 'hidden')
    setFadeScrollToBottom(phase === 'fading')
  }, [])

  const clearScrollButtonTimers = useCallback(() => {
    if (scrollButtonFadeTimeout.current != null) {
      window.clearTimeout(scrollButtonFadeTimeout.current)
      scrollButtonFadeTimeout.current = null
    }
    if (scrollButtonHideTimeout.current != null) {
      window.clearTimeout(scrollButtonHideTimeout.current)
      scrollButtonHideTimeout.current = null
    }
  }, [])

  const commitPendingScrollPersistence = useCallback(() => {
    const pending = pendingScrollPersistence.current
    pendingScrollPersistence.current = null
    if (pending) writeConversationScroll(pending.threadId, pending.state)
  }, [])

  const flushScrollPersistence = useCallback(() => {
    if (scrollPersistenceFrame.current != null) {
      window.cancelAnimationFrame(scrollPersistenceFrame.current)
      scrollPersistenceFrame.current = null
    }
    commitPendingScrollPersistence()
  }, [commitPendingScrollPersistence])

  const scheduleScrollPersistence = useCallback((
    threadId: string,
    state: ConversationScrollState,
  ) => {
    pendingScrollPersistence.current = { threadId, state }
    if (scrollPersistenceFrame.current != null) return
    scrollPersistenceFrame.current = window.requestAnimationFrame(() => {
      scrollPersistenceFrame.current = null
      commitPendingScrollPersistence()
    })
  }, [commitPendingScrollPersistence])

  const armScrollButtonFade = useCallback(() => {
    clearScrollButtonTimers()
    scrollButtonFadeTimeout.current = window.setTimeout(() => {
      setScrollButtonPhase('fading')
    }, SCROLL_BUTTON_IDLE_MS)
    scrollButtonHideTimeout.current = window.setTimeout(() => {
      setScrollButtonPhase('hidden')
    }, SCROLL_BUTTON_HIDE_MS)
  }, [clearScrollButtonTimers, setScrollButtonPhase])

  const handleScroll = useCallback((pane: HTMLElement) => {
    if (followScrollFrame.current != null) {
      window.cancelAnimationFrame(followScrollFrame.current)
      followScrollFrame.current = null
    }
    const isNearBottom = pane.scrollHeight - pane.scrollTop - pane.clientHeight <= 96
    if (pendingUserScrollIntent.current) {
      pendingUserScrollIntent.current = false
      userHasScrolled.current = !isNearBottom
      followLatest.current = isNearBottom
    }
    if (isNearBottom && !readingHistory.current) {
      followLatest.current = true
      scrollingToBottom.current = false
      userHasScrolled.current = false
    }
    if (conversation.threadId) {
      scheduleScrollPersistence(conversation.threadId, {
        scrollTop: pane.scrollTop,
        followLatest: followLatest.current,
      })
    }
    // 调宽恢复阅读位置产生的滚动只保存位置，不唤醒按钮或重置闲置计时
    const resized = resizedScroll.current
    if (resized?.pane === pane && resized.top === pane.scrollTop) return
    resizedScroll.current = null
    if (scrollMeasureFrame.current != null) return
    scrollMeasureFrame.current = window.requestAnimationFrame(() => {
      scrollMeasureFrame.current = null
      const isNearBottom = pane.scrollHeight - pane.scrollTop - pane.clientHeight <= 96
      if (isNearBottom && !readingHistory.current) {
        followLatest.current = true
        scrollingToBottom.current = false
        clearScrollButtonTimers()
        setScrollButtonPhase('hidden')
        scrollButtonHovered.current = false
        userHasScrolled.current = false
      } else if (userHasScrolled.current) {
        followLatest.current = false
        if (scrollingToBottom.current) {
          setScrollButtonPhase('hidden')
        } else {
          setScrollButtonPhase('visible')
          if (!scrollButtonHovered.current && !scrollButtonFocused.current) armScrollButtonFade()
        }
      } else if (scrollingToBottom.current) {
        setScrollButtonPhase('hidden')
      } else {
        setScrollButtonPhase('hidden')
      }
    })
  }, [armScrollButtonFade, clearScrollButtonTimers, conversation.threadId, scheduleScrollPersistence, setScrollButtonPhase])

  const scrollToBottomImmediately = useCallback(() => {
    readingHistory.current = false
    pendingImmediateScroll.current = true
    followLatest.current = true
    scrollingToBottom.current = false
    if (followScrollFrame.current != null) {
      window.cancelAnimationFrame(followScrollFrame.current)
      followScrollFrame.current = null
    }
    clearScrollButtonTimers()
    setScrollButtonPhase('hidden')
    scrollButtonHovered.current = false
    pendingUserScrollIntent.current = false
    userHasScrolled.current = false

    const pane = paneRef.current
    if (pane) {
      pane.scrollTop = pane.scrollHeight
      if (conversation.threadId) {
        scheduleScrollPersistence(conversation.threadId, {
          scrollTop: pane.scrollTop,
          followLatest: true,
        })
      }
    } else {
      messageEndRef.current?.scrollIntoView?.({ behavior: 'auto', block: 'end' })
    }
  }, [clearScrollButtonTimers, conversation.threadId, scheduleScrollPersistence, setScrollButtonPhase])

  const syncToBottomIfFollowing = useCallback(() => {
    if (!followLatest.current) return
    const pane = paneRef.current
    if (!pane) return
    pane.scrollTop = pane.scrollHeight
  }, [])

  // 阅读历史前同步暂停跟随，避免定位期间的新消息把视口拉回末尾
  const pauseFollowing = useCallback(() => {
    readingHistory.current = true
    followLatest.current = false
    pendingImmediateScroll.current = false
    scrollingToBottom.current = false
    pendingUserScrollIntent.current = false
    userHasScrolled.current = true
    if (followScrollFrame.current != null) {
      window.cancelAnimationFrame(followScrollFrame.current)
      followScrollFrame.current = null
    }
    clearScrollButtonTimers()
    setScrollButtonPhase('visible')
  }, [clearScrollButtonTimers, setScrollButtonPhase])

  const markUserScrollIntent = useCallback(() => {
    resizedScroll.current = null
    readingHistory.current = false
    scrollingToBottom.current = false
    pendingUserScrollIntent.current = true
  }, [])

  const scrollToBottom = useCallback(() => {
    readingHistory.current = false
    // 操作完成后按钮会退出可访问树，焦点必须交给仍可继续阅读的对话区域
    paneRef.current?.focus({ preventScroll: true })
    followLatest.current = true
    scrollingToBottom.current = true
    clearScrollButtonTimers()
    setScrollButtonPhase('hidden')
    scrollButtonHovered.current = false
    pendingUserScrollIntent.current = false
    userHasScrolled.current = false
    const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    paneRef.current?.scrollTo({
      top: paneRef.current.scrollHeight,
      behavior: reduceMotion ? 'auto' : 'smooth',
    })
  }, [clearScrollButtonTimers, setScrollButtonPhase])

  const pauseScrollToBottomFade = useCallback(() => {
    scrollButtonHovered.current = true
    clearScrollButtonTimers()
    setScrollButtonPhase('visible')
  }, [clearScrollButtonTimers, setScrollButtonPhase])

  const resumeScrollToBottomFade = useCallback(() => {
    scrollButtonHovered.current = false
    if (!scrollButtonFocused.current && !followLatest.current) armScrollButtonFade()
  }, [armScrollButtonFade])

  const focusScrollToBottom = useCallback(() => {
    scrollButtonFocused.current = true
    clearScrollButtonTimers()
    setScrollButtonPhase('visible')
  }, [clearScrollButtonTimers, setScrollButtonPhase])

  const blurScrollToBottom = useCallback(() => {
    scrollButtonFocused.current = false
    if (!scrollButtonHovered.current && !followLatest.current) armScrollButtonFade()
  }, [armScrollButtonFade])

  useLayoutEffect(() => {
    // 会话切换前先提交旧会话最后一次滚动位置，避免新线程覆盖待写状态
    flushScrollPersistence()
    const savedScroll = conversation.threadId
      ? readConversationScroll(conversation.threadId)
      : null
    pendingConversationScroll.current = conversation.threadId
      ? { threadId: conversation.threadId, state: savedScroll }
      : null
    lastForcedApprovalIdentity.current = null
    resizedScroll.current = null
    readingHistory.current = false
    followLatest.current = savedScroll?.followLatest ?? true
    scrollingToBottom.current = false
    if (followScrollFrame.current != null) {
      window.cancelAnimationFrame(followScrollFrame.current)
      followScrollFrame.current = null
    }
    if (scrollMeasureFrame.current != null) {
      window.cancelAnimationFrame(scrollMeasureFrame.current)
      scrollMeasureFrame.current = null
    }
    pendingUserScrollIntent.current = false
    userHasScrolled.current = false
    clearScrollButtonTimers()
    setScrollButtonPhase('hidden')
    scrollButtonHovered.current = false
    scrollButtonFocused.current = false
  }, [clearScrollButtonTimers, conversation.threadId, flushScrollPersistence, setScrollButtonPhase])

  useLayoutEffect(() => {
    const wasActive = previousActive.current
    previousActive.current = active
    if (wasActive && !active) {
      flushScrollPersistence()
      return
    }
    if (wasActive || !active) return

    const pane = paneRef.current
    if (!pane) return
    const maxScroll = Math.max(0, pane.scrollHeight - pane.clientHeight)
    if (followLatest.current) {
      pane.scrollTop = maxScroll
    } else if (conversation.threadId) {
      const savedScroll = readConversationScroll(conversation.threadId)
      if (savedScroll != null) pane.scrollTop = Math.min(savedScroll.scrollTop, maxScroll)
    }
    handleScroll(pane)
  }, [active, conversation.threadId, flushScrollPersistence, handleScroll])

  useLayoutEffect(() => {
    if (!conversation.threadId || !conversation.isHydrated || !pendingApprovalKey) return
    const identity = `${conversation.threadId}:${pendingApprovalKey}`
    if (lastForcedApprovalIdentity.current === identity) return
    lastForcedApprovalIdentity.current = identity
    // 待审批会话需要先让用户看到对话尾部状态，但只能覆盖缓存位置一次
    scrollToBottomImmediately()
  }, [
    conversation.isHydrated,
    conversation.threadId,
    pendingApprovalKey,
    scrollToBottomImmediately,
  ])

  useLayoutEffect(() => {
    const pane = paneRef.current
    if (pendingImmediateScroll.current) {
      pendingImmediateScroll.current = false
      pendingConversationScroll.current = null
      followLatest.current = true
      if (pane) {
        pane.scrollTop = pane.scrollHeight
        if (conversation.threadId) {
          scheduleScrollPersistence(conversation.threadId, {
            scrollTop: pane.scrollTop,
            followLatest: true,
          })
        }
      } else {
        messageEndRef.current?.scrollIntoView?.({ behavior: 'auto', block: 'end' })
      }
      return
    }
    const pendingScroll = pendingConversationScroll.current
    const shouldRestoreConversationScroll = Boolean(
      pane
      && conversation.threadId
      && pendingScroll?.threadId === conversation.threadId
      && conversation.isHydrated
    )
    if (shouldRestoreConversationScroll && pane && pendingScroll) {
      pendingConversationScroll.current = null
      if (pendingScroll.state != null) {
        pane.scrollTop = pendingScroll.state.followLatest
          ? pane.scrollHeight
          : pendingScroll.state.scrollTop
        handleScroll(pane)
        return
      }
    }
    if (!followLatest.current) return
    if (followScrollFrame.current != null) window.cancelAnimationFrame(followScrollFrame.current)
    followScrollFrame.current = window.requestAnimationFrame(() => {
      followScrollFrame.current = null
      if (!followLatest.current) return
      if (paneRef.current) paneRef.current.scrollTop = paneRef.current.scrollHeight
      else messageEndRef.current?.scrollIntoView?.({ behavior: 'auto', block: 'end' })
      clearScrollButtonTimers()
      setScrollButtonPhase('hidden')
      scrollButtonHovered.current = false
    })
  }, [
    conversation.approval?.submitted,
    conversation.isHydrated,
    conversation.messages,
    conversation.notice,
    conversation.threadId,
    clearScrollButtonTimers,
    handleScroll,
    isRunning,
    setScrollButtonPhase,
    scheduleScrollPersistence,
  ])

  useEffect(() => {
    const measure = () => {
      if (paneRef.current) handleScroll(paneRef.current)
    }
    window.addEventListener('resize', measure)
    return () => window.removeEventListener('resize', measure)
  }, [handleScroll])

  useLayoutEffect(() => {
    const content = messageEndRef.current?.parentElement
    if (!active || !content) return
    // 正文逐字增长发生在消息组件内，接收结束后仍需跟随；阅读历史时不移动视口
    const observer = new ResizeObserver(syncToBottomIfFollowing)
    observer.observe(content)
    return () => observer.disconnect()
  }, [active, conversation.threadId, conversation.messages.length, conversation.isHydrated, syncToBottomIfFollowing])

  useEffect(() => {
    const handlePageHide = () => flushScrollPersistence()
    window.addEventListener('pagehide', handlePageHide)
    return () => {
      window.removeEventListener('pagehide', handlePageHide)
      flushScrollPersistence()
    }
  }, [flushScrollPersistence])

  useEffect(() => () => {
    clearScrollButtonTimers()
    if (followScrollFrame.current != null) window.cancelAnimationFrame(followScrollFrame.current)
    if (scrollMeasureFrame.current != null) window.cancelAnimationFrame(scrollMeasureFrame.current)
    flushScrollPersistence()
  }, [clearScrollButtonTimers, flushScrollPersistence])

  // 调宽只调整视觉布局；阅读历史时保留可见消息的位置，不改变跟随意图
  const resizeContent = useCallback((apply: () => void) => {
    const pane = paneRef.current
    const content = messageEndRef.current?.parentElement
    if (!pane || !content) { apply(); return }
    pendingUserScrollIntent.current = false
    const following = followLatest.current && !readingHistory.current
    const top = pane.getBoundingClientRect().top
    const anchor = Array.from(content.children).find(element => element.getBoundingClientRect().bottom > top)
    const offset = anchor?.getBoundingClientRect().top
    apply()
    if (following) pane.scrollTop = pane.scrollHeight
    else if (anchor?.isConnected && offset !== undefined) pane.scrollTop += anchor.getBoundingClientRect().top - offset
    resizedScroll.current = { pane, top: pane.scrollTop }
  }, [])

  const isFollowingLatest = useCallback(() => followLatest.current && !readingHistory.current, [])

  return {
    isFollowingLatest,
    resizeContent,
    paneRef,
    messageEndRef,
    showScrollToBottom,
    fadeScrollToBottom,
    handleScroll,
    scrollToBottomImmediately,
    syncToBottomIfFollowing,
    pauseFollowing,
    markUserScrollIntent,
    scrollToBottom,
    pauseScrollToBottomFade,
    resumeScrollToBottomFade,
    focusScrollToBottom,
    blurScrollToBottom,
  }
}
