import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { RefObject } from 'react'

import type { ConversationDisplayEntry } from '../conversation/todoTrace/displayEntries'

const MESSAGE_RENDER_BATCH_SIZE = 100
const LOCATE_CONTEXT_BEFORE = 20

export type MessageLocateResult = 'found' | 'not-found' | 'failed' | 'cancelled'

const entryMessageId = (entry: ConversationDisplayEntry) => (
  entry.type === 'compaction' ? `compaction:${entry.operation.runId}` : entry.type === 'run-failure' ? `failure:${entry.failure.runId}` : entry.type === 'tools'
    ? entry.messages[0]?.id
    : entry.message.id
)

// 任务卡与工具批次共用底层消息标识，任务详情晚于正文加载也能恢复位置
const entryKey = (entry: ConversationDisplayEntry) => entry.type === 'run-failure'
  ? `failure:${entry.failure.runId}`
  : `message:${entryMessageId(entry)}`

/** 页面内只保留阅读锚点，不持有消息、快照或 DOM 引用 */
type ReadingBookmark = { entryKey: string | null; offset: number; followLatest: boolean }

type ReadingPositionOptions = {
  isHydrated: boolean
  threadIds: readonly string[]
  isFollowingLatest: () => boolean
  pauseFollowing: () => void
  scrollToBottom: () => void
  onMissing: () => void
}


const waitForFrame = (signal: AbortSignal) => new Promise<boolean>((resolve) => {
  if (signal.aborted) {
    resolve(false)
    return
  }
  const frame = window.requestAnimationFrame(() => {
    signal.removeEventListener('abort', abort)
    resolve(true)
  })
  const abort = () => {
    window.cancelAnimationFrame(frame)
    resolve(false)
  }
  signal.addEventListener('abort', abort, { once: true })
})

export function useConversationMessageWindow({
  threadId,
  entries,
  historyCursor,
  paneRef,
  loadOlderTrace,
  active = true,
  readingPosition,
}: {
  threadId: string
  active?: boolean
  readingPosition?: ReadingPositionOptions
  entries: ConversationDisplayEntry[]
  historyCursor?: string | null
  paneRef: RefObject<HTMLElement | null>
  loadOlderTrace: (
    threadId: string,
    options?: { signal?: AbortSignal },
  ) => Promise<boolean>
}) {
  const defaultStart = Math.max(0, entries.length - MESSAGE_RENDER_BATCH_SIZE)
  const [windowState, setWindowState] = useState({
    threadId,
    start: defaultStart,
    end: entries.length,
    followsTail: true,
  })
  const entriesRef = useRef(entries)
  const historyCursorRef = useRef(historyCursor)
  const revisionRef = useRef(0)
  const previousInputs = useRef({ entries, historyCursor })
  const earlierAnchor = useRef<{
    threadId: string
    scrollHeight: number
    scrollTop: number
    trigger: HTMLButtonElement
  } | null>(null)
  const locateController = useRef<AbortController | null>(null)
  const highlightCleanup = useRef<(() => void) | null>(null)
  const bookmarks = useRef(new Map<string, ReadingBookmark>())
  const readingOptions = useRef(readingPosition)
  readingOptions.current = readingPosition
  const pendingBookmark = useRef<ReadingBookmark | null>(null)
  const restoringBookmark = useRef(false)
  const restoreGeneration = useRef(0)
  const [readingPositionFailed, setReadingPositionFailed] = useState(false)


  entriesRef.current = entries
  historyCursorRef.current = historyCursor
  if (
    previousInputs.current.entries !== entries
    || previousInputs.current.historyCursor !== historyCursor
  ) {
    previousInputs.current = { entries, historyCursor }
    revisionRef.current += 1
  }

  const initializesFromEmpty = windowState.threadId === threadId
    && windowState.followsTail
    && windowState.start === 0
    && windowState.end === 0
    && entries.length > 0
  const resolvedWindow = windowState.threadId === threadId
    ? {
        start: initializesFromEmpty
          ? defaultStart
          : Math.min(windowState.start, entries.length),
        end: windowState.followsTail
          ? entries.length
          : Math.min(Math.max(windowState.end, windowState.start), entries.length),
      }
    : { start: defaultStart, end: entries.length }

  const visibleEntries = useMemo(
    () => entries.slice(resolvedWindow.start, resolvedWindow.end),
    [entries, resolvedWindow.end, resolvedWindow.start],
  )

  useLayoutEffect(() => {
    setWindowState((current) => {
      if (current.threadId !== threadId) {
        earlierAnchor.current = null
        return {
          threadId,
          start: defaultStart,
          end: entries.length,
          followsTail: true,
        }
      }
      if (
        current.followsTail
        && current.start === 0
        && current.end === 0
        && entries.length > 0
      ) {
        return {
          ...current,
          start: defaultStart,
          end: entries.length,
        }
      }
      if (current.followsTail && current.end !== entries.length) {
        return { ...current, end: entries.length }
      }
      if (current.start > entries.length) {
        return {
          ...current,
          start: defaultStart,
          end: entries.length,
          followsTail: true,
        }
      }
      return current
    })
  }, [defaultStart, entries.length, threadId])

  useLayoutEffect(() => {
    const anchor = earlierAnchor.current
    if (!anchor || anchor.threadId !== threadId) return
    const pane = paneRef.current
    if (pane) {
      pane.scrollTop = anchor.scrollTop + (pane.scrollHeight - anchor.scrollHeight)
    }
    if (anchor.trigger.isConnected) anchor.trigger.focus({ preventScroll: true })
    else pane?.focus({ preventScroll: true })
    earlierAnchor.current = null
  }, [entries.length, paneRef, resolvedWindow.start, threadId])

  const loadEarlierMessages = useCallback(async (trigger: HTMLButtonElement) => {
    restoreGeneration.current += 1
    locateController.current?.abort()
    pendingBookmark.current = null
    restoringBookmark.current = false
    setReadingPositionFailed(false)
    const pane = paneRef.current
    if (pane) {
      earlierAnchor.current = {
        threadId,
        scrollHeight: pane.scrollHeight,
        scrollTop: pane.scrollTop,
        trigger,
      }
    }
    if (resolvedWindow.start > 0) {
      setWindowState((current) => ({
        ...current,
        threadId,
        start: Math.max(0, resolvedWindow.start - MESSAGE_RENDER_BATCH_SIZE),
      }))
      return
    }
    const loaded = await loadOlderTrace(threadId)
    if (loaded || earlierAnchor.current?.trigger !== trigger) return
    earlierAnchor.current = null
    if (trigger.isConnected) trigger.focus({ preventScroll: true })
  }, [loadOlderTrace, paneRef, resolvedWindow.start, threadId])

  const waitForRevision = useCallback(async (
    previousRevision: number,
    signal: AbortSignal,
  ) => {
    for (let frame = 0; frame < 120; frame += 1) {
      if (revisionRef.current !== previousRevision) return true
      if (!await waitForFrame(signal)) return false
    }
    return false
  }, [])

  const waitForElement = useCallback(async (
    messageId: string,
    signal: AbortSignal,
  ) => {
    for (let frame = 0; frame < 20; frame += 1) {
      const element = document.getElementById(messageId)
      if (element) return element
      if (!await waitForFrame(signal)) return null
    }
    return null
  }, [])

  const highlight = useCallback((target: HTMLElement, alignment: ScrollLogicalPosition) => {
    highlightCleanup.current?.()
    const previousTabIndex = target.getAttribute('tabindex')
    target.setAttribute('tabindex', '-1')
    target.scrollIntoView({
      block: alignment,
      behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches
        ? 'auto'
        : 'smooth',
    })
    target.focus({ preventScroll: true })
    target.classList.add('todo-trace-locate-target')
    const timer = window.setTimeout(() => {
      target.classList.remove('todo-trace-locate-target')
      if (previousTabIndex == null) target.removeAttribute('tabindex')
      else target.setAttribute('tabindex', previousTabIndex)
      highlightCleanup.current = null
    }, 1_000)
    highlightCleanup.current = () => {
      window.clearTimeout(timer)
      target.classList.remove('todo-trace-locate-target')
      if (previousTabIndex == null) target.removeAttribute('tabindex')
      else target.setAttribute('tabindex', previousTabIndex)
      highlightCleanup.current = null
    }
  }, [])

  const locateEntry = useCallback(async (
    messageId: string,
    alignment: ScrollLogicalPosition = 'center',
    bookmark?: ReadingBookmark,
  ): Promise<MessageLocateResult> => {
    locateController.current?.abort()
    const controller = new AbortController()
    locateController.current = controller
    const { signal } = controller
    try {
      while (!signal.aborted) {
        const rendered = bookmark ? null : document.getElementById(messageId)
        if (rendered) {
          highlight(rendered, alignment)
          return 'found'
        }

        const index = entriesRef.current.findIndex(
          (entry) => bookmark
            ? entryKey(entry) === bookmark.entryKey
            : entryMessageId(entry) === messageId
              || (entry.type !== 'tools' && entry.type !== 'compaction' && entry.message.meta?.traceMessageId === messageId),
        )
        if (index >= 0) {
          const renderedId = entryMessageId(entriesRef.current[index]!)
          if (!renderedId) return 'not-found'
          const mappedElement = document.getElementById(renderedId)
          if (mappedElement) {
            if (bookmark) {
              const pane = paneRef.current
              if (pane) pane.scrollTop += mappedElement.getBoundingClientRect().top
                - pane.getBoundingClientRect().top - bookmark.offset
            } else highlight(mappedElement, alignment)
            return 'found'
          }
          const start = Math.max(0, index - LOCATE_CONTEXT_BEFORE)
          setWindowState({
            threadId,
            start,
            end: Math.min(entriesRef.current.length, start + MESSAGE_RENDER_BATCH_SIZE),
            followsTail: false,
          })
          const target = await waitForElement(renderedId, signal)
          if (!target) return signal.aborted ? 'cancelled' : 'failed'
          if (bookmark) {
            const pane = paneRef.current
            if (pane) pane.scrollTop += target.getBoundingClientRect().top
              - pane.getBoundingClientRect().top - bookmark.offset
          } else highlight(target, alignment)
          return 'found'
        }

        if (!historyCursorRef.current) return 'not-found'
        const previousRevision = revisionRef.current
        const loaded = await loadOlderTrace(threadId, { signal })
        if (signal.aborted) return 'cancelled'
        if (!loaded) {
          return historyCursorRef.current ? 'failed' : 'not-found'
        }
        if (!await waitForRevision(previousRevision, signal)) {
          return signal.aborted ? 'cancelled' : 'failed'
        }
      }
      return 'cancelled'
    } finally {
      if (locateController.current === controller) locateController.current = null
    }
  }, [highlight, loadOlderTrace, paneRef, threadId, waitForElement, waitForRevision])

  const restoreTail = useCallback(() => {
    pendingBookmark.current = null
    restoringBookmark.current = false
    setReadingPositionFailed(false)
    locateController.current?.abort()
    const currentEntries = entriesRef.current
    setWindowState({
      threadId,
      start: Math.max(0, currentEntries.length - MESSAGE_RENDER_BATCH_SIZE),
      end: currentEntries.length,
      followsTail: true,
    })
  }, [threadId])

  const captureReadingPosition = useCallback(() => {
    const options = readingOptions.current
    const pane = paneRef.current
    // 分页或水化失败时保留原锚点，不能用临时展示的末页覆盖它
    if (!threadId || !active || !options?.isHydrated || !pane
      || restoringBookmark.current || pendingBookmark.current) return
    const following = options.isFollowingLatest()
    const paneTop = pane.getBoundingClientRect().top
    const anchor = visibleEntries.map(entry => ({
      entry,
      element: document.getElementById(entryMessageId(entry) ?? ''),
    })).find(({ element }) => element && pane.contains(element)
      && element.getBoundingClientRect().bottom > paneTop)
    if (!following && !anchor) return
    bookmarks.current.set(threadId, {
      entryKey: anchor ? entryKey(anchor.entry) : null,
      offset: anchor?.element ? anchor.element.getBoundingClientRect().top - paneTop : 0,
      followLatest: following,
    })
  }, [active, paneRef, threadId, visibleEntries])

  const cancelReadingRestore = useCallback(() => {
    restoreGeneration.current += 1
    locateController.current?.abort()
    pendingBookmark.current = null
    restoringBookmark.current = false
    setReadingPositionFailed(false)
  }, [])

  const revealMessage = useCallback((
    messageId: string,
    alignment: ScrollLogicalPosition = 'center',
  ) => {
    cancelReadingRestore()
    return locateEntry(messageId, alignment)
  }, [cancelReadingRestore, locateEntry])

  const retryReadingPosition = useCallback(async () => {
    const bookmark = pendingBookmark.current
    const options = readingOptions.current
    if (!bookmark || !options?.isHydrated || !active || restoringBookmark.current) return
    setReadingPositionFailed(false)
    if (bookmark.followLatest || !bookmark.entryKey) {
      pendingBookmark.current = null
      restoreTail()
      options.scrollToBottom()
      return
    }
    options.pauseFollowing()
    restoringBookmark.current = true
    const generation = ++restoreGeneration.current
    try {
      const result = await locateEntry('', 'start', bookmark)
      if (restoreGeneration.current !== generation || pendingBookmark.current !== bookmark) return
      if (result === 'found') pendingBookmark.current = null
      else if (result === 'not-found') {
        pendingBookmark.current = null
        bookmarks.current.delete(threadId)
        restoreTail()
        options.scrollToBottom()
        options.onMissing()
      } else if (result === 'failed') setReadingPositionFailed(true)
    } catch {
      if (restoreGeneration.current === generation && pendingBookmark.current === bookmark) setReadingPositionFailed(true)
    } finally {
      if (restoreGeneration.current === generation) {
        restoringBookmark.current = false
      }
    }
  }, [active, locateEntry, restoreTail, threadId])

  useLayoutEffect(() => {
    restoreGeneration.current += 1
    locateController.current?.abort()
    highlightCleanup.current?.()
    restoringBookmark.current = false
    setReadingPositionFailed(false)
    pendingBookmark.current = active ? bookmarks.current.get(threadId) ?? null : null
    return () => {
      locateController.current?.abort()
      pendingBookmark.current = null
      restoringBookmark.current = false
    }
  }, [threadId, active])

  const retryReadingPositionRef = useRef(retryReadingPosition)
  retryReadingPositionRef.current = retryReadingPosition
  useEffect(() => {
    void retryReadingPositionRef.current()
  }, [threadId, active, readingPosition?.isHydrated])

  const knownThreadIds = readingPosition?.threadIds
  useEffect(() => {
    if (!knownThreadIds) return
    const existing = new Set(knownThreadIds)
    for (const savedThreadId of bookmarks.current.keys()) {
      if (!existing.has(savedThreadId)) bookmarks.current.delete(savedThreadId)
    }
  }, [knownThreadIds])

  useEffect(() => {
    window.addEventListener('popstate', captureReadingPosition)
    return () => window.removeEventListener('popstate', captureReadingPosition)
  }, [captureReadingPosition])

  useEffect(() => () => {
    locateController.current?.abort()
    highlightCleanup.current?.()
    bookmarks.current.clear()
  }, [])

  return {
    visibleEntries,
    captureReadingPosition,
    cancelReadingRestore,
    retryReadingPosition,
    readingPositionFailed,
    hasEarlierMessages: resolvedWindow.start > 0 || historyCursor != null,
    followsTail: windowState.threadId !== threadId || windowState.followsTail,
    loadEarlierMessages,
    revealMessage,
    restoreTail,
  }
}
