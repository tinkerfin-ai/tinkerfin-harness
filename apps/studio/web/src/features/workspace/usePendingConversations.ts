import { useCallback, useState } from 'react'
import type { Conversation } from '../../types'

export interface PendingConversation {
  runId: string
  conversation: Conversation
}

/** 未受理的提交用运行 ID 定位，不能作为服务端会话 ID 调用接口 */
export function usePendingConversations() {
  const [items, setItems] = useState<PendingConversation[]>([])
  const [selectedRunId, select] = useState<string | null>(null)
  const update = useCallback((runId: string, conversation: Conversation) => {
    setItems(current => current.some(item => item.runId === runId)
      ? current.map(item => item.runId === runId ? { runId, conversation } : item)
      : [{ runId, conversation }, ...current])
  }, [])
  const remove = useCallback((runId: string) => {
    setItems(current => current.filter(item => item.runId !== runId))
    select(current => current === runId ? null : current)
  }, [])
  return { items, selectedRunId, select, update, remove }
}
