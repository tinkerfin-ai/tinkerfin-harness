import { ChevronDown, CircleAlert, LoaderCircle } from 'lucide-react'
import type { ReactNode } from 'react'
import { Button } from '../../../components/ui'
import { useI18n } from '../../../i18n'

export interface InteractionSubmissionState {
  failed: boolean
  checking: boolean
  retry: () => void
}

/** 提交期间保留可读内容，重新检查只读取保存结果 */
export function InteractionSubmission({ state, stopControl, children }: {
  state: InteractionSubmissionState
  stopControl?: ReactNode
  children: ReactNode
}) {
  const { t } = useI18n()
  return <section className="interaction-submission" aria-label={t('提交确认')}>
    <div className="interaction-submission-heading">
      <span className="interaction-submission-icon" aria-hidden="true">
        {state.failed ? <CircleAlert size={14} /> : <LoaderCircle size={14} className={state.checking ? 'ui-button__spinner' : undefined} />}
      </span>
      <span role="status">{state.failed ? t('暂时无法确认提交结果') : t('正在确认提交状态')}</span>
      <div className="interaction-submission-actions">
        <Button type="button" size="sm" variant="text" loading={state.checking} onClick={state.retry}>{t('重新检查状态')}</Button>
        {stopControl}
      </div>
    </div>
    <details className="interaction-submission-details">
      <summary>{t('查看提交内容')}<ChevronDown size={14} aria-hidden="true" /></summary>
      <div className="interaction-submission-content">{children}</div>
    </details>
  </section>
}
