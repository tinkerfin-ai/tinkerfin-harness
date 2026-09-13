import { useEffect, useRef, useState } from 'react'

import { Button, OverlayScrollbar } from '../../../components/ui'
import type {
  ApprovalDecision,
  ApprovalState,
  Conversation,
  Message,
} from '../../../types'
import { useI18n } from '../../../i18n'
import { MarkdownContent } from './MarkdownContent'
import { ToolCallCard } from './MessageBlock'
import { ActivityDots } from './ActivityDots'
import { InteractionCardColorBridge } from './InteractionCardColorBridge'

export interface ApprovalSubmissionDecision {
  interruptId: string
  decision: ApprovalDecision
  rejectionReason?: string
}

export function ApprovalStatusRow() {
  const { t } = useI18n()
  return (
    <div className="approval-wait-state">
      <ActivityDots label={t('等待处理')} />
    </div>
  )
}

const descriptionParts = (description: string, fallback: string) => {
  const normalized = description.trim()
  const [title, ...rest] = normalized.split(/\n\s*\n/).filter(Boolean)
  return {
    title: title || fallback,
    detail: rest.join('\n\n'),
  }
}

const matchesApprovalGroup = (
  approval: ApprovalState,
  interruptIds: readonly string[],
) => approval.items.length === interruptIds.length
  && approval.items.every(
    (item, index) => item.interruptId === interruptIds[index],
  )

const decideItem = (
  approval: ApprovalState,
  decision: ApprovalSubmissionDecision,
) => {
  const activeIndex = approval.items.findIndex(
    (item) => item.interruptId === decision.interruptId,
  )
  if (activeIndex < 0) return approval
  const items = approval.items.map((item, index) => index === activeIndex
    ? {
        ...item,
        decision: decision.decision,
        rejectionReason: decision.decision === 'rejected'
          ? decision.rejectionReason
          : undefined,
      }
    : item)
  const nextUndecided = items.findIndex((item) => !item.decision)
  return {
    ...approval,
    items,
    activeIndex: nextUndecided < 0 ? activeIndex : nextUndecided,
    mode: 'options' as const,
    error: undefined,
  }
}

export function ApprovalCard({
  conversation,
  onChange,
  onSubmit,
}: {
  conversation: Conversation
  onChange: (updater: (approval: ApprovalState) => ApprovalState) => void
  onSubmit: (
    interruptIds: readonly string[],
    finalDecision?: ApprovalSubmissionDecision,
  ) => void
}) {
  const { t } = useI18n()
  const approval = conversation.approval
  const bodyRef = useRef<HTMLDivElement>(null)
  const rejectionTextareaRef = useRef<HTMLTextAreaElement>(null)
  const rejectButtonRef = useRef<HTMLButtonElement>(null)
  const approveButtonRef = useRef<HTMLButtonElement>(null)
  const retryButtonRef = useRef<HTMLButtonElement>(null)
  const restoreRejectFocusRef = useRef(false)
  const [rejectionDrafts, setRejectionDrafts] = useState<Record<string, string>>({})

  const activeIndex = Math.max(0, Math.min(
    approval?.activeIndex ?? 0,
    (approval?.items.length ?? 1) - 1,
  ))
  const active = approval?.items[activeIndex]
  const activeInterruptId = active?.interruptId
  const previousActiveInterruptIdRef = useRef(activeInterruptId)

  useEffect(() => {
    const previous = previousActiveInterruptIdRef.current
    previousActiveInterruptIdRef.current = activeInterruptId
    if (!activeInterruptId || !previous || previous === activeInterruptId) return
    const frame = window.requestAnimationFrame(() => (
      rejectButtonRef.current ?? approveButtonRef.current ?? retryButtonRef.current
    )?.focus())
    return () => window.cancelAnimationFrame(frame)
  }, [activeInterruptId])

  useEffect(() => {
    if (!activeInterruptId || approval?.mode !== 'reject') return
    const frame = window.requestAnimationFrame(() => rejectionTextareaRef.current?.focus())
    return () => window.cancelAnimationFrame(frame)
  }, [activeInterruptId, approval?.mode])

  useEffect(() => {
    if (!restoreRejectFocusRef.current || approval?.mode === 'reject') return
    restoreRejectFocusRef.current = false
    const frame = window.requestAnimationFrame(() => rejectButtonRef.current?.focus())
    return () => window.cancelAnimationFrame(frame)
  }, [activeInterruptId, approval?.mode])

  if (!approval || approval.items.length === 0) return null
  if (!active) return null
  const interruptIds = approval.items.map((item) => item.interruptId)
  const description = descriptionParts(active.description, t('请确认本次操作'))
  const canApprove = active.allowedDecisions.includes('approve')
  const canReject = active.allowedDecisions.includes('reject')
  const allDecided = approval.items.every((item) => Boolean(item.decision))
  const rejectionReason = rejectionDrafts[active.interruptId]
    ?? active.rejectionReason
    ?? ''
  const rejectionFormId = `approval-rejection-${active.id}`
  const toolMessage: Message = {
    id: `approval-tool-${active.interruptId}`,
    role: 'tool',
    content: active.toolName,
    createdAt: conversation.updatedAt,
    meta: {
      toolName: active.toolName,
      params: active.params,
      status: 'paused',
      toolCallId: active.toolCallId,
      interruptId: active.interruptId,
    },
  }

  const updateApproval = (updater: (current: ApprovalState) => ApprovalState) => {
    onChange((current) => matchesApprovalGroup(current, interruptIds)
      ? updater(current)
      : current)
  }

  const recordDecision = (decision: ApprovalSubmissionDecision) => {
    const completesGroup = approval.items.every((item) => (
      Boolean(item.decision) || item.interruptId === decision.interruptId
    ))
    if (completesGroup) {
      // 最后一项由提交所有者合并到权威会话，避免先 setState 再读取造成遗漏
      onSubmit(interruptIds, decision)
      return
    }
    updateApproval((current) => decideItem(current, decision))
  }

  const confirmRejection = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    recordDecision({
      interruptId: active.interruptId,
      decision: 'rejected',
      rejectionReason,
    })
  }

  const setMode = (mode: NonNullable<ApprovalState['mode']>) => {
    updateApproval((current) => ({ ...current, mode, error: undefined }))
  }

  const cancelRejection = () => {
    restoreRejectFocusRef.current = true
    setMode('options')
  }

  return (
    <section
      className="approval-composer"
      aria-label={t('等待审批')}
      onWheel={(event) => {
        const body = bodyRef.current
        if (!body || body.scrollHeight <= body.clientHeight) {
          event.preventDefault()
          event.stopPropagation()
          return
        }
        if (!body.contains(event.target as Node)) {
          event.preventDefault()
          event.stopPropagation()
          body.scrollTop += event.deltaY
        }
      }}
    >
      <header className="approval-composer-head">
        <div className="approval-composer-heading">
          <h2>
            <span className="approval-status-dot" aria-hidden="true" />
            <span className="plan-interaction-card-title">{description.title}</span>
          </h2>
        </div>
      </header>
      <InteractionCardColorBridge tone="warning" />

      <div ref={bodyRef} className="approval-composer-body ui-scrollbar">
        {description.detail && (
          <div className="approval-composer-detail">
            <MarkdownContent content={description.detail} variant="compact" />
          </div>
        )}
        <ToolCallCard message={toolMessage} className="approval-tool-card" />
        {approval.mode === 'reject' && (
          <form
            id={rejectionFormId}
            key={active.interruptId}
            className="approval-rejection-form"
            onSubmit={confirmRejection}
          >
            <label htmlFor={`approval-reason-${active.id}`}>
              {t('拒绝原因（可选）')}
            </label>
            <textarea
              ref={rejectionTextareaRef}
              id={`approval-reason-${active.id}`}
              name="reason"
              value={rejectionReason}
              rows={3}
              placeholder={t('说明拒绝此操作的原因…')}
              onChange={(event) => {
                const value = event.currentTarget.value
                setRejectionDrafts((current) => ({
                  ...current,
                  [active.interruptId]: value,
                }))
              }}
            />
          </form>
        )}
      </div>
      <OverlayScrollbar viewportRef={bodyRef} />
      <footer className="approval-composer-footer">
        <p className="approval-composer-feedback" role="alert">
          {approval.error ?? ''}
        </p>
        <div className="approval-composer-actions">
          {approval.mode === 'reject' ? (
            <>
              <Button size="sm" shape="capsule" onClick={cancelRejection}>{t('取消')}</Button>
              <Button size="sm" shape="capsule" type="submit" form={rejectionFormId} variant="danger">
                {t('确认拒绝')}
              </Button>
            </>
          ) : allDecided ? (
            <Button
              ref={retryButtonRef}
              size="sm"
              shape="capsule"
              variant="solid"
              onClick={() => onSubmit(interruptIds)}
            >
              {t('重新提交')}
            </Button>
          ) : (
            <>
              {canReject && (
                <Button
                  ref={rejectButtonRef}
                  size="sm"
                  shape="capsule"
                  className="approval-reject-button"
                  onClick={() => setMode('reject')}
                >
                  {t('拒绝')}
                </Button>
              )}
              {canApprove && (
                <Button
                  ref={approveButtonRef}
                  size="sm"
                  shape="capsule"
                  variant="solid"
                  onClick={() => recordDecision({
                    interruptId: active.interruptId,
                    decision: 'approved',
                  })}
                >
                  {t('允许')}
                </Button>
              )}
            </>
          )}
        </div>
      </footer>
    </section>
  )
}
