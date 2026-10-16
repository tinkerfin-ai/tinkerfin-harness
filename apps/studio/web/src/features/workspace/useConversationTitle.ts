import { useEffect, type Dispatch, type SetStateAction } from 'react'
import { fetchConversationTitle } from '../../api/conversation/titles'
import { watchResource } from '../../api/shared/watchResource'
import { mergeConversationTitle, updateConversation } from '../../lib/workspace'
import type { WorkspaceState } from '../../types'

/** 仅为列表窗口外的当前会话读取标题；列表内标题由历史摘要统一更新 */
export function useConversationTitle(
  threadId: string | undefined,
  setWorkspace: Dispatch<SetStateAction<WorkspaceState>>,
) {
  useEffect(() => {
    if (!threadId) return
    const watch = watchResource({
      matches: change => change.topic === 'studio.conversation.title.changed' && change.key === threadId,
      read: signal => fetchConversationTitle(threadId, signal),
      refreshWhile: title => title.titleGenerationStatus === 'running',
      update: (title, signal) => setWorkspace(state => {
        if (signal.aborted || state.currentThreadId !== threadId) return state
        const current = state.conversations.find(item => item.threadId === threadId)
        if (!current) return state
        const merged = mergeConversationTitle(current, title)
        if (current.title === merged.title && current.titleSource === merged.titleSource
          && current.titleGenerationStatus === merged.titleGenerationStatus && current.titleSeq === merged.titleSeq) return state
        return updateConversation(state, threadId, item => ({ ...item, ...merged }))
      }),
    })
    return watch.close
  }, [threadId, setWorkspace])
}
