import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'

import type { WebTaskTraceViewState } from '../../../types'
import { restoreFocus } from '../../../components/ui/focus'

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
      restoreFocus(launcherRef.current, { preventScroll: true })
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

  const close = useCallback((shouldRestoreFocus = true) => {
    if (threadId) writePreference(threadId, false)
    setDesiredOpen(false)
    if (shouldRestoreFocus) {
      window.requestAnimationFrame(() => restoreFocus(launcherRef.current, { preventScroll: true }))
    }
  }, [threadId])

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
