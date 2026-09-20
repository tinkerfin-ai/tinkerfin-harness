import { Route, X } from 'lucide-react'
import { useEffect, useRef } from 'react'

import { Button, IconButton, OverlayScrollbar } from '../../../components/ui'
import type { PlanReviewState } from '../../../types'
import { useI18n } from '../../../i18n'
import { MarkdownContent } from './MarkdownContent'
import { PlanInteractionCard, PlanInteractionStatusRow } from './PlanInteractionCard'

export function PlanReviewStatusRow({ interaction }: { interaction: PlanReviewState }) {
  const { t } = useI18n()
  return (
    <PlanInteractionStatusRow
      kind="review"
      icon={<Route size={14} aria-hidden="true" />}
      label="Plan"
      pendingStatus={t('等待审阅')}
      submittedStatus={t('正在处理决定')}
      submitted={interaction.submitted}
    />
  )
}

export function PlanReviewCard({
  interaction,
  onChange,
  onSubmit,
  onClose,
}: {
  interaction: PlanReviewState
  onChange: (updater: (current: PlanReviewState) => PlanReviewState) => void
  onSubmit: (action: 'approve' | 'reject') => void
  onClose: () => void
}) {
  const { t } = useI18n()
  const bodyRef = useRef<HTMLDivElement | null>(null)
  const messageRef = useRef<HTMLTextAreaElement | null>(null)
  const rejectButtonRef = useRef<HTMLButtonElement | null>(null)
  const restoreRejectFocusRef = useRef(false)
  useEffect(() => {
    if (interaction.action !== 'reject') return
    const frame = window.requestAnimationFrame(() => messageRef.current?.focus())
    return () => window.cancelAnimationFrame(frame)
  }, [interaction.action, interaction.interruptId])

  useEffect(() => {
    if (!restoreRejectFocusRef.current || interaction.action === 'reject') return
    restoreRejectFocusRef.current = false
    const frame = window.requestAnimationFrame(() => rejectButtonRef.current?.focus())
    return () => window.cancelAnimationFrame(frame)
  }, [interaction.action, interaction.interruptId])

  const update = (patch: Partial<PlanReviewState>) => onChange((current) => ({
    ...current,
    ...patch,
    error: undefined,
  }))

  const actionAllowed = (action: PlanReviewState['allowedActions'][number]) => (
    interaction.allowedActions?.includes(action) ?? false
  )
  const rejectionFormId = `plan-review-rejection-${interaction.interruptId}`

  const cancelRejection = () => {
    restoreRejectFocusRef.current = true
    update({ action: undefined })
  }

  return (
    <PlanInteractionCard
      disabled={interaction.submitted}
      kind="review"
      ariaLabel={t('Plan 审阅')}
      minimized={false}
      collapsible={false}
      icon={<Route size={16} aria-hidden="true" />}
      title={interaction.draft.content.description}
      bodyRef={bodyRef}
      headerAction={(
        <IconButton
          size="sm"
          className="plan-interaction-card-head-button plan-review-composer-head-button"
          label={t('关闭卡片，继续对话')}
          tooltip={t('关闭卡片，继续对话')}
          icon={<X size={15} />}
          onClick={onClose}
          disabled={interaction.submitted}
        />
      )}
    >
      <>
        <div
          ref={bodyRef}
          className="plan-review-composer-body ui-scrollbar"
          role="region"
          aria-label={t('计划草稿内容')}
        >
          <div
            className="plan-review-content"
            key={`${interaction.interruptId}:${interaction.revision}`}
          >
            <MarkdownContent content={interaction.draft.content.markdown} />
          </div>
          {interaction.action === 'reject' && (
            <form
              id={rejectionFormId}
              className="plan-review-rejection-form"
              onSubmit={(event) => {
                event.preventDefault()
                onSubmit('reject')
              }}
            >
              <label htmlFor={`plan-review-reason-${interaction.interruptId}`}>
                {t('拒绝原因（可选）')}
              </label>
              <textarea
                ref={messageRef}
                id={`plan-review-reason-${interaction.interruptId}`}
                name="reason"
                rows={3}
                value={interaction.message ?? ''}
                placeholder={t('说明为什么不执行这份计划…')}
                onChange={(event) => update({ message: event.currentTarget.value })}
              />
            </form>
          )}
          <footer className="plan-review-composer-footer">
            <p className="plan-review-composer-feedback" role="alert">
              {interaction.error ?? ''}
            </p>
            <div className="approval-composer-actions">
              {interaction.action === 'reject' ? (
                <>
                  <Button size="sm" shape="capsule" onClick={cancelRejection}>
                    {t('取消')}
                  </Button>
                  <Button
                    size="sm"
                    shape="capsule"
                    type="submit"
                    form={rejectionFormId}
                    variant="danger"
                  >
                    {t('确认拒绝')}
                  </Button>
                </>
              ) : (
                <>
                  {actionAllowed('reject') && (
                    <Button
                      ref={rejectButtonRef}
                      size="sm"
                      shape="capsule"
                      className="approval-reject-button"
                      onClick={() => update({ action: 'reject' })}
                    >
                      {t('拒绝')}
                    </Button>
                  )}
                  {actionAllowed('approve') && (
                    <Button
                      size="sm"
                      shape="capsule"
                      variant="solid"
                      onClick={() => onSubmit('approve')}
                    >
                      {t('批准')}
                    </Button>
                  )}
                </>
              )}
            </div>
          </footer>
        </div>
        <OverlayScrollbar viewportRef={bodyRef} />
      </>
    </PlanInteractionCard>
  )
}
