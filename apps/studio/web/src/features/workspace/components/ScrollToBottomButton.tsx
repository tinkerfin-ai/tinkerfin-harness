import { ArrowDown } from 'lucide-react'

import { useI18n } from '../../../i18n'

export function ScrollToBottomButton({
  fading,
  onPointerEnter,
  onPointerLeave,
  onFocus,
  onBlur,
  onClick,
}: {
  fading: boolean
  onPointerEnter: () => void
  onPointerLeave: () => void
  onFocus: () => void
  onBlur: () => void
  onClick: () => void
}) {
  const { t } = useI18n()

  return (
    <button
      type="button"
      className={`composer-auxiliary-control scroll-to-bottom is-visible${fading ? ' is-fading' : ''}`}
      aria-label={t('回到底部')}
      onPointerEnter={onPointerEnter}
      onPointerLeave={onPointerLeave}
      onFocus={onFocus}
      onBlur={onBlur}
      onClick={onClick}
    >
      <ArrowDown size={16} aria-hidden="true" />
      <span>{t('回到底部')}</span>
    </button>
  )
}
