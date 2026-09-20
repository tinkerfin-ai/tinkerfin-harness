import { ChevronDown, ChevronUp } from 'lucide-react'
import { useId, useRef, type ReactNode, type RefObject } from 'react'

import { IconButton } from '../../../components/ui'
import { ActivityDots } from './ActivityDots'
import { InteractionCardColorBridge } from './InteractionCardColorBridge'
import { InteractionCardResizeHandle } from './InteractionCardResizeHandle'

type PlanInteractionKind = 'question' | 'review'

const namespaceFor = (kind: PlanInteractionKind) => (
  kind === 'question' ? 'plan-question-composer' : 'plan-review-composer'
)

export function PlanInteractionStatusRow({
  kind,
  icon,
  label,
  pendingStatus,
  submittedStatus,
  submitted,
}: {
  kind: PlanInteractionKind
  icon: ReactNode
  label: string
  pendingStatus: string
  submittedStatus: string
  submitted: boolean
}) {
  const namespace = kind === 'question' ? 'plan-question' : 'plan-review'
  const status = submitted ? submittedStatus : pendingStatus
  return (
    <div className={`plan-interaction-wait-state ${namespace}-wait-state`}>
      <div
        className={`plan-interaction-status-row ${namespace}-status-row`}
        role={submitted ? 'status' : undefined}
      >
        {icon}
        <span className="plan-interaction-status-label">{label}</span>
        <span
          className={`plan-interaction-status-separator ${namespace}-status-separator`}
          aria-hidden="true"
        />
        <span>{status}</span>
      </div>
      {!submitted && <ActivityDots label={pendingStatus} />}
    </div>
  )
}

export function PlanInteractionCard({
  kind,
  ariaLabel,
  minimized,
  collapsible = true,
  disabled = false,
  icon,
  title,
  titleMeta,
  description,
  toggleSurfaceLabel,
  toggleLabel,
  onToggle,
  bodyRef,
  headerAction,
  minimizedContent,
  children,
}: {
  kind: PlanInteractionKind
  ariaLabel: string
  minimized: boolean
  collapsible?: boolean
  disabled?: boolean
  icon: ReactNode
  title: ReactNode
  titleMeta?: ReactNode
  description?: ReactNode
  toggleSurfaceLabel?: string
  toggleLabel?: string
  onToggle?: () => void
  bodyRef: RefObject<HTMLDivElement | null>
  headerAction?: ReactNode
  minimizedContent?: ReactNode
  children: ReactNode
}) {
  const namespace = namespaceFor(kind)
  const cardId = useId()
  const cardRef = useRef<HTMLElement>(null)
  const toggleSurfaceClass = 'plan-question-toggle-surface'
  return (
    <section
      ref={cardRef}
      id={cardId}
      className={`plan-interaction-card ${namespace}${minimized ? ' is-minimized' : ''}`}
      aria-label={ariaLabel}
      aria-busy={disabled || undefined}
      inert={disabled || undefined}
      onWheel={(event) => {
        // 卡片接管输入区后，外部滚轮只驱动卡片正文，避免误滚动会话历史
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
      {!minimized && <InteractionCardResizeHandle cardRef={cardRef} controls={cardId} />}
      <header className={`plan-interaction-card-head ${namespace}-head`}>
        {collapsible && toggleSurfaceLabel && onToggle && (
          <button
            type="button"
            className={`plan-interaction-toggle-surface ${toggleSurfaceClass}`}
            aria-label={toggleSurfaceLabel}
            aria-expanded={!minimized}
            onClick={onToggle}
          />
        )}
        <div className={`plan-interaction-card-heading ${namespace}-heading`}>
          <h2>
            {icon}
            <span className="plan-interaction-card-title">{title}</span>
            {titleMeta && <small className="plan-interaction-card-title-meta">{titleMeta}</small>}
            {description && (
              <small className="plan-interaction-card-description">{description}</small>
            )}
          </h2>
        </div>
        {(collapsible || headerAction) && (
          <div className={`plan-interaction-card-head-actions ${namespace}-head-actions`}>
            {headerAction}
            {collapsible && toggleLabel && onToggle && (
              <IconButton
                size="sm"
                className={`plan-interaction-card-head-button ${namespace}-head-button`}
                label={toggleLabel}
                tooltip={toggleLabel}
                icon={minimized ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
                aria-expanded={!minimized}
                onClick={onToggle}
              />
            )}
          </div>
        )}
      </header>
      {!minimized && (
        <InteractionCardColorBridge tone={kind === 'question' ? 'plan' : 'warning'} />
      )}
      {minimized ? minimizedContent : children}
    </section>
  )
}
