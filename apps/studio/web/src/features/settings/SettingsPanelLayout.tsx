import { useRef, type ReactNode } from 'react'
import { OverlayScrollbar } from '../../components/ui'

/** 设置内容独立滚动，底部操作始终位于滚动区域外 */
export function SettingsPanelLayout({ children, actions, label, contentClassName }: {
  children: ReactNode
  actions?: ReactNode
  label: string
  contentClassName?: string
}) {
  const viewport = useRef<HTMLDivElement>(null)
  return <div className="settings-panel">
    <div className="settings-panel__content-shell">
      <div ref={viewport} className="settings-panel__scroll ui-scrollbar" role="region" aria-label={label} tabIndex={0}>
        <div className={contentClassName}>{children}</div>
      </div>
      <OverlayScrollbar viewportRef={viewport} />
    </div>
    {actions && <footer className="settings-panel__actions"><div className="settings-panel__footer-content">{actions}</div></footer>}
  </div>
}
