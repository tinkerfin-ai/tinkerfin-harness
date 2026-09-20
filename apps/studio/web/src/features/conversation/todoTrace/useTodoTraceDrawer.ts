import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'

import type { WebTaskTraceViewState } from '../../../types'

const PREFERENCE_PREFIX = 'tinkerfin:todo-trace-drawer:'
const preferenceKey = (threadId: string) => `${PREFERENCE_PREFIX}${threadId}`

const readPreference = (threadId: string) => {
  try {
    return window.sessionStorage.getItem(preferenceKey(threadId)) === 'open'
  } catch {
    return false
  }
}

const writePreference = (threadId: string, open: boolean) => {
  try {
    window.sessionStorage.setItem(preferenceKey(threadId), open ? 'open' : 'closed')
  } catch {
    // 禁用会话存储时仍保留当前页面内的用户选择
  }
}

export function useTodoTraceDrawer({
  threadId,
  taskTrace,
  blocked,
  available,
}: {
  threadId: string
  taskTrace: WebTaskTraceViewState
  blocked: boolean
  available: boolean
}) {
  const [desiredOpen, setDesiredOpen] = useState(false)
  const [openEpoch, setOpenEpoch] = useState(0)
  const launcherRef = useRef<HTMLButtonElement>(null)
  const drawerRef = useRef<HTMLElement>(null)
  const hasGroups = taskTrace.phase === 'ready' && taskTrace.snapshot.todoGroups.length > 0
  const open = desiredOpen && available && !blocked && hasGroups

  useEffect(() => {
    setDesiredOpen(threadId ? readPreference(threadId) : false)
  }, [threadId])

  useLayoutEffect(() => {
    if (!open && drawerRef.current?.contains(document.activeElement)) {
      launcherRef.current?.focus({ preventScroll: true })
    }
  }, [open])

  const toggle = useCallback(() => {
    setDesiredOpen((current) => {
      const next = available ? !current : true
      if (threadId) writePreference(threadId, next)
      if (next) setOpenEpoch((value) => value + 1)
      return next
    })
  }, [available, threadId])

  const close = useCallback((restoreFocus = true) => {
    if (threadId) writePreference(threadId, false)
    setDesiredOpen(false)
    if (restoreFocus) {
      window.requestAnimationFrame(() => launcherRef.current?.focus({ preventScroll: true }))
    }
  }, [threadId])

  useEffect(() => {
    if (!open) return
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented) return
      if (event.key === 'Escape') {
        event.preventDefault()
        close(true)
        return
      }
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [close, open])

  return {
    open,
    desiredOpen,
    openEpoch,
    launcherRef,
    drawerRef,
    toggle,
    close,
  }
}
