import { FeedbackState } from '../../../components/ui'
import { useI18n } from '../../../i18n'
import { conversationErrorMessage, conversationRunError } from '../../../api/conversation/errors'

/** 运行结果独立于用户消息；重新发送的资格由持久失败事实提供 */
export function ConversationRunFailure({ id, errorCode, retryable, disabled, onRetry }: {
  id?: string
  errorCode: string | null
  retryable: boolean
  disabled: boolean
  onRetry?: () => void
}) {
  const { t } = useI18n()
  const title = errorCode === 'workspace_busy' || errorCode === 'workspace_file_conflict'
    ? conversationErrorMessage(conversationRunError(errorCode), 'run_failed')
    : t('会话异常')
  return (
    <section id={id} className="conversation-run-failure" aria-label={t('会话异常')}>
      <FeedbackState kind="error" appearance="retry" title={title} retryLabel={t('重试')} retryDisabled={disabled} onRetry={retryable ? onRetry : undefined} />
    </section>
  )
}
