import { Check, Info, LoaderCircle, RotateCcw, TriangleAlert } from 'lucide-react'

import { useI18n } from '../../i18n'
import { IconButton } from './IconButton'
import { Surface } from './Surface'

type FeedbackIconKind = 'loading' | 'success' | 'info' | 'error' | 'warning'

export function FeedbackIcon({ kind }: { kind: FeedbackIconKind }) {
  return (
    <span className={`ui-feedback-icon is-${kind}`} aria-hidden="true">
      {kind === 'loading' && <LoaderCircle className="ui-feedback-icon__spinner" size={18} />}
      {kind === 'success' && <Check size={17} />}
      {kind === 'info' && <Info size={16} />}
      {kind === 'error' && <span className="ui-feedback-icon__mark">!</span>}
      {kind === 'warning' && <TriangleAlert size={17} />}
    </span>
  )
}

export function FeedbackState({
  kind,
  title,
  onRetry,
  retryLabel,
  compact = false,
}: {
  kind: 'loading' | 'error'
  title: string
  onRetry?: () => void
  retryLabel?: string
  compact?: boolean
}) {
  const { t } = useI18n()
  const actionLabel = retryLabel ?? t('重试')

  return (
    <Surface
      className={`ui-feedback-state is-${kind}${onRetry ? ' has-action' : ' is-title-only'}${compact ? ' is-compact' : ''}`}
      role={kind === 'error' ? 'alert' : 'status'}
      aria-busy={kind === 'loading' || undefined}
    >
      <FeedbackIcon kind={kind} />
      <strong className="ui-feedback-state__title">{title}</strong>
      {kind === 'error' && onRetry && (
        <IconButton
          className="ui-feedback-state__retry"
          size="lg"
          label={actionLabel}
          tooltip={actionLabel}
          icon={<RotateCcw size={18} />}
          onClick={onRetry}
        />
      )}
    </Surface>
  )
}
