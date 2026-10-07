import { useEffect, useRef, useState, type Dispatch, type SetStateAction } from 'react'
import { patchConversation } from '../../api/conversation/history'
import type { ToastHandler } from '../../components/ui/ToastViewport'
import { isTranslationKey, useI18n } from '../../i18n'
import type { Conversation, WorkspaceState } from '../../types'

type MoveIntent = { conversation: Conversation; trigger: HTMLElement }
export function useProjectConversationActions(workspace: WorkspaceState, setWorkspace: Dispatch<SetStateAction<WorkspaceState>>, onToast: ToastHandler, validateDraftMove?: (conversation: Conversation) => void) {
  const { t } = useI18n()
  const [moving, setMoving] = useState<MoveIntent | null>(null)
  const [pendingMove, setPendingMove] = useState<MoveIntent | null>(null)
  const [failure, setFailure] = useState<{ intent: MoveIntent; message: string } | null>(null)
  const requests = useRef(new Map<string, AbortController>())
  useEffect(() => { const owned = requests.current; return () => { for (const request of owned.values()) request.abort() } }, [])
  const change = async (target: Conversation, update: { archived?: boolean; projectId?: string }, intent?: MoveIntent) => {
    if (requests.current.has(target.threadId)) return
    const controller = new AbortController(); requests.current.set(target.threadId, controller)
    if (intent) { setPendingMove(intent); setFailure(null) }
    try {
      if (update.projectId) validateDraftMove?.(target)
      await patchConversation(target.threadId, update, controller.signal)
      if (controller.signal.aborted) return
      setWorkspace(current => ({ ...current, conversations: current.conversations.filter(item => item.threadId !== target.threadId), currentThreadId: current.currentThreadId === target.threadId ? '' : current.currentThreadId }))
      if (intent) setMoving(current => current === intent ? null : current)
      onToast('success', t(update.projectId ? '会话已移动' : update.archived ? '会话已归档' : '会话已恢复'))
    } catch (reason) {
      if (controller.signal.aborted) return
      const message = reason instanceof Error ? isTranslationKey(reason.message) ? t(reason.message) : reason.message : t('操作失败，请重试')
      if (intent) setFailure({ intent, message })
      else onToast('error', message)
    } finally {
      requests.current.delete(target.threadId)
      if (intent && !controller.signal.aborted) setPendingMove(current => current === intent ? null : current)
    }
  }
  return {
    moving, pending: moving !== null && pendingMove === moving, error: failure?.intent === moving ? failure.message : '',
    close: () => { if (!pendingMove) setMoving(null) },
    move: (threadId: string, trigger: HTMLElement) => { const conversation = workspace.conversations.find(item => item.threadId === threadId); if (conversation && !pendingMove) { setMoving({ conversation, trigger }); setFailure(null) } },
    confirmMove: (projectId: string) => { if (moving) void change(moving.conversation, { projectId }, moving) },
    archive: (threadId: string) => { const target = workspace.conversations.find(item => item.threadId === threadId); if (target) void change(target, { archived: !target.archived }) },
  }
}
