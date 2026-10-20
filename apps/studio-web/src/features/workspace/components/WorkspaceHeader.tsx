import { Menu } from 'lucide-react'
import type { ReactNode, RefObject } from 'react'

import { IconButton } from '../../../components/ui'
import { useI18n } from '../../../i18n'

export function WorkspaceHeader({
  conversationTitle,
  overlayTriggerRef,
  onOpenOverlay,
  navigation,
  actions,
  backgroundInert = false,
}: {
  conversationTitle: string
  overlayTriggerRef: RefObject<HTMLButtonElement | null>
  onOpenOverlay: () => void
  navigation?: ReactNode
  actions?: ReactNode
  backgroundInert?: boolean
}) {
  const { t } = useI18n()
  return (
    <header
      className="chat-header"
      aria-hidden={backgroundInert || undefined}
      inert={backgroundInert || undefined}
    >
      <div className="header-left">
        <IconButton ref={overlayTriggerRef} className="menu-toggle" label={t('打开导航')} icon={<Menu size={19} />} onClick={onOpenOverlay} />
        <h1 className="workspace-title">{conversationTitle || t('新会话')}</h1>
      </div>
      {navigation && <div className="header-navigation">{navigation}</div>}
      <div className="header-actions">
        {actions}
      </div>
    </header>
  )
}
