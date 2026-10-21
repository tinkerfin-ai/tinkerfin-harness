import { Laptop } from 'lucide-react'
import { Button } from '../../components/ui'
import { useI18n } from '../../i18n'
import './devices.css'

export function ComputerResourceWait({ onOpenSettings, onCancel, cancelling }: {
  onOpenSettings: (trigger: HTMLElement | null) => void
  onCancel: () => void
  cancelling: boolean
}) {
  const { t } = useI18n()
  return <div className="computer-resource-wait">
    <span role="status"><Laptop size={17} aria-hidden="true" />{t('等待本机连接')}</span>
    <div className="computer-resource-wait__actions">
      <Button type="button" variant="text" size="lg" onClick={event => onOpenSettings(event.currentTarget)}>{t('配置设备')}</Button>
      <Button type="button" variant="text" size="lg" disabled={cancelling} onClick={onCancel}>{t('取消等待')}</Button>
    </div>
  </div>
}
