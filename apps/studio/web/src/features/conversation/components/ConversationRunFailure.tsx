import { CircleAlert, RotateCcw } from 'lucide-react'

import { Button } from '../../../components/ui'
import { useI18n } from '../../../i18n'

/** 运行结果独立于用户消息；重新发送的资格由持久失败事实提供 */
export function ConversationRunFailure({ id, retryable, disabled, onRetry }: {
  id?: string
  retryable: boolean
  disabled: boolean
  onRetry?: () => void
}) {
  const { t } = useI18n()
  return (
    <section id={id} className="conversation-run-failure" aria-label={t('会话异常')}>
      <span className="conversation-run-failure-label"><CircleAlert size={16} aria-hidden="true" />{t('会话异常')}</span>
      {retryable && onRetry && <Button type="button" variant="text" disabled={disabled} leadingIcon={<RotateCcw size={16} />} onClick={onRetry}>{t('重试')}</Button>}
    </section>
  )
}
