import { useEffect, useRef, useState } from 'react'
import type { Dispatch, SetStateAction } from 'react'

import {
  deleteConversation as deleteConversationApi,
  patchConversation,
} from '../../api/conversation/history'
import type { ToastKind } from '../../components/ui/ToastViewport'
import type { Conversation, WorkspaceState } from '../../types'
import { clearPlanQuestionCollapsed } from '../conversation/planQuestionCollapse'
import {
  createNewConversation,
  mergeConversationTitle,
  removeConversation,
  selectCurrentConversation,
  updateConversation,
} from '../../lib/workspace'
import type { WorkspaceDialog } from './components/WorkspaceDialogs'
import { useI18n } from '../../i18n'

export function useConversationManagement({
  workspace,
  conversation,
  setWorkspace,
  setDraftConversation,
  setDraftModel,
  setDraftAccessMode,
  defaultModelId,
  followDetachedConversation,
  abandonPlanInteraction,
  cancelRun,
  isActiveThread,
  onToast,
  onConversationBoundary,
}: {
  workspace: WorkspaceState
  conversation: Conversation
  setWorkspace: Dispatch<SetStateAction<WorkspaceState>>
  setDraftConversation: Dispatch<SetStateAction<Conversation | null>>
  setDraftModel: Dispatch<SetStateAction<string>>
  setDraftAccessMode: Dispatch<SetStateAction<Conversation['accessMode']>>
  defaultModelId: string
  followDetachedConversation: (threadId: string) => Promise<void>
  abandonPlanInteraction: (threadId: string) => void
  cancelRun: (threadId: string) => Promise<boolean>
  isActiveThread: (threadId: string) => boolean
  onToast: (kind: ToastKind, message: string) => void
  onConversationBoundary: () => void
}) {
  const { t } = useI18n()
  const [dialog, setDialog] = useState<WorkspaceDialog | null>(null)
  const [dialogPending, setDialogPending] = useState(false)
  const [pinPendingThreadIds, setPinPendingThreadIds] = useState<ReadonlySet<string>>(
    () => new Set(),
  )
  const pinRequests = useRef(new Set<string>())
  const isMounted = useRef(true)
  const latest = useRef({ workspace, conversation })
  latest.current = { workspace, conversation }

  useEffect(() => {
    isMounted.current = true
    return () => { isMounted.current = false }
  }, [])

  const findConversation = (threadId: string) => (
    latest.current.workspace.conversations.find((item) => item.threadId === threadId)
  )

  const performSelectConversation = (threadId: string) => {
    if (threadId !== latest.current.workspace.currentThreadId) onConversationBoundary()
    setDraftConversation(null)
    setWorkspace((state) => selectCurrentConversation(state, threadId))
    if (findConversation(threadId)?.isHydrated) void followDetachedConversation(threadId)
  }

  const performNewConversation = () => {
    onConversationBoundary()
    setDraftConversation(null)
    setDraftModel(defaultModelId)
    setDraftAccessMode('full')
    setWorkspace((state) => createNewConversation(state))
  }

  const openDialog = (nextDialog: WorkspaceDialog) => {
    setDialog(nextDialog)
  }

  const closeDialog = () => {
    if (dialogPending) return
    setDialog(null)
  }

  const selectConversation = (threadId: string) => performSelectConversation(threadId)
  const newConversation = () => performNewConversation()

  const pinConversation = (threadId: string) => {
    const target = findConversation(threadId)
    if (!target || pinRequests.current.has(threadId)) return
    pinRequests.current.add(threadId)
    setPinPendingThreadIds((current) => new Set(current).add(threadId))
    const previousPinned = target.pinned
    const nextPinned = !target?.pinned
    setWorkspace((state) => updateConversation(
      state,
      threadId,
      (item) => ({ ...item, pinned: nextPinned }),
    ))
    // 同一会话只允许一个置顶 mutation 在途，响应才能安全提交或回滚其乐观值
    void patchConversation(threadId, { pinned: nextPinned }).then((summary) => {
      if (!isMounted.current) return
      setWorkspace((state) => updateConversation(
        state,
        threadId,
        (item) => item.pinned === nextPinned
          ? { ...item, pinned: summary.pinned }
          : item,
      ))
    }).catch(() => {
      if (!isMounted.current) return
      setWorkspace((state) => updateConversation(
        state,
        threadId,
        (item) => item.pinned === nextPinned
          ? { ...item, pinned: previousPinned }
          : item,
      ))
      onToast('error', t('置顶状态更新失败，请重试'))
    }).finally(() => {
      pinRequests.current.delete(threadId)
      if (!isMounted.current) return
      setPinPendingThreadIds((current) => {
        const next = new Set(current)
        next.delete(threadId)
        return next
      })
    })
  }

  const renameConversation = (threadId: string, restoreFocusTo?: HTMLElement | null) => {
    const target = findConversation(threadId)
    if (!target) return
    openDialog({ kind: 'rename', threadId, initialValue: target.title, restoreFocusTo })
  }

  const deleteConversation = (threadId: string, restoreFocusTo?: HTMLElement | null) => {
    const target = findConversation(threadId)
    if (!target) return
    openDialog({
      kind: 'delete',
      threadId,
      title: target.title,
      isRunning: isActiveThread(threadId),
      restoreFocusTo,
    })
  }

  const requestDisablePlan = (threadId: string) => {
    openDialog({ kind: 'disable-plan', threadId })
  }

  const confirmDialog = async (value?: string) => {
    if (!dialog || dialogPending) return
    setDialogPending(true)
    try {
      if (dialog.kind === 'rename') {
        const title = value?.trim()
        if (!title) throw new Error(t('会话名称不能为空'))
        if (Array.from(title).length > 32) throw new Error(t('会话名称最多32个字符'))
        const summary = await patchConversation(dialog.threadId, { title })
        if (!isMounted.current) return
        setWorkspace((state) => updateConversation(
          state, dialog.threadId,
          (item) => ({ ...item, ...mergeConversationTitle(item, summary) }),
        ))
      } else if (dialog.kind === 'disable-plan') {
        abandonPlanInteraction(dialog.threadId)
        onToast('info', t('已关闭 Plan，下一条消息将使用 default 模式'))
      } else if (dialog.kind === 'delete') {
        if (dialog.isRunning) await cancelRun(dialog.threadId)
        if (!isMounted.current) return
        await deleteConversationApi(dialog.threadId)
        if (!isMounted.current) return
        clearPlanQuestionCollapsed(dialog.threadId)
        if (dialog.threadId === latest.current.workspace.currentThreadId) {
          onConversationBoundary()
        }
        setWorkspace((state) => removeConversation(state, dialog.threadId))
      }
      setDialog(null)
    } catch (error) {
      if (!isMounted.current) return
      onToast('error', error instanceof Error && error.message
        ? error.message
        : t('操作失败，请稍后重试'))
    } finally {
      if (isMounted.current) setDialogPending(false)
    }
  }

  return {
    dialog,
    dialogPending,
    closeDialog,
    confirmDialog,
    selectConversation,
    newConversation,
    pinConversation,
    pinPendingThreadIds,
    renameConversation,
    deleteConversation,
    requestDisablePlan,
  }
}
