import { X } from 'lucide-react'
import { Tooltip } from '../../../components/ui'
import { useI18n } from '../../../i18n'

export function ComposerPlanChip({
  locked,
  onExitPlan,
}: {
  locked: boolean
  onExitPlan: () => void
}) {
  const { t } = useI18n()
  return (
    <Tooltip content={t('Plan 已开启 — 点击关闭')} placement="top"><button
      type="button"
      className="composer-plan-chip"
      aria-label={t('Plan 已开启，点击关闭')}
      disabled={locked}
      onClick={onExitPlan}
    >
      Plan
      <span className="composer-plan-chip-close" aria-hidden="true">
        <X size={12} strokeWidth={2.4} />
      </span>
    </button></Tooltip>
  )
}
