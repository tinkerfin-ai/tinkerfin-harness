import { useI18n } from '../../i18n'
import { FeedbackState } from './FeedbackState'

export function GlobalErrorFallback() {
  const { t } = useI18n()
  return (
    <main id="main-content" className="app-error-fallback">
      <FeedbackState
        kind="error"
        title={t('页面暂时无法显示')}
        retryLabel={t('重新加载页面')}
        onRetry={() => window.location.reload()}
      />
    </main>
  )
}
