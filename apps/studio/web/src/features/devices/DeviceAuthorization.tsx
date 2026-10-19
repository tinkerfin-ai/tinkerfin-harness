import { Laptop } from 'lucide-react'
import { useEffect, useId, useRef, useState } from 'react'
import type { AuthSession } from '../../auth/session'
import type { LoginRequest } from '../../api/auth/types'
import { BrandLogo, Button, FeedbackState, TextField } from '../../components/ui'
import { useI18n } from '../../i18n'
import type { DeviceAuthorizationIntent } from './authorizationRoute'
import { useDeviceAuthorization } from './useDeviceAuthorization'
import './deviceAuthorization.css'

interface Props {
  intent: DeviceAuthorizationIntent
  session: AuthSession | null
  checking: boolean
  pending: boolean
  onLogin: (credentials: LoginRequest) => Promise<void>
  onExit: (id: string) => void
}

export function DeviceAuthorization({ intent, session, checking, pending, onLogin, onExit }: Props) {
  const { t } = useI18n()
  const headingId = useId()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const { state, request } = useDeviceAuthorization(intent, session, checking)
  const preview = 'preview' in state ? state.preview : null
  // 取消只校验浏览器入口，返回已批准状态也不能证明当前账号授权成功
  const authorized = state.phase === 'ready' && state.action !== 'deny' && state.preview.state === 'authorized'
  const terminal = state.phase === 'unavailable' || state.phase === 'expired' ? state.phase
    : state.phase === 'ready' && state.action === 'deny' ? 'deny'
    : state.phase === 'ready' && state.preview.state !== 'waiting' && state.preview.state !== 'authorized' ? state.preview.state : null
  const busy = state.phase === 'loading'
  const completed = useRef<string | null>(null)
  const completionKey = JSON.stringify([intent.id, intent.browserToken, session?.token, session?.user.user_id, terminal])
  useEffect(() => {
    if (!terminal || completed.current === completionKey) return
    completed.current = completionKey
    onExit(intent.id)
  }, [terminal, completionKey, intent.id, onExit])
  if (terminal) return null
  const title = checking ? '连接 Studio' : !session ? '登录' : preview ? '允许连接这台 Mac？' : '连接 Studio'
  return <main className="device-authorization" id="main-content">
    <div className="device-authorization__brand" role="img" aria-label="TinkerFin"><BrandLogo className="device-authorization__logo" /></div>
    <div className="device-authorization__content">
    {authorized ? <p className="device-authorization__success" role="status">{t('授权成功，你可以关闭此页面')}</p> :
      <section className="device-authorization__body" aria-labelledby={headingId}>
        <div className="device-authorization__badge" aria-hidden="true"><Laptop size={32} strokeWidth={1.75} /></div>
        <h2 id={headingId}>{t(title)}</h2>
        {session && preview && <p className="device-authorization__description">{t('请确认下方账号与设备信息')}</p>}
        {checking ? <FeedbackState kind="loading" compact title={t('正在检查登录状态')} /> : <>
        {session && preview && <dl className="device-authorization__summary">
          <div><dt>{t('账号')}</dt><dd><strong>{session.user.username}</strong></dd></div>
          <div><dt>{t('设备')}</dt><dd><strong>{preview.name}</strong></dd></div>
        </dl>}
        {state.phase === 'retry' ? <>
          <p className="device-authorization__error" role="alert">{t('授权暂不可用，请重试')}</p>
          <div className="device-authorization__actions device-authorization__actions--single">
            <Button type="button" size="lg" variant="solid" disabled={pending} onClick={() => void request(state.action)}>{t('重试')}</Button>
          </div>
        </> : !session ? <form onSubmit={event => {
          event.preventDefault()
          if (!pending && !busy) void onLogin({ username: username.trim(), password })
        }}>
          <div className="device-authorization__fields">
            <TextField label={t('用户名')} value={username} onChange={event => setUsername(event.target.value)} autoComplete="username" required disabled={pending || busy} />
            <TextField label={t('密码')} value={password} onChange={event => setPassword(event.target.value)} type="password" autoComplete="current-password" required disabled={pending || busy} />
          </div>
          <div className="device-authorization__actions">
            <Button type="button" size="lg" disabled={pending} loading={busy} onClick={() => void request('deny')}>{t('取消')}</Button>
            <Button variant="solid" type="submit" size="lg" loading={pending} disabled={busy}>{t('登录')}</Button>
          </div>
        </form> : <>
          {state.phase === 'loading' && state.action === 'preview' && <FeedbackState kind="loading" compact title={t('正在读取连接请求')} />}
          {preview && <div className="device-authorization__actions">
            <Button type="button" size="lg" disabled={busy} loading={state.phase === 'loading' && state.action === 'deny'} onClick={() => void request('deny')}>{t('取消')}</Button>
            <Button type="button" size="lg" variant="solid" disabled={busy} loading={state.phase === 'loading' && state.action === 'approve'} onClick={() => void request('approve')}>{t('授权')}</Button>
          </div>}
        </>}
        </>}
      </section>}
    </div>
  </main>
}
