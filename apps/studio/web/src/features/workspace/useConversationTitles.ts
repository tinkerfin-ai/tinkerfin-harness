import { useEffect, useRef, type Dispatch, type SetStateAction } from 'react'
import { fetchConversationTitle } from '../../api/conversation/titles'
import { mergeConversationTitle, updateConversation } from '../../lib/workspace'
import type { Conversation, WorkspaceState } from '../../types'
import { messageText } from '../conversation/attachments/content'

function needsTitle(conversation: Conversation): boolean {
  return Boolean(conversation.threadId && conversation.titleSource === 'default'
    && (conversation.titleGenerationStatus === 'running'
      || (conversation.titleGenerationStatus === 'idle'
        && conversation.messages.some(message => message.role === 'user' && messageText(message.content).trim()))))
}

/** 标题查询属于工作区，不受当前会话选择或主回复结束影响 */
export function useConversationTitles(workspace: WorkspaceState, setWorkspace: Dispatch<SetStateAction<WorkspaceState>>) {
  const requests = useRef(new Map<string, { controller: AbortController; timer?: number }>())
  useEffect(() => {
    const pending = new Set(workspace.conversations.filter(needsTitle).map(item => item.threadId))
    for (const [threadId, request] of requests.current) {
      if (pending.has(threadId)) continue
      request.controller.abort()
      window.clearTimeout(request.timer)
      requests.current.delete(threadId)
    }
    for (const threadId of pending) {
      if (requests.current.has(threadId)) continue
      const request = { controller: new AbortController(), timer: undefined as number | undefined }
      requests.current.set(threadId, request)
      const poll = async () => {
        try {
          const title = await fetchConversationTitle(threadId, request.controller.signal)
          if (request.controller.signal.aborted) return
          setWorkspace(state => updateConversation(state, threadId, item => ({ ...item, ...mergeConversationTitle(item, title) })))
        } catch {
          if (request.controller.signal.aborted) return
        }
        request.timer = window.setTimeout(() => void poll(), 1000)
      }
      void poll()
    }
  }, [workspace.conversations, setWorkspace])

  useEffect(() => {
    const owned = requests.current
    return () => {
      for (const request of owned.values()) {
        request.controller.abort()
        window.clearTimeout(request.timer)
      }
      owned.clear()
    }
  }, [])
}
