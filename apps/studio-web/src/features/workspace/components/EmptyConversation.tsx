import { BrandLogo } from '../../../components/ui'
import { useI18n } from '../../../i18n'

export function EmptyConversationBrand() {
  return (
    <div className="empty-brand-lockup" aria-hidden="true">
      <BrandLogo size="lg" />
    </div>
  )
}

export function EmptyConversation() {
  const { t } = useI18n()
  return (
    <div className="empty-conversation">
      <h2 className="visually-hidden">{t('暂无消息')}</h2>
      <p className="visually-hidden">{t('发送一条消息，开始新的真实对话流')}</p>
    </div>
  )
}
