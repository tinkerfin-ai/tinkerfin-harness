import { Accessibility, ArrowUpRight, Check, ScanLine } from 'lucide-react'
import { Button } from '../components/ui/Button'
import { send } from '../bridge'
import { useI18n } from '../i18n'
import type { DesktopState } from '../state'

export function Permissions({ state }: { state: DesktopState }) {
  const { t } = useI18n()
  return <><main className="client-body" aria-label={t('权限')}><div className="permission-list">
    {([{ id: 'accessibility', title: '辅助功能', detail: '读取控件、点击与输入', Icon: Accessibility },
      { id: 'screenCapture', title: '屏幕录制', detail: '截取应用窗口', Icon: ScanLine }] as const).map(({ id, title, detail, Icon }) =>
      <div className="permission-row" key={id}><Icon aria-hidden="true" /><div className="grow"><strong>{t(title)}</strong><p>{t(detail)}</p></div>
        <div><span className={`permission-state${state.permissions[id] ? ' permission-state--on' : ''}`}>
          {state.permissions[id] && <Check aria-hidden="true" />}
          <Button variant="text" className="row-action" trailingIcon={<ArrowUpRight size={16} />} onClick={() => void send({ type: 'permission', permission: id })}>{t(state.permissions[id] ? '已开启' : '前往开启')}</Button>
        </span></div>
      </div>)}
  </div></main><footer className="pane-actions" aria-hidden="true" /></>
}
