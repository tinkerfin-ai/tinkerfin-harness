import { useCallback, useEffect, useReducer, useRef, type Dispatch, type SetStateAction } from 'react'

import type { WorkspaceState } from '../../types'
import {
  createWorkspaceCacheState,
  reduceWorkspaceCache,
  type ComposerPreferences,
} from './workspaceDetailCache'

export type { ComposerPreferences } from './workspaceDetailCache'

export type RetainConversationDetails = (threadId: string) => {
  moveTo: (threadId: string) => void
  release: () => void
}

export type AcknowledgeComposerPreferences = (threadId: string, submitted: ComposerPreferences) => void

/** 当前页面统一管理详情缓存与本地选择，退出工作台后由组件所有权一并释放 */
export function useWorkspaceState() {
  const [state, dispatch] = useReducer(reduceWorkspaceCache, undefined, createWorkspaceCacheState)
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])

  const setWorkspace: Dispatch<SetStateAction<WorkspaceState>> = useCallback(update => {
    if (mounted.current) dispatch({ type: 'update', update })
  }, [])

  const retainConversationDetails: RetainConversationDetails = useCallback(threadId => {
    const token = Symbol('conversation-details')
    let released = !mounted.current
    if (!released) dispatch({ type: 'retain', token, threadId })
    return {
      moveTo: nextThreadId => {
        if (!released && mounted.current) dispatch({ type: 'move', token, threadId: nextThreadId })
      },
      release: () => {
        if (released) return
        released = true
        if (mounted.current) dispatch({ type: 'release', token })
      },
    }
  }, [])

  const setComposerPreference = useCallback((threadId: string, patch: Partial<ComposerPreferences>) => {
    if (mounted.current) dispatch({ type: 'preferences', threadId, patch })
  }, [])

  const acknowledgeComposerPreferences: AcknowledgeComposerPreferences = useCallback((threadId, submitted) => {
    if (mounted.current) dispatch({ type: 'accepted', threadId, submitted })
  }, [])

  return {
    workspace: state.workspace,
    setWorkspace,
    retainConversationDetails,
    setComposerPreference,
    acknowledgeComposerPreferences,
  }
}
