import { useEffect, useRef, useState } from 'react'
import { fetchConversationHistoryDetail } from '../../api/conversation/history'
import { watchResource } from '../../api/shared/watchResource'
import type { Conversation } from '../../types'
import { applyInteractionConfirmation, interactionInterruptIds } from './interactionConfirmation'

/** 独立跟随已提交决定的保存结果，生命周期归属当前可见会话 */
export function useInteractionConfirmation(
  conversation: Conversation,
  update: (threadId: string, updater: (current: Conversation) => Conversation) => void,
) {
  const form = conversation.approval ?? conversation.planInteraction
  const submissionRunId = form?.submitted ? form.submissionRunId : undefined
  const requestRejected = form?.requestRejected === true
  const interruptIds = interactionInterruptIds(conversation)
  const latestInterruptIds = useRef(interruptIds)
  latestInterruptIds.current = interruptIds
  const threadId = conversation.threadId
  const key = submissionRunId ? JSON.stringify([threadId, submissionRunId, requestRejected, interruptIds]) : ''
  const latestUpdate = useRef(update)
  latestUpdate.current = update
  const watcher = useRef<ReturnType<typeof watchResource> | null>(null)
  const [status, setStatus] = useState({ key: '', failed: false, checking: false })
  useEffect(() => {
    if (!threadId || !submissionRunId) return
    const submittedIds = latestInterruptIds.current
    const current = watchResource({
      read: signal => {
        setStatus({ key, failed: false, checking: true })
        return fetchConversationHistoryDetail(threadId, { includeTaskTrace: false, submissionRunId, signal, suppressGlobalError: true })
      },
      update: detail => {
        setStatus({ key, failed: false, checking: false })
        latestUpdate.current(threadId, current => applyInteractionConfirmation(current, detail, submissionRunId,
          requestRejected ? submittedIds : undefined))
      },
      matches: change => change.topic === 'studio.conversation.interactions.changed' && change.key === threadId,
      refreshWhile: detail => detail.submissionResult?.state !== 'not_saved'
        && submittedIds.some(id => !detail.interactionAvailability.some(item => item.interruptId === id
          && item.submissionRunId === submissionRunId && (item.state === 'resolved' || item.state === 'cancelled'))),
      onError: () => setStatus({ key, failed: true, checking: false }),
    })
    watcher.current = current
    return () => { current.close(); if (watcher.current === current) watcher.current = null }
  // 明确拒绝后重新建立读取，拒绝前在途的空闲权限不能开放下一次操作
  }, [key, submissionRunId, threadId, requestRejected])
  return {
    failed: status.key === key && status.failed,
    checking: status.key === key && status.checking,
    retry: () => watcher.current?.refresh(),
  }
}
