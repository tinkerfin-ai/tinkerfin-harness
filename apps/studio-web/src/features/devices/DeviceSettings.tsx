import { ExternalLink, Laptop, Link2, Link2Off, RefreshCw } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { Button, FeedbackState, IconButton, Surface } from '../../components/ui'
import { useI18n } from '../../i18n'
import { revokeDevice } from './api'
import { useDevices } from './useDevices'
import { restoreFocus } from '../../components/ui/focus'
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
  const [connecting, setConnecting] = useState(false)
  const revokeButton = useRef<HTMLButtonElement>(null)
  const cancelRevoke = useRef<HTMLButtonElement>(null)
  const wasConfirming = useRef(false)
  const operation = useRef<AbortController | null>(null)
  const currentDevice = devices.devices.find(device => !device.revoked)
  const revokeTarget = devices.devices.find(device => device.device_id === revoking && !device.revoked)
  useEffect(() => () => operation.current?.abort(), [])
  useEffect(() => {
    if (revoking) cancelRevoke.current?.focus()
    else if (wasConfirming.current) restoreFocus(revokeButton.current)
    wasConfirming.current = Boolean(revoking)
  }, [revoking])
  const perform = async (action: (signal: AbortSignal) => Promise<void>) => {
    if (operation.current) return
    const controller = new AbortController()
    operation.current = controller
    setBusy(true); setError(false)
    try { await action(controller.signal) }
    catch { if (!controller.signal.aborted) setError(true) }
    finally { if (!controller.signal.aborted) { setBusy(false); operation.current = null } }
  }
  const online = Boolean(currentDevice?.online && devices.status === 'ready')
  const waiting = connecting && !online
  const openClient = () => { openDeviceClient(); if (!currentDevice) setConnecting(true) }
  return <SettingsPanelLayout label={t('设置')}>
    <section className="local-resources device-settings settings-section" aria-labelledby="device-settings-title">
      <div className="lr-content-heading">
        <h3 id="device-settings-title">{t('本地设备')}</h3>
        {currentDevice && <div className="lr-heading-actions">
          <Button type="button" className="lr-action" leadingIcon={<ExternalLink size={14} />} onClick={openClient}>{t('打开客户端')}</Button>
          <IconButton type="button" className="lr-icon-button" variant="ghost" label={t('刷新设备状态')} tooltip={t('刷新')} icon={<RefreshCw size={17} />} loading={devices.refreshing} disabled={busy} onClick={devices.refresh} />
        </div>}
      </div>
      {error && <FeedbackState kind="error" compact title={t('设备操作未完成，请重试')} />}
      {(devices.status === 'loading' || devices.status === 'error' && devices.refreshing && !currentDevice && !connecting) && <FeedbackState kind="loading" compact title={t('正在加载设备')} />}
      {devices.status === 'error' && !devices.refreshing && <FeedbackState kind="error" compact title={t('设备列表暂不可用')} onRetry={devices.refresh} retryDisabled={busy} />}
      {currentDevice && <Surface as="section" className="lr-device-card" aria-label={t('已授权设备')}>
        <div className="lr-card-device">
          <span className="lr-device-tile"><Laptop size={21} aria-hidden="true" /></span>
          <div className="lr-card-device-copy">
            <span className="lr-card-device-name">{currentDevice.name}</span>
            <span className={`lr-status${online ? '' : ' lr-status-offline'}`} role="status"><span className={`lr-dot${online ? '' : ' lr-dot-offline'}`} aria-hidden="true" />{t(online ? '已连接' : '未连接')}</span>
          </div>
          {!revokeTarget && <div className="lr-device-actions">
            {!online && <Button type="button" className="lr-action" leadingIcon={<Link2 size={14} />} disabled={busy} onClick={openDeviceClient}>{t('重连')}</Button>}
            <Button ref={revokeButton} type="button" className="lr-revoke-button" leadingIcon={<Link2Off size={14} />} disabled={busy || devices.refreshing || devices.status !== 'ready'} onClick={() => setRevoking(currentDevice.device_id)}>{t('撤销授权')}</Button>
          </div>}
        </div>
        {revokeTarget && <div className="lr-revoke-confirm" role="group" aria-label={t('撤销授权')}>
          <p>{t('撤销后将无法访问此设备，再次使用需重新授权。')}</p>
          <div className="lr-confirm-actions">
            <Button ref={cancelRevoke} type="button" className="lr-action" disabled={busy} onClick={() => setRevoking(null)}>{t('取消')}</Button>
            <Button type="button" className="lr-action lr-action-danger" variant="danger" loading={busy} disabled={devices.refreshing || devices.status !== 'ready'} onClick={() => void perform(async signal => { await revokeDevice(revokeTarget.device_id, signal); if (!signal.aborted) { setRevoking(null); setConnecting(false); devices.refresh() } })}>{t('撤销授权')}</Button>
          </div>
        </div>}
      </Surface>}
      {!currentDevice && (devices.status === 'ready' || connecting) && <Surface as="section" className="lr-empty" aria-label={t('尚未配置本地设备')}>
        <span className="lr-device-tile"><Laptop size={21} aria-hidden="true" /></span>
        <h4>{t(waiting ? '等待设备连接' : '连接你的电脑')}</h4>
        <p>{t(waiting ? '在客户端完成授权后，刷新连接状态。' : '连接后即可使用已授权的本机资源。')}</p>
        <div className="lr-connect-actions">
          <Button type="button" className="lr-action lr-action-primary" variant="solid" leadingIcon={<ExternalLink size={14} />} onClick={openClient}>{t('打开客户端')}</Button>
          <IconButton type="button" className="lr-icon-button" variant="ghost" label={t('刷新连接状态')} tooltip={t('刷新连接状态')} icon={<RefreshCw size={17} />} loading={devices.refreshing} disabled={busy} onClick={devices.refresh} />
        </div>
        <div className="lr-setup-links">
          <span className="lr-download-note">{t('尚未安装？')}<a className="lr-download-link" href="https://github.com/tinkerfin-ai/tinkerfin-harness/releases/latest/download/TinkerFin.zip" target="_blank" rel="noopener noreferrer">{t('下载客户端')}</a></span>
        </div>
        <span className="lr-empty-note" role="status">{t(waiting ? '设备连接成功后会显示在这里' : '在客户端登录同一账号并完成授权')}</span>
      </Surface>}
    </section>
  </SettingsPanelLayout>
}
