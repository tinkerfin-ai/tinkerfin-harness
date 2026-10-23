import { ArrowUpRight, ChevronRight, Globe, Link, LoaderCircle, RotateCcw, RotateCw, TriangleAlert } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Button } from '../components/ui/Button'
import { IconButton } from '../components/ui/IconButton'
import { TextField } from '../components/ui/TextField'
import { ValidatedForm } from '../components/ui/ValidatedForm'
import { send } from '../bridge'
import { useI18n } from '../i18n'
import type { DesktopState, ViewState } from '../state'
import { useAction } from './useAction'

function addressError(value: string) {
  if (!value.trim()) return '请输入服务地址'
  try {
    const url = new URL(value.trim())
    if (!['http:', 'https:'].includes(url.protocol) || !url.hostname || url.username || url.password || url.search || url.hash) return '请输入完整的 http 或 https 地址'
    return ''
  } catch { return '请输入完整的 http 或 https 地址' }
}

interface Props { state: DesktopState; view: ViewState; change: (patch: Partial<ViewState>) => void }

export function Connection({ state, view, change }: Props) {
  const { t } = useI18n()
  const [errors, setErrors] = useState({ service: '', api: '' })
  const [attempt, setAttempt] = useState(0)
  const action = useAction()
  const [authorizationError, setAuthorizationError] = useState('')
  const busy = action.pending || state.busy === 'connect'
  const changed = state.connected && (view.service.trim().replace(/\/$/, '') !== state.service || (view.manualAPI && view.api.trim() ? view.api.trim().replace(/\/$/, '') : state.api) !== state.api)
  const awaiting = state.authorizationPhase !== 'idle' || state.connection === 'authorizing'
  const connecting = state.connection === 'connecting' || state.connection === 'loading'
  const disconnected = state.connection === 'offline' && (!view.connectionForm || !state.ready)
  useEffect(() => {
    if (!awaiting) setAuthorizationError('')
  }, [awaiting])
  useEffect(() => {
    if (view.page !== 'connection' || !changed || busy || state.update.phase !== 'idle') return
    const cancel = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || event.isComposing || event.defaultPrevented) return
      event.preventDefault()
      change({ service: state.service, api: state.api === state.service ? '' : state.api, manualAPI: state.api !== state.service })
      setErrors({ service: '', api: '' })
    }
    document.addEventListener('keydown', cancel)
    return () => document.removeEventListener('keydown', cancel)
  }, [view.page, changed, busy, state.update.phase, state.service, state.api, change])

  function submit() {
    setAuthorizationError('')
    const invalid = { service: addressError(view.service), api: view.manualAPI && view.api.trim() ? addressError(view.api) : '' }
    setErrors(invalid); setAttempt(value => value + 1)
    if (invalid.service || invalid.api) return
    void action.run({ type: 'connect', service: view.service.trim(), api: view.manualAPI ? view.api.trim() : '' }, result => {
      if (!result.ok) {
        const api = result.field === 'api'
        setErrors({ service: api ? '' : '请检查地址后重试', api: api ? '无法识别 API 地址，请填写' : '' })
        if (api) change({ manualAPI: true })
        setAttempt(value => value + 1)
      }
    })
  }

  if (awaiting || connecting || disconnected) {
    const warning = disconnected || Boolean(authorizationError || state.authorizationError)
    const Icon = warning ? TriangleAlert : awaiting ? Globe : state.connection === 'loading' ? LoaderCircle : Link
    const title = awaiting ? '请在浏览器完成授权' : disconnected ? (state.connectionError ? '连接失败' : '连接已中断') : state.connection === 'loading' ? '正在启动' : '正在完成连接'
    const description = disconnected ? (state.connectionError || '请检查网络后重新连接')
      : state.connection === 'connecting' && state.connectionReason === 'authorization' ? '授权已完成，正在连接服务' : ''
    const authBusy = busy || ['starting', 'cancelling'].includes(state.authorizationPhase)
    return <>
      <main className="client-body" aria-label={t('连接')}>
        <div className={`state-block${warning ? ' state-block--warning' : ''}`} role="status">
          <div className="state-icon"><Icon className={state.connection === 'loading' ? 'spinner' : undefined} aria-hidden="true" /></div>
          <h2>{t(title)}</h2>{description && <p>{t(description)}</p>}
          {awaiting && (authorizationError || state.authorizationError) && <p role="alert">{t(authorizationError || state.authorizationError)}</p>}
        </div>
        {awaiting && state.switching && <p className="consequence">{t('新授权成功后将更换连接，本机执行会中断')}</p>}
      </main>
      <footer className="pane-actions">{awaiting ? <>
        <Button variant="text" disabled={authBusy} loading={state.authorizationPhase === 'cancelling'} onClick={() => void action.run({ type: 'cancel-authorization' }, result => {
          if (result.ok) { change({ connectionForm: true }); setAuthorizationError('') }
          else setAuthorizationError(result.message)
        })}>{t('取消授权')}</Button>
        <Button variant="primary" disabled={authBusy} loading={state.authorizationPhase === 'starting'} leadingIcon={<ArrowUpRight />} onClick={() => void action.run({ type: 'open-authorization' }, result => setAuthorizationError(result.ok ? '' : result.message))}>{t('重新打开浏览器')}</Button>
      </> : <Button variant="primary" disabled={connecting || busy} loading={connecting || busy} leadingIcon={<RotateCw />} onClick={() => void action.run({ type: 'retry' })}>{t('重新连接')}</Button>}</footer>
    </>
  }

  return <>
    <main className="client-body" aria-label={t('连接')}>
      <ValidatedForm id="connection-form" className="connection-form" errors={errors} validationAttempt={attempt} onSubmit={event => { event.preventDefault(); submit() }}>
        <TextField id="service-address" name="service" label={t('服务地址')} value={view.service} onChange={event => { change({ service: event.target.value }); setErrors(previous => ({ ...previous, service: '' })) }}
          placeholder="https://studio.example.com" maxLength={4096} autoComplete="off" spellCheck={false} inputMode="url" disabled={busy} error={errors.service && t(errors.service)} rootClassName="desktop-field" fieldSize="md" shape="standard" />
        {view.manualAPI ? <div className="connection-field api-manual">
          <TextField id="api-address" name="api" label={t('API 地址')} value={view.api} onChange={event => { change({ api: event.target.value }); setErrors(previous => ({ ...previous, api: '' })) }}
            placeholder="https://api.example.com" maxLength={4096} autoComplete="off" spellCheck={false} inputMode="url" disabled={busy} error={errors.api && t(errors.api)} rootClassName="desktop-field" fieldSize="md" shape="standard" />
          <IconButton label={t('恢复自动识别')} tooltip={t('恢复自动识别')} icon={<RotateCcw size={16} />} variant="ghost" disabled={busy} onClick={() => { change({ manualAPI: false }); setErrors(previous => ({ ...previous, api: '' })) }} />
        </div> : <div className="connection-row"><span className="field-label">{t('API 地址')}</span><div className="api-default"><span>{t('自动识别')}</span><Button variant="text" disabled={busy} trailingIcon={<ChevronRight />} onClick={() => change({ manualAPI: true })}>{t('手动填写')}</Button></div></div>}
        {changed && <p className="consequence">{t('新授权成功后将更换连接，本机执行会中断')}</p>}
        {(state.connectionError || state.connection === 'revoked') && <p className="consequence">{t(state.connectionError || '授权已失效，请重新连接')}</p>}
      </ValidatedForm>
    </main>
    <footer className="pane-actions">
      {changed && <Button variant="text" disabled={busy} onClick={() => { change({ service: state.service, api: state.api === state.service ? '' : state.api, manualAPI: state.api !== state.service }); setErrors({ service: '', api: '' }) }}>{t('取消')}</Button>}
      {state.connected && !changed && !errors.service && !errors.api
        ? <Button variant="primary" leadingIcon={<ArrowUpRight />} onClick={() => void send({ type: 'open-studio' })}>{t('打开 Studio')}</Button>
        : <Button type="submit" form="connection-form" variant="primary" loading={busy} disabled={!state.ready}>{t('连接并授权')}</Button>}
    </footer>
  </>
}
