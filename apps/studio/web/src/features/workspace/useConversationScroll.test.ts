import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ApprovalState, Conversation } from '../../types'
import { useConversationScroll } from './useConversationScroll'

const conversation: Conversation = { accessMode: 'write_approval',
  threadId: 'thread-scroll',
  title: '滚动测试',
  pinned: false,
  updatedAt: '2026-08-24T00:00:00Z',
  model: 'GPT-5.5',
  mode: 'default',
  messages: [],
  todos: [],
  taskTrace: { phase: 'unloaded' },
  runStatus: 'idle',
  isHydrated: true,
}

const pendingApproval: ApprovalState = {
  activeIndex: 0,
  submitted: false,
  mode: 'options',
  items: [{
    id: 'approval-scroll',
    interruptId: 'interrupt-scroll',
    toolCallId: 'tool-scroll',
    toolName: 'write_file',
    params: '{"file_path":"/scroll.txt"}',
    input: '/scroll.txt',
    description: '等待写入文件',
    originalArgs: { file_path: '/scroll.txt' },
    allowedDecisions: ['approve', 'reject'],
  }],
}

const storedScroll = (threadId: string) => JSON.parse(
  window.sessionStorage.getItem(`tinkerfin:conversation-scroll:${threadId}`) ?? 'null',
) as { scrollTop: number; followLatest: boolean } | null

const setStoredScroll = (
  threadId: string,
  value: { scrollTop: number; followLatest: boolean },
) => window.sessionStorage.setItem(
  `tinkerfin:conversation-scroll:${threadId}`,
  JSON.stringify(value),
)

describe('useConversationScroll', () => {
  beforeEach(() => {
    window.sessionStorage.clear()
    vi.useFakeTimers()
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => (
      window.setTimeout(() => callback(performance.now()), 0)
    ))
    vi.stubGlobal('cancelAnimationFrame', (id: number) => window.clearTimeout(id))
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('调宽保持历史消息位置，跟随输出时保持底部', () => {
    const { result, unmount } = renderHook(() => useConversationScroll({ conversation, isRunning: false }))
    const pane = document.createElement('section')
    const content = document.createElement('div')
    const message = document.createElement('article')
    const end = document.createElement('div')
    content.append(message, end)
    pane.append(content)
    document.body.append(pane)
    Object.defineProperties(pane, {
      clientHeight: { value: 400 }, scrollHeight: { value: 1200 },
      scrollTop: { writable: true, value: 100 },
    })
    let messageTop = -20
    message.getBoundingClientRect = () => ({ top: messageTop, bottom: messageTop + 200 }) as DOMRect
    result.current.paneRef.current = pane
    result.current.messageEndRef.current = end
    act(() => result.current.pauseFollowing())
    act(() => result.current.resizeContent(() => { messageTop = 60 }))
    expect(pane.scrollTop).toBe(180)
    act(() => result.current.scrollToBottomImmediately())
    act(() => result.current.resizeContent(() => { messageTop = 80 }))
    expect(pane.scrollTop).toBe(1200)
    unmount()
    pane.remove()
  })

  it.each([0, 1500, 1800])('闲置%s毫秒后调宽保持按钮显隐和计时，用户再次滚动仍可显示', (idle) => {
    const { result, unmount } = renderHook(() => useConversationScroll({ conversation, isRunning: false }))
    const pane = document.createElement('section')
    const content = document.createElement('div')
    const message = document.createElement('article')
    const end = document.createElement('div')
    content.append(message, end)
    pane.append(content)
    document.body.append(pane)
    Object.defineProperties(pane, {
      clientHeight: { value: 400 }, scrollHeight: { value: 2000 },
      scrollTop: { writable: true, value: 100 },
    })
    let messageTop = -20
    message.getBoundingClientRect = () => ({ top: messageTop, bottom: messageTop + 200 }) as DOMRect
    result.current.paneRef.current = pane
    result.current.messageEndRef.current = end
    act(() => {
      result.current.markUserScrollIntent()
      result.current.handleScroll(pane)
    })
    act(() => vi.advanceTimersByTime(0))
    expect(result.current.showScrollToBottom).toBe(true)
    act(() => vi.advanceTimersByTime(idle))
    const before = { show: result.current.showScrollToBottom, fade: result.current.fadeScrollToBottom }
    act(() => {
      result.current.resizeContent(() => { messageTop = 60 })
      result.current.handleScroll(pane)
    })
    act(() => vi.advanceTimersByTime(0))
    expect(pane.scrollTop).toBe(180)
    expect(result.current.showScrollToBottom).toBe(before.show)
    expect(result.current.fadeScrollToBottom).toBe(before.fade)
    act(() => vi.advanceTimersByTime(1800 - idle))
    expect(result.current.showScrollToBottom).toBe(false)
    act(() => {
      result.current.markUserScrollIntent()
      pane.scrollTop = 220
      result.current.handleScroll(pane)
    })
    act(() => vi.advanceTimersByTime(0))
    expect(result.current.showScrollToBottom).toBe(true)
    unmount()
    pane.remove()
  })

  it('接收结束后正文增长仍跟随，用户阅读历史时不抢滚动，卸载后释放观察', () => {
    let resized = () => {}
    const disconnect = vi.fn()
    vi.stubGlobal('ResizeObserver', class {
      constructor(callback: () => void) { resized = callback }
      observe() {}
      disconnect = disconnect
    })
    const { result, rerender, unmount } = renderHook(({ currentConversation }) => useConversationScroll({
      conversation: currentConversation,
      isRunning: false,
    }), { initialProps: { currentConversation: conversation } })
    const pane = document.createElement('section')
    const content = document.createElement('div')
    const end = document.createElement('div')
    content.append(end)
    pane.append(content)
    Object.defineProperties(pane, {
      clientHeight: { configurable: true, value: 400 },
      scrollHeight: { configurable: true, value: 1200 },
      scrollTop: { configurable: true, writable: true, value: 800 },
    })
    result.current.paneRef.current = pane
    result.current.messageEndRef.current = end
    rerender({ currentConversation: { ...conversation, messages: [{ id: 'reply', role: 'assistant', content: '正文', createdAt: '' }] } })
    act(() => resized())
    expect(pane.scrollTop).toBe(1200)
    act(() => result.current.pauseFollowing())
    pane.scrollTop = 100
    act(() => resized())
    expect(pane.scrollTop).toBe(100)
    unmount()
    expect(disconnect).toHaveBeenCalledOnce()
  })

  it('定位历史时不会因窗口暂时接近底部恢复跟随，返回底部后恢复', () => {
    const { result, rerender, unmount } = renderHook(({ currentConversation }) => useConversationScroll({
      conversation: currentConversation,
      isRunning: true,
    }), { initialProps: { currentConversation: conversation } })
    const pane = document.createElement('section')
    Object.defineProperties(pane, {
      clientHeight: { configurable: true, value: 400 },
      scrollHeight: { configurable: true, value: 1200 },
      scrollTop: { configurable: true, writable: true, value: 800 },
    })
    result.current.paneRef.current = pane
    pane.scrollTo = vi.fn()
    act(() => {
      result.current.pauseFollowing()
      result.current.handleScroll(pane)
      vi.advanceTimersByTime(20)
    })
    pane.scrollTop = 100
    rerender({ currentConversation: { ...conversation, messages: [{ id: 'stream', role: 'assistant', content: '继续流式回答', createdAt: '' }] } })
    act(() => vi.advanceTimersByTime(20))
    expect(pane.scrollTop).toBe(100)
    act(() => result.current.syncToBottomIfFollowing())
    expect(pane.scrollTop).toBe(100)
    act(() => result.current.scrollToBottom())
    act(() => result.current.syncToBottomIfFollowing())
    expect(pane.scrollTop).toBe(1200)
    unmount()
  })

  it('resets idle timing, fades after 1500ms, hides after 1800ms, and pauses while hovered', () => {
    const { result } = renderHook(() => useConversationScroll({ conversation, isRunning: false }))
    const pane = document.createElement('section')
    Object.defineProperties(pane, {
      clientHeight: { configurable: true, value: 400 },
      scrollHeight: { configurable: true, value: 1200 },
      scrollTop: { configurable: true, writable: true, value: 100 },
    })

    act(() => {
      result.current.markUserScrollIntent()
      result.current.handleScroll(pane)
    })
    act(() => vi.advanceTimersByTime(0))
    expect(result.current.showScrollToBottom).toBe(true)
    expect(result.current.fadeScrollToBottom).toBe(false)

    act(() => vi.advanceTimersByTime(1000))
    act(() => {
      result.current.markUserScrollIntent()
      result.current.handleScroll(pane)
    })
    act(() => vi.advanceTimersByTime(0))
    act(() => vi.advanceTimersByTime(1499))
    expect(result.current.fadeScrollToBottom).toBe(false)
    act(() => vi.advanceTimersByTime(1))
    expect(result.current.fadeScrollToBottom).toBe(true)
    act(() => vi.advanceTimersByTime(300))
    expect(result.current.showScrollToBottom).toBe(false)

    act(() => {
      result.current.markUserScrollIntent()
      result.current.handleScroll(pane)
    })
    act(() => vi.advanceTimersByTime(0))
    act(() => result.current.pauseScrollToBottomFade())
    act(() => vi.advanceTimersByTime(5000))
    expect(result.current.showScrollToBottom).toBe(true)
    expect(result.current.fadeScrollToBottom).toBe(false)

    act(() => result.current.resumeScrollToBottomFade())
    act(() => vi.advanceTimersByTime(1500))
    expect(result.current.fadeScrollToBottom).toBe(true)
  })

  it('shows again for either scroll direction after idle hide until reaching the bottom', () => {
    const { result } = renderHook(() => useConversationScroll({ conversation, isRunning: false }))
    const pane = document.createElement('section')
    Object.defineProperties(pane, {
      clientHeight: { configurable: true, value: 400 },
      scrollHeight: { configurable: true, value: 1200 },
      scrollTop: { configurable: true, writable: true, value: 200 },
    })

    act(() => {
      result.current.markUserScrollIntent()
      result.current.handleScroll(pane)
    })
    act(() => vi.advanceTimersByTime(0))
    expect(result.current.showScrollToBottom).toBe(true)

    act(() => vi.advanceTimersByTime(1800))
    expect(result.current.showScrollToBottom).toBe(false)

    pane.scrollTop = 300
    act(() => {
      result.current.markUserScrollIntent()
      result.current.handleScroll(pane)
    })
    act(() => vi.advanceTimersByTime(0))
    expect(result.current.showScrollToBottom).toBe(true)

    act(() => vi.advanceTimersByTime(1800))
    expect(result.current.showScrollToBottom).toBe(false)
    pane.scrollTop = 800
    act(() => {
      result.current.markUserScrollIntent()
      result.current.handleScroll(pane)
    })
    act(() => vi.advanceTimersByTime(0))
    expect(result.current.showScrollToBottom).toBe(false)

    pane.scrollTop = 600
    act(() => {
      result.current.markUserScrollIntent()
      result.current.handleScroll(pane)
    })
    act(() => vi.advanceTimersByTime(0))
    expect(result.current.showScrollToBottom).toBe(true)
  })

  it('keeps the scroll action visible while it owns keyboard focus', () => {
    const { result } = renderHook(() => useConversationScroll({ conversation, isRunning: false }))
    const pane = document.createElement('section')
    Object.defineProperties(pane, {
      clientHeight: { configurable: true, value: 400 },
      scrollHeight: { configurable: true, value: 1200 },
      scrollTop: { configurable: true, writable: true, value: 100 },
    })

    act(() => {
      result.current.markUserScrollIntent()
      result.current.handleScroll(pane)
    })
    act(() => vi.advanceTimersByTime(0))
    act(() => result.current.focusScrollToBottom())
    act(() => vi.advanceTimersByTime(5000))
    expect(result.current.showScrollToBottom).toBe(true)
    expect(result.current.fadeScrollToBottom).toBe(false)

    act(() => result.current.blurScrollToBottom())
    act(() => vi.advanceTimersByTime(1500))
    expect(result.current.fadeScrollToBottom).toBe(true)
  })

  it('把回到底部操作的焦点交给对话区域后再隐藏按钮', () => {
    const { result } = renderHook(() => useConversationScroll({ conversation, isRunning: false }))
    const pane = document.createElement('section')
    pane.tabIndex = 0
    pane.scrollTo = vi.fn()
    Object.defineProperty(pane, 'scrollHeight', { configurable: true, value: 1200 })
    document.body.append(pane)
    const action = document.createElement('button')
    document.body.append(action)
    action.focus()
    result.current.paneRef.current = pane

    act(() => result.current.scrollToBottom())

    expect(pane).toHaveFocus()
    expect(pane.scrollTo).toHaveBeenCalledWith({ top: 1200, behavior: 'smooth' })
    expect(result.current.showScrollToBottom).toBe(false)
    act(() => vi.runOnlyPendingTimers())
    pane.remove()
    action.remove()
  })

  it('每帧合并滚动持久化，并在会话切换和卸载时提交最后位置', () => {
    const secondConversation = { ...conversation, threadId: 'thread-scroll-second' }
    const { result, rerender, unmount } = renderHook(
      ({ currentConversation }) => useConversationScroll({
        conversation: currentConversation,
        isRunning: false,
      }),
      { initialProps: { currentConversation: conversation } },
    )
    const pane = document.createElement('section')
    Object.defineProperties(pane, {
      clientHeight: { configurable: true, value: 400 },
      scrollHeight: { configurable: true, value: 1200 },
      scrollTop: { configurable: true, writable: true, value: 100 },
    })

    act(() => result.current.handleScroll(pane))
    pane.scrollTop = 180
    act(() => result.current.handleScroll(pane))
    expect(window.sessionStorage.getItem('tinkerfin:conversation-scroll:thread-scroll')).toBeNull()

    rerender({ currentConversation: secondConversation })
    expect(storedScroll('thread-scroll')).toEqual({ scrollTop: 180, followLatest: true })

    pane.scrollTop = 260
    act(() => result.current.handleScroll(pane))
    unmount()
    expect(storedScroll('thread-scroll-second')).toEqual({
      scrollTop: 260,
      followLatest: true,
    })
  })

  it('页面进入后台前提交尚未执行的滚动写入', () => {
    const { result } = renderHook(() => useConversationScroll({ conversation, isRunning: false }))
    const pane = document.createElement('section')
    Object.defineProperties(pane, {
      clientHeight: { configurable: true, value: 400 },
      scrollHeight: { configurable: true, value: 1200 },
      scrollTop: { configurable: true, writable: true, value: 320 },
    })

    act(() => result.current.handleScroll(pane))
    act(() => window.dispatchEvent(new Event('pagehide')))

    expect(storedScroll('thread-scroll')).toEqual({ scrollTop: 320, followLatest: true })
    act(() => vi.runOnlyPendingTimers())
  })

  it('restores an off-bottom conversation position without treating it as user scrolling', () => {
    const firstConversation = { ...conversation, threadId: 'thread-first' }
    const secondConversation = { ...conversation, threadId: 'thread-second' }
    setStoredScroll('thread-second', { scrollTop: 240, followLatest: false })
    const { result, rerender } = renderHook(
      ({ currentConversation }) => useConversationScroll({
        conversation: currentConversation,
        isRunning: false,
      }),
      { initialProps: { currentConversation: firstConversation } },
    )
    const pane = document.createElement('section')
    Object.defineProperties(pane, {
      clientHeight: { configurable: true, value: 400 },
      scrollHeight: { configurable: true, value: 1200 },
      scrollTop: { configurable: true, writable: true, value: 0 },
    })
    result.current.paneRef.current = pane

    rerender({ currentConversation: secondConversation })
    act(() => vi.advanceTimersByTime(0))

    expect(pane.scrollTop).toBe(240)
    expect(result.current.showScrollToBottom).toBe(false)

    pane.scrollTop = 120
    act(() => {
      result.current.markUserScrollIntent()
      result.current.handleScroll(pane)
    })
    act(() => vi.advanceTimersByTime(0))
    expect(result.current.showScrollToBottom).toBe(true)
  })

  it('restores the exact reading position after the conversation viewport remounts', () => {
    const { result, rerender, unmount } = renderHook(
      ({ active }) => useConversationScroll({ conversation, isRunning: false, active }),
      { initialProps: { active: true } },
    )
    const firstPane = document.createElement('section')
    Object.defineProperties(firstPane, {
      clientHeight: { configurable: true, value: 400 },
      scrollHeight: { configurable: true, value: 1200 },
      scrollTop: { configurable: true, writable: true, value: 260 },
    })
    result.current.paneRef.current = firstPane

    act(() => {
      result.current.markUserScrollIntent()
      result.current.handleScroll(firstPane)
    })
    rerender({ active: false })
    expect(storedScroll('thread-scroll')).toEqual({ scrollTop: 260, followLatest: false })

    result.current.paneRef.current = null
    const remountedPane = document.createElement('section')
    Object.defineProperties(remountedPane, {
      clientHeight: { configurable: true, value: 400 },
      scrollHeight: { configurable: true, value: 1200 },
      scrollTop: { configurable: true, writable: true, value: 0 },
    })
    result.current.paneRef.current = remountedPane
    rerender({ active: true })

    expect(remountedPane.scrollTop).toBe(260)
    act(() => vi.runOnlyPendingTimers())
    unmount()
  })

  it('跨会话返回时让原本位于底部的会话继续跟随新增内容', () => {
    const secondConversation = { ...conversation, threadId: 'thread-scroll-second' }
    const { result, rerender } = renderHook(
      ({ currentConversation }) => useConversationScroll({
        conversation: currentConversation,
        isRunning: false,
      }),
      { initialProps: { currentConversation: conversation } },
    )
    const pane = document.createElement('section')
    Object.defineProperties(pane, {
      clientHeight: { configurable: true, value: 400 },
      scrollHeight: { configurable: true, value: 1200 },
      scrollTop: { configurable: true, writable: true, value: 800 },
    })
    result.current.paneRef.current = pane
    act(() => result.current.handleScroll(pane))
    act(() => vi.advanceTimersByTime(0))

    rerender({ currentConversation: secondConversation })
    expect(storedScroll('thread-scroll')).toEqual({ scrollTop: 800, followLatest: true })

    Object.defineProperties(pane, {
      scrollHeight: { configurable: true, value: 1500 },
      scrollTop: { configurable: true, writable: true, value: 0 },
    })
    rerender({ currentConversation: conversation })
    act(() => vi.advanceTimersByTime(0))

    expect(pane.scrollTop).toBe(1500)
    expect(result.current.showScrollToBottom).toBe(false)
  })

  it('程序性滚动进入底部阈值后跨会话继续跟随新增内容', () => {
    const otherConversation = { ...conversation, threadId: 'thread-scroll-other' }
    setStoredScroll('thread-scroll', { scrollTop: 240, followLatest: false })
    const { result, rerender } = renderHook(
      ({ currentConversation }) => useConversationScroll({
        conversation: currentConversation,
        isRunning: false,
      }),
      { initialProps: { currentConversation: otherConversation } },
    )
    const pane = document.createElement('section')
    Object.defineProperties(pane, {
      clientHeight: { configurable: true, value: 400 },
      scrollHeight: { configurable: true, value: 1200 },
      scrollTop: { configurable: true, writable: true, value: 0 },
    })
    result.current.paneRef.current = pane

    rerender({ currentConversation: conversation })
    act(() => vi.advanceTimersByTime(0))
    expect(pane.scrollTop).toBe(240)

    pane.scrollTop = 800
    act(() => result.current.handleScroll(pane))
    act(() => vi.advanceTimersByTime(0))
    rerender({ currentConversation: otherConversation })
    expect(storedScroll('thread-scroll')).toEqual({ scrollTop: 800, followLatest: true })

    Object.defineProperties(pane, {
      scrollHeight: { configurable: true, value: 1600 },
      scrollTop: { configurable: true, writable: true, value: 0 },
    })
    rerender({ currentConversation: conversation })
    act(() => vi.advanceTimersByTime(0))

    expect(pane.scrollTop).toBe(1600)
    expect(result.current.showScrollToBottom).toBe(false)
  })

  it('continues following the latest message after an inactive viewport remount', () => {
    const { result, rerender, unmount } = renderHook(
      ({ active, currentConversation }) => useConversationScroll({
        conversation: currentConversation,
        isRunning: false,
        active,
      }),
      { initialProps: { active: true, currentConversation: conversation } },
    )
    const firstPane = document.createElement('section')
    Object.defineProperties(firstPane, {
      clientHeight: { configurable: true, value: 400 },
      scrollHeight: { configurable: true, value: 1200 },
      scrollTop: { configurable: true, writable: true, value: 800 },
    })
    result.current.paneRef.current = firstPane
    act(() => result.current.handleScroll(firstPane))
    act(() => vi.advanceTimersByTime(0))

    rerender({ active: false, currentConversation: conversation })
    result.current.paneRef.current = null
    const remountedPane = document.createElement('section')
    Object.defineProperties(remountedPane, {
      clientHeight: { configurable: true, value: 400 },
      scrollHeight: { configurable: true, value: 1400 },
      scrollTop: { configurable: true, writable: true, value: 0 },
    })
    result.current.paneRef.current = remountedPane
    rerender({
      active: true,
      currentConversation: {
        ...conversation,
        messages: [{
          id: 'message-while-trace-open',
          role: 'assistant',
          content: '新增消息',
          createdAt: '2026-08-24T00:00:01Z',
        }],
      },
    })

    expect(remountedPane.scrollTop).toBe(1000)
    act(() => vi.runOnlyPendingTimers())
    unmount()
  })

  it('does not stop following when wheel or touch intent cannot move past the bottom', () => {
    const { result } = renderHook(() => useConversationScroll({ conversation, isRunning: false }))
    const pane = document.createElement('section')
    Object.defineProperties(pane, {
      clientHeight: { configurable: true, value: 400 },
      scrollHeight: { configurable: true, value: 1200 },
      scrollTop: { configurable: true, writable: true, value: 800 },
    })
    result.current.paneRef.current = pane
    act(() => result.current.handleScroll(pane))
    act(() => vi.advanceTimersByTime(0))

    act(() => result.current.markUserScrollIntent())
    Object.defineProperty(pane, 'scrollHeight', { configurable: true, value: 1400 })
    act(() => result.current.syncToBottomIfFollowing())

    expect(pane.scrollTop).toBe(1400)
  })

  it('布局缩小会话视口时保持底部跟随，只有用户滚动才能退出', () => {
    const { result } = renderHook(() => useConversationScroll({ conversation, isRunning: false }))
    const pane = document.createElement('section')
    Object.defineProperties(pane, {
      clientHeight: { configurable: true, value: 400 },
      scrollHeight: { configurable: true, value: 1200 },
      scrollTop: { configurable: true, writable: true, value: 800 },
    })
    result.current.paneRef.current = pane

    act(() => result.current.handleScroll(pane))
    act(() => vi.advanceTimersByTime(0))
    Object.defineProperty(pane, 'clientHeight', { configurable: true, value: 200 })
    act(() => result.current.handleScroll(pane))
    act(() => vi.advanceTimersByTime(0))
    act(() => result.current.syncToBottomIfFollowing())
    expect(pane.scrollTop).toBe(1200)

    pane.scrollTop = 600
    act(() => {
      result.current.markUserScrollIntent()
      result.current.handleScroll(pane)
    })
    act(() => vi.advanceTimersByTime(0))
    act(() => result.current.syncToBottomIfFollowing())
    expect(pane.scrollTop).toBe(600)
  })

  it('待审批出现时仅一次滚到底部，之后保留用户手动位置', () => {
    const { result, rerender } = renderHook(
      ({ currentConversation }) => useConversationScroll({
        conversation: currentConversation,
        isRunning: false,
      }),
      { initialProps: { currentConversation: conversation } },
    )
    const pane = document.createElement('section')
    Object.defineProperties(pane, {
      clientHeight: { configurable: true, value: 400 },
      scrollHeight: { configurable: true, value: 1200 },
      scrollTop: { configurable: true, writable: true, value: 180 },
    })
    result.current.paneRef.current = pane
    setStoredScroll('thread-scroll', { scrollTop: 180, followLatest: false })

    const approvalConversation: Conversation = {
      ...conversation,
      runStatus: 'waiting_approval',
      approval: pendingApproval,
    }
    rerender({ currentConversation: approvalConversation })
    act(() => vi.advanceTimersByTime(0))
    expect(pane.scrollTop).toBe(1200)

    pane.scrollTop = 260
    act(() => {
      result.current.markUserScrollIntent()
      result.current.handleScroll(pane)
    })
    act(() => vi.advanceTimersByTime(0))

    rerender({
      currentConversation: {
        ...approvalConversation,
        messages: [{
          id: 'approval-tail-update',
          role: 'process',
          content: '',
          createdAt: '2026-08-24T00:00:01Z',
        }],
      },
    })
    act(() => vi.advanceTimersByTime(0))
    expect(pane.scrollTop).toBe(260)
  })
})
