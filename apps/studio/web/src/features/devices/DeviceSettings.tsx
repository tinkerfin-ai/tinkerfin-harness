import { RefreshCw } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { Button, FeedbackState, IconButton } from '../../components/ui'
import { useI18n } from '../../i18n'
import { revokeDevice } from './api'
import { useDevices } from './useDevices'
import { openDeviceClient } from './authorizationRoute'
import { SettingsPanelLayout } from '../settings/SettingsPanelLayout'
import './devices.css'

export function DeviceSettings({ devices }: { devices?: ReturnType<typeof useDevices> }) {
  return devices ? <DeviceSettingsContent devices={devices} /> : <OwnDeviceSettings />
}
function OwnDeviceSettings() { return <DeviceSettingsContent devices={useDevices()} /> }
function DeviceSettingsContent({ devices }: { devices: ReturnType<typeof useDevices> }) {
  const { t } = useI18n()
  const [error, setError] = useState(false)
  const [busy, setBusy] = useState(false)
  const [revoking, setRevoking] = useState<string | null>(null)
  const operation = useRef<AbortController | null>(null)
  const currentDevice = devices.devices.find(device => !device.revoked)
  const revokeTarget = devices.devices.find(device => device.device_id === revoking && !device.revoked)
  useEffect(() => () => operation.current?.abort(), [])
  const perform = async (action: (signal: AbortSignal) => Promise<void>) => {
    if (operation.current) return
    const controller = new AbortController()
    operation.current = controller
    setBusy(true); setError(false)
    try { await action(controller.signal) }
    catch { if (!controller.signal.aborted) setError(true) }
    finally { if (!controller.signal.aborted) { setBusy(false); operation.current = null } }
  }
  const actions = currentDevice && (revokeTarget ? <div className="device-settings__confirm" role="group" aria-label={t('撤销授权')}>
    <p>{t('撤销“{name}”将阻止后续连接和模型请求，已执行的操作不会撤回；离线任务需要在本机核验', { name: revokeTarget.name })}</p>
    <div className="device-settings__actions"><Button type="button" size="sm" disabled={busy} onClick={() => setRevoking(null)}>{t('取消')}</Button><Button type="button" size="sm" variant="danger" loading={busy} onClick={() => void perform(async signal => { await revokeDevice(revokeTarget.device_id, signal); if (!signal.aborted) { setRevoking(null); devices.refresh() } })}>{t('确认撤销设备')}</Button></div>
  </div> : <Button type="button" size="sm" variant="text" className="device-settings__revoke" disabled={busy || devices.status !== 'ready'} onClick={() => setRevoking(currentDevice.device_id)}>{t('撤销授权')}</Button>)
  return <SettingsPanelLayout label={t('设置')} actions={actions}>
  <section className="device-settings settings-section" aria-labelledby="device-settings-title">
    <div className="device-settings__heading">
      <h3 id="device-settings-title">{t('本地设备')}</h3>
      <div className="device-settings__actions">
        <Button type="button" size="sm" variant="text" onClick={() => openDeviceClient()}>{t('打开客户端')}</Button>
        <IconButton type="button" size="sm" label={t('刷新设备')} tooltip={t('刷新')} icon={<RefreshCw size={16} />} onClick={devices.refresh} />
      </div>
    </div>
    {error && <FeedbackState kind="error" compact title={t('设备操作未完成，请重试')} />}
    {devices.status === 'loading' && <div className="device-settings__empty"><FeedbackState kind="loading" compact title={t('正在加载设备')} /></div>}
    {devices.status === 'error' && <div className="device-settings__empty"><FeedbackState kind="error" compact title={t('设备列表暂不可用')} onRetry={devices.refresh} /></div>}
    {devices.status === 'ready' && !devices.devices.length && <div className="device-settings__empty">
      <p>{t('尚未连接设备')}</p>
    </div>}
    <ul className="device-settings__list">{devices.devices.map(device => <li key={device.device_id}>
      <div className="device-settings__heading"><strong>{device.name}</strong><span className={`device-settings__status${device.online && devices.status === 'ready' ? ' is-online' : ''}`}><i aria-hidden="true" />{t(device.revoked ? '已撤销' : device.online && devices.status === 'ready' ? '已连接' : '未连接')}</span></div>
    </li>)}</ul>
  </section>
  </SettingsPanelLayout>
}
