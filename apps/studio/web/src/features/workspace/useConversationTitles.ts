import { useEffect, useRef, useState, type Dispatch, type SetStateAction } from 'react'
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

const sameTitle = (current: Conversation, next: ReturnType<typeof mergeConversationTitle>) => (
  current.title === next.title && current.titleSource === next.titleSource
  && current.titleGenerationStatus === next.titleGenerationStatus && current.titleSeq === next.titleSeq
)

/** 标题查询属于工作区，不受当前会话选择或主回复结束影响 */
export function useConversationTitles(workspace: WorkspaceState, setWorkspace: Dispatch<SetStateAction<WorkspaceState>>) {
  const [visible, setVisible] = useState(document.visibilityState !== 'hidden')
  const requests = useRef(new Map<string, { controller: AbortController; timer?: number; failures: number }>())
  const latestConversations = useRef(workspace.conversations)
  latestConversations.current = workspace.conversations

  useEffect(() => {
    const updateVisibility = () => setVisible(document.visibilityState !== 'hidden')
    document.addEventListener('visibilitychange', updateVisibility)
    return () => document.removeEventListener('visibilitychange', updateVisibility)
  }, [])

  useEffect(() => {
    const pending = new Set(visible ? workspace.conversations.filter(needsTitle).map(item => item.threadId) : [])
    for (const [threadId, request] of requests.current) {
      if (pending.has(threadId)) continue
      request.controller.abort()
      window.clearTimeout(request.timer)
      requests.current.delete(threadId)
    }
    for (const threadId of pending) {
      if (requests.current.has(threadId)) continue
      const request = { controller: new AbortController(), timer: undefined as number | undefined, failures: 0 }
      requests.current.set(threadId, request)
      const isCurrent = () => !request.controller.signal.aborted && document.visibilityState !== 'hidden'
      const poll = async () => {
        request.timer = undefined
        if (!isCurrent()) return
        try {
          const title = await fetchConversationTitle(threadId, request.controller.signal)
          if (!isCurrent()) return
          request.failures = 0
          const current = latestConversations.current.find(item => item.threadId === threadId)
          if (!current) return
          const merged = mergeConversationTitle(current, title)
          if (!sameTitle(current, merged)) {
            setWorkspace(state => {
              if (!isCurrent()) return state
              const item = state.conversations.find(conversation => conversation.threadId === threadId)
              if (!item) return state
              const nextTitle = mergeConversationTitle(item, title)
              return sameTitle(item, nextTitle) ? state : updateConversation(state, threadId, conversation => ({ ...conversation, ...nextTitle }))
            })
          }
          // 标题终态无需再唤醒定时器；工作区提交后会释放对应查询
          if (!needsTitle({ ...current, ...merged })) return
        } catch {
          if (!isCurrent()) return
          request.failures = Math.min(request.failures + 1, 5)
        }
        request.timer = window.setTimeout(() => void poll(), Math.min(1000 * (2 ** request.failures), 30_000))
      }
      void poll()
    }
  }, [workspace.conversations, setWorkspace, visible])

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
