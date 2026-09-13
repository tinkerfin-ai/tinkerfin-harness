import { useRef, type ReactNode } from 'react'
import { OverlayScrollbar } from '../../components/ui'
import { useI18n } from '../../i18n'

/** 表单内容独立滚动，底部操作始终位于滚动区域外 */
export function ModelSettingsLayout({ children, actions }: { children: ReactNode; actions: ReactNode }) {
  const { t } = useI18n()
  const viewport = useRef<HTMLDivElement>(null)
  return <>
    <div className="settings-models__content-shell">
      <div ref={viewport} className="settings-models__scroll ui-scrollbar" role="region" aria-label={t('模型配置')} tabIndex={0}>
        <div className="settings-models__editor">{children}</div>
      </div>
      <OverlayScrollbar viewportRef={viewport} />
    </div>
    <div className="settings-models__form-actions"><div className="settings-models__footer-content">{actions}</div></div>
  </>
}
