import { useCallback, useRef, type Dispatch, type SetStateAction } from 'react'
import { useI18n } from '../../../i18n'
import { conversationErrorMessage } from '../../../api/conversation/errors'
import { updateConversation } from '../../../lib/workspace'
import type { Conversation, WorkspaceState } from '../../../types'
import { createRunId } from '../agui/runtime'
import type { ConversationStreamController } from '../stream/useConversationStreamController'
import { beginCompaction } from './state'

/** 压缩只提交会话和模型；草稿及附件继续由输入框持有 */
export function useContextCompaction({ conversation, setWorkspace, streamRun, isActiveThread, onNotice }: {
  conversation: Conversation
  setWorkspace: Dispatch<SetStateAction<WorkspaceState>>
  streamRun: ConversationStreamController['streamRun']
  isActiveThread: ConversationStreamController['isActiveThread']
  onNotice: (message: string) => void
}) {
  const { t } = useI18n()
  const pending = useRef(new Set<string>())
  const plan = conversation.serverState?.tinkerfin_plan
  const waitingForPlan = plan != null && typeof plan === 'object' && 'status' in plan && plan.status === 'awaiting_input'
  const disabledReason = conversation.runStatus === 'streaming' || conversation.runStatus === 'detached'
    || conversation.pendingInteractionKind || conversation.approval || conversation.planInteraction || waitingForPlan
    ? t('完成当前任务或交互后可压缩')
    : undefined
  const execute = useCallback(() => {
    const threadId = conversation.threadId
    if (disabledReason || isActiveThread(threadId) || pending.current.has(threadId)) return false
    if (!threadId || conversation.messages.length === 0) {
      onNotice(t('暂无可压缩的历史'))
      return true
    }
    if (!conversation.isHydrated) return false
    const runId = createRunId()
    pending.current.add(threadId)
    setWorkspace(state => updateConversation(state, threadId, item => beginCompaction(item, runId)))
    void streamRun(threadId, { threadId, runId, model: conversation.model }, 'compact', { target: 'workspace' })
      .catch(error => {
        setWorkspace(state => updateConversation(state, threadId, item => item.activeRunId !== runId ? item : {
          ...item,
          activeRunId: undefined,
          runStatus: 'error',
          compactions: item.compactions?.map(operation => operation.runId === runId ? { ...operation, status: 'failed' } : operation),
          notice: { id: `${runId}:request`, kind: 'error', content: conversationErrorMessage(error, 'run_request_failed') },
        }))
      })
      .finally(() => pending.current.delete(threadId))
    return true
  }, [conversation, disabledReason, isActiveThread, onNotice, setWorkspace, streamRun, t])
  const active = conversation.compactions?.find(operation => operation.runId === conversation.activeRunId)
  return { execute, disabledReason, saving: active?.status === 'saving' }
}
