import { lazy, Suspense, useCallback, useEffect, useRef, useState, useSyncExternalStore } from 'react'
import type { ReactNode } from 'react'

import {
  login,
  logout as logoutApi,
} from './api/auth/client'
import { getServerAddress, subscribeServerAddress } from './api/shared/config'
import { LoginTransition } from './features/auth/LoginTransition'
import { useAuthVerification } from './features/auth/useAuthVerification'
import { ApiError, AuthError, subscribeApiErrors } from './api/shared/http'
import { FeedbackState } from './components/ui/FeedbackState'
import { ToastViewport } from './components/ui/ToastViewport'
import type { ToastAction, ToastItem, ToastKind } from './components/ui/ToastViewport'
import {
  clearAuthSession,
  createAuthSession,
  getAuthSession,
  saveAuthSession,
  startAuthSessionLifecycle,
  subscribeAuthSession,
  AuthSessionStorageError,
} from './auth/session'
import { clearWorkspaceLocation, writeDeviceSettingsToLocation } from './lib/threadRoute'
import { clearActiveRunSession } from './features/conversation/stream/activeRunSession'
import { useI18n } from './i18n'
import type { LoginRequest } from './api/auth/types'
import { deviceAuthorizationServer, openDeviceClient, readDeviceAuthorizationIntent } from './features/devices/authorizationRoute'
import { DeviceAuthorization } from './features/devices/DeviceAuthorization'

type AuthPhase = 'checking' | 'signedOut' | 'transitioning' | 'signedIn'
type AuthEntry = 'restore' | 'manual'
let toastSequence = 0

const AuthScreen = lazy(async () => ({
  default: (await import('./features/auth/AuthScreen')).AuthScreen,
}))
const WorkspaceScreen = lazy(async () => ({
  default: (await import('./features/workspace/WorkspaceScreen')).WorkspaceScreen,
}))

export default function App() {
  const { t } = useI18n()
  const [deviceAuthorization, setDeviceAuthorization] = useState(readDeviceAuthorizationIntent)
  const session = useSyncExternalStore(subscribeAuthSession, getAuthSession, () => null)
  const [phase, setPhase] = useState<AuthPhase>(() => (
    getAuthSession() ? 'checking' : 'signedOut'
  ))
  const [isLoginPending, setLoginPending] = useState(false)
  const [authCheckVersion, setAuthCheckVersion] = useState(0)
  const toastOwner = session ? `${session.serverAddress}\n${session.user.user_id}\n${session.token}` : ''
  const [toasts, setToasts] = useState<Array<ToastItem & { owner: string }>>([])
  const lastToastOwner = useRef(toastOwner)
  const authEntry = useRef<AuthEntry>('restore')
  const loginRequest = useRef<AbortController | null>(null)
  useEffect(() => () => loginRequest.current?.abort(), [])
  const isAuthRetrying = useAuthVerification({
    enabled: phase === 'checking',
    version: authCheckVersion,
    onAuthenticated: () => {
      const entry = authEntry.current
      authEntry.current = 'restore'
      setPhase(
        entry === 'manual' && !deviceAuthorization
          && !window.matchMedia('(prefers-reduced-motion: reduce)').matches
          ? 'transitioning'
          : 'signedIn',
      )
    },
    onUnauthenticated: () => {
      clearAuthSession()
      setPhase('signedOut')
    },
  })

  const pushToast = useCallback((kind: ToastKind, message: string, action?: ToastAction) => {
    toastSequence += 1
    setToasts((current) => [
      ...current,
      { id: `toast-${toastSequence}`, kind, message, action, owner: toastOwner },
    ].slice(-4))
  }, [toastOwner])

  useEffect(() => subscribeServerAddress(() => {
    loginRequest.current?.abort()
    loginRequest.current = null
    setLoginPending(false)
    clearAuthSession()
    clearActiveRunSession()
    authEntry.current = 'restore'
    setPhase('signedOut')
  }), [])

  useEffect(() => subscribeApiErrors((error) => {
    pushToast('error', error.message)
  }), [pushToast])

  useEffect(() => subscribeAuthSession((session) => {
    const owner = session ? `${session.serverAddress}\n${session.user.user_id}\n${session.token}` : ''
    if (owner !== lastToastOwner.current) {
      lastToastOwner.current = owner
      setToasts([])
    }
    if (session) return
    authEntry.current = 'restore'
    clearActiveRunSession()
    setToasts([])
    if (!deviceAuthorization) clearWorkspaceLocation()
    setPhase('signedOut')
  }), [deviceAuthorization])

  useEffect(() => startAuthSessionLifecycle({
    onExternalSession: () => {
      authEntry.current = 'restore'
      setPhase((current) => current === 'signedOut' && !deviceAuthorization ? current : 'checking')
      setAuthCheckVersion((current) => current + 1)
    },
  }), [deviceAuthorization])

  useEffect(() => {
    if (phase === 'signedOut' && !deviceAuthorization) clearWorkspaceLocation()
  }, [phase, deviceAuthorization])

  const handleLogin = async (credentials: LoginRequest) => {
    if (loginRequest.current) return
    const serverAddress = getServerAddress()
    if (deviceAuthorization && serverAddress !== deviceAuthorizationServer()) {
      pushToast('error', t('授权连接已变化，请从客户端重新打开'))
      return
    }
    const controller = new AbortController()
    loginRequest.current = controller
    setLoginPending(true)
    try {
      const payload = await login(credentials, controller.signal)
      if (controller.signal.aborted || getServerAddress() !== serverAddress) return
      authEntry.current = 'manual'
      saveAuthSession(createAuthSession(payload))
      setPhase('checking')
      setAuthCheckVersion((current) => current + 1)
    } catch (error) {
      if (controller.signal.aborted) return
      if (error instanceof AuthError) {
        pushToast('error', error.message)
      } else if (error instanceof AuthSessionStorageError) {
        pushToast('error', t('浏览器无法保存登录状态，请检查隐私或存储设置后重试'))
      } else if (!(error instanceof ApiError)) {
        pushToast('error', t('登录失败，请稍后重试'))
      }
    } finally {
      if (loginRequest.current === controller) loginRequest.current = null
      if (!controller.signal.aborted) setLoginPending(false)
    }
  }

  const exitDeviceAuthorization = useCallback((id: string) => {
    if (!deviceAuthorization || id !== deviceAuthorization.id) return
    writeDeviceSettingsToLocation()
    setDeviceAuthorization(null)
    openDeviceClient()
  }, [deviceAuthorization])

  let content: ReactNode

  if (deviceAuthorization) {
    content = <DeviceAuthorization key={session?.token ?? 'signed-out'} intent={deviceAuthorization}
      session={phase === 'signedIn' ? session : null} checking={phase === 'checking'}
      pending={isLoginPending} onLogin={handleLogin} onExit={exitDeviceAuthorization} />
  } else if (phase === 'checking') {
    content = (
      <main
        className="auth-checking"
        aria-label={isAuthRetrying ? t('正在重新验证登录状态') : t('正在检查登录状态')}
      >
        <FeedbackState kind="loading" title={isAuthRetrying ? t('正在重新验证登录状态') : t('正在检查登录状态')} />
      </main>
    )
  } else if (phase === 'signedOut') {
    content = (
      <AuthScreen
        pending={isLoginPending}
        onError={(message) => pushToast('error', message)}
        onLogin={handleLogin}
      />
    )
  } else if (phase === 'transitioning') {
    content = <LoginTransition onComplete={() => setPhase('signedIn')} />
  } else {
    content = session ? (
      <WorkspaceScreen
        user={session.user}
        onToast={pushToast}
        onLogout={() => {
          const logoutRequest = logoutApi()
          clearAuthSession()
          void logoutRequest.catch(() => undefined)
        }}
      />
    ) : null
  }

  return (
    <>
      <a className="skip-link" href="#main-content">{t('跳到主要内容')}</a>
      <Suspense fallback={(
        <main id="main-content" className="auth-checking" aria-label={t('正在加载界面')}>
          <FeedbackState kind="loading" title={t('正在加载界面')} />
        </main>
      )}>
        {content}
      </Suspense>
      <ToastViewport
        toasts={toasts.filter(toast => toast.owner === toastOwner)}
        onDismiss={(id) => setToasts((current) => current.filter((toast) => toast.id !== id))}
      />
    </>
  )
}
