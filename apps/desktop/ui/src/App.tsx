import { CircleCheck, CircleMinus, Folders, Laptop, Link, LoaderCircle, Power, Settings2, ShieldCheck, UserRound } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { send, useDesktopState } from './bridge'
import { Button } from './components/ui/Button'
import { ErrorBoundary } from './components/ui/ErrorBoundary'
import { IconButton } from './components/ui/IconButton'
import { Account } from './features/Account'
import { Connection } from './features/Connection'
import { Permissions } from './features/Permissions'
import { UpdateDialog } from './features/UpdateDialog'
import { Workspace } from './features/Workspace'
import { LocaleContext, translate, useI18n } from './i18n'
import type { DesktopState, Page, ViewState } from './state'

const pages = [
  { id: 'connection', label: '连接', Icon: Link }, { id: 'workspace', label: '工作区', Icon: Folders },
  { id: 'permissions', label: '权限', Icon: ShieldCheck }, { id: 'account', label: '我的', Icon: UserRound },
] as const

function initialView(state: DesktopState): ViewState {
  return state.resume ?? {
    page: state.connected ? 'workspace' : 'connection', connectionForm: !state.connected && !state.service,
    service: state.service, api: state.api === state.service ? '' : state.api,
    manualAPI: Boolean(state.api && state.api !== state.service), editingName: false, name: state.name,
  }
}

function Workbench({ state }: { state: DesktopState }) {
  const { t } = useI18n()
  const [view, setView] = useState(() => initialView(state))
  const initialized = useRef(state.ready)
  const wasConnected = useRef(state.connected)
  const savedConnection = useRef({ service: state.service, api: state.api })
  const page = state.connected ? view.page : 'connection'
  const updateOpen = state.update.phase !== 'idle'
  const change = (patch: Partial<ViewState>) => setView(previous => ({ ...previous, ...patch }))
  useEffect(() => {
    if (initialized.current || !state.ready) return
    initialized.current = true
    setView(initialView(state))
  }, [state])
  useEffect(() => { if (initialized.current) void send({ type: 'save-view', view }) }, [view])
  useEffect(() => {
    const configurationChanged = savedConnection.current.service !== state.service || savedConnection.current.api !== state.api
    const newlyConnected = state.connected && !wasConnected.current
    if (!state.connected) setView(previous => previous.page === 'connection' && !previous.editingName ? previous
      : { ...previous, page: 'connection', editingName: false, name: state.name })
    else if (newlyConnected || configurationChanged) {
      setView(previous => ({ ...previous, page: newlyConnected ? 'workspace' : previous.page, connectionForm: false,
        ...(configurationChanged ? { service: state.service, api: state.api === state.service ? '' : state.api, manualAPI: state.api !== state.service } : {}) }))
    }
    if (state.connected) savedConnection.current = { service: state.service, api: state.api }
    wasConnected.current = state.connected
  }, [state.connected, state.service, state.api, state.name])

  const navigate = (target: Page) => change({ page: target, editingName: false, name: state.name,
    ...(target === 'connection' ? { connectionForm: true } : {}) })
  return <>
    <section className="client workbench" aria-label="TinkerFin" inert={updateOpen}>
      <div className="native-titlebar-space" aria-hidden="true" />
      <aside className="client-rail"><div className="brand"><img className="brand-mark" src="./brand.svg" alt="" aria-hidden="true" /><span>TinkerFin</span></div>
        <nav className="rail-nav" aria-label={t('工作台导航')}>{pages.map(({ id, label, Icon }) =>
          <button type="button" key={id} aria-current={page === id ? 'page' : undefined} disabled={state.busy === 'logout' || (id !== 'connection' && !state.connected)} onClick={() => navigate(id)}><Icon aria-hidden="true" /><span>{t(label)}</span></button>,
        )}</nav>
      </aside>
      <div className="client-main" hidden={page !== 'connection'}><Connection state={state} view={view} change={change} /></div>
      <div className="client-main" hidden={page !== 'workspace'}><Workspace state={state} /></div>
      <div className="client-main" hidden={page !== 'permissions'}><Permissions state={state} /></div>
      <div className="client-main account-pane" hidden={page !== 'account'}><Account state={state} view={view} change={change} /></div>
    </section>
    <UpdateDialog update={state.update} completed={() => change({ page: 'connection', connectionForm: false })} />
  </>
}

function Status({ state }: { state: DesktopState }) {
  const { t } = useI18n()
  const connecting = ['loading', 'connecting'].includes(state.connection)
  const Icon = state.connected ? CircleCheck : connecting ? LoaderCircle : CircleMinus
  return <section className="client status-popover" aria-label="TinkerFin" inert={state.update.phase !== 'idle'}>
    <div className="device-glyph"><Laptop aria-hidden="true" /></div>
    <div className="grow"><div className="device-title truncate">{state.name}</div><div className={`status status--${state.connected ? 'online' : connecting ? '' : 'offline'}`} role="status"><Icon className={connecting && !state.connected ? 'spinner' : undefined} aria-hidden="true" /><span>{t(state.connected ? '已连接' : connecting ? '连接中' : '未连接')}</span></div></div>
    <div className="popover-actions"><IconButton label={t('设置')} icon={<Settings2 />} variant="ghost" onClick={() => void send({ type: 'open-workbench' })} /><IconButton label={t('退出')} className="quit-button" icon={<Power />} variant="ghost" onClick={() => void send({ type: 'quit' })} /></div>
  </section>
}

export function App() {
  const state = useDesktopState()
  const [unavailable, setUnavailable] = useState(false)
  useEffect(() => { void send({ type: 'ready' }).then(result => setUnavailable(!result.ok)) }, [])
  useEffect(() => {
    if (!state) return
    document.documentElement.lang = state.locale
    document.documentElement.dataset.theme = state.theme
    document.documentElement.dataset.surface = state.surface
  }, [state])
  const locale = state?.locale ?? (navigator.language.startsWith('zh') ? 'zh-Hans' : 'en')
  const t = (key: string) => translate(locale, key)
  const fallback = <main className="interface-unavailable"><p>{t('界面暂不可用')}</p><Button onClick={() => location.reload()}>{t('重新打开')}</Button></main>
  return <LocaleContext.Provider value={{ t }}><ErrorBoundary fallback={() => fallback}>
    {unavailable ? fallback : !state ? <main className="interface-unavailable" role="status"><LoaderCircle className="spinner" aria-hidden="true" /><p>{t('正在启动')}</p></main>
      : state.surface === 'status' ? <Status state={state} /> : state.surface === 'updater' ? <UpdateDialog update={state.update} completed={() => {}} /> : <Workbench state={state} />}
  </ErrorBoundary></LocaleContext.Provider>
}
