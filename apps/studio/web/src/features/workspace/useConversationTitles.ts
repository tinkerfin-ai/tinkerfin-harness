import { useEffect, useRef, type Dispatch, type SetStateAction } from 'react'
import { fetchConversationTitle } from '../../api/conversation/titles'
import { watchResource } from '../../api/shared/watchResource'
import { mergeConversationTitle, updateConversation } from '../../lib/workspace'
import type { Conversation, WorkspaceState } from '../../types'
import { messageText } from '../conversation/attachments/content'

function needsTitle(conversation: Conversation): boolean {
  return Boolean(conversation.threadId && conversation.titleSource === 'default'
    && (conversation.titleGenerationStatus === 'running'
      || (conversation.titleGenerationStatus === 'idle'
        && conversation.messages.some(message => message.role === 'user' && messageText(message.content).trim()))))
}

const sameTitle = (current: Conversation, next: ReturnType<typeof mergeConversationTitle>) => (
  current.title === next.title && current.titleSource === next.titleSource
  && current.titleGenerationStatus === next.titleGenerationStatus && current.titleSeq === next.titleSeq
)

/** 标题查询属于工作区，不受当前会话选择或主回复结束影响 */
export function useConversationTitles(workspace: WorkspaceState, setWorkspace: Dispatch<SetStateAction<WorkspaceState>>) {
  const watches = useRef(new Map<string, ReturnType<typeof watchResource>>())
  const latestConversations = useRef(workspace.conversations)
  latestConversations.current = workspace.conversations

  useEffect(() => {
    const known = new Map(workspace.conversations.filter(item => item.threadId).map(item => [item.threadId, item]))
    for (const [threadId, watch] of watches.current) {
      if (known.has(threadId)) continue
      watch.close()
      watches.current.delete(threadId)
    }
    for (const [threadId, conversation] of known) {
      if (watches.current.has(threadId)) continue
      const watch = watchResource({
        initialRead: needsTitle(conversation),
        repairWhen: () => {
          const current = latestConversations.current.find(item => item.threadId === threadId)
          return Boolean(current && needsTitle(current))
        },
        matches: change => change.topic === 'studio.conversation.title.changed' && change.key === threadId,
        read: signal => fetchConversationTitle(threadId, signal),
        update: (title, signal) => setWorkspace(state => {
          if (signal.aborted) return state
          const current = state.conversations.find(item => item.threadId === threadId)
          if (!current) return state
          const merged = mergeConversationTitle(current, title)
          return sameTitle(current, merged) ? state : updateConversation(state, threadId, item => ({ ...item, ...merged }))
        }),
      })
      watches.current.set(threadId, watch)
    }
  }, [workspace.conversations, setWorkspace])

  useEffect(() => {
    const owned = watches.current
    return () => { for (const watch of owned.values()) watch.close(); owned.clear() }
  }, [])
}
