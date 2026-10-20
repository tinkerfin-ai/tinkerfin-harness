import { Cloud, Laptop, RotateCw } from 'lucide-react'
import { IconButton } from '../../components/ui'
import { useI18n } from '../../i18n'
import type { LocalTarget } from './api'
import type { useDevices } from './useDevices'
import './devices.css'

export function DevicePicker({ devices, projectId, value, onChange, locked, disabled, targetState, onRetry }: {
  devices: ReturnType<typeof useDevices>
  projectId: string
  value: LocalTarget | undefined
  onChange: (value: LocalTarget | undefined) => void
  locked: boolean
  targetState: 'loading' | 'error' | 'ready'
  onRetry: () => void
  disabled: boolean
}) {
  const { t } = useI18n()
  const device = value ? devices.devices.find(item => item.device_id === value.device_id) : devices.devices.find(item => !item.revoked)
  const connected = devices.status === 'ready' && Boolean(device?.online && !device.revoked)
  const showLocal = Boolean(value || device && !device.revoked)
  const connection = devices.status === 'loading' ? t('正在加载设备') : devices.status === 'error'
    ? t('设备列表暂不可用') : connected ? t('已连接') : t('未连接')
  return <div className="execution-location" role="group" aria-label={t('执行位置')}>
    <IconButton type="button" size="xs" label={t('云端')} tooltip={locked && value ? t('请在新会话中选择执行位置') : t('云端')} selected={targetState === 'ready' && !value}
      icon={<Cloud size={17} />} disabled={disabled || locked && Boolean(value)}
      onClick={() => onChange(undefined)} />
    {showLocal && <div className="execution-location__local">
      <IconButton type="button" size="xs" label={`${t('本地')} · ${connection}`} tooltip={locked && !value ? t('请在新会话中选择执行位置') : `${t('本地')} · ${connection}`}
        selected={targetState === 'ready' && Boolean(value)} disabled={disabled || locked && !value || !device || device.revoked || devices.status !== 'ready'}
        icon={<><Laptop size={17} /><span aria-hidden="true" className={`execution-location__dot${connected ? ' is-online' : ''}`} /></>}
        onClick={() => device && onChange({ device_id: device.device_id, workspace_id: projectId })} />
    </div>}
    {targetState === 'error' && <IconButton type="button" size="xs" label={t('重试执行位置')} tooltip={t('重试执行位置')} icon={<RotateCw size={15} />} onClick={onRetry} />}
  </div>
}
