import { lazy, Suspense, useCallback, useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'

import {
  login,
  logout as logoutApi,
} from './api/auth/client'
import { LoginTransition } from './features/auth/LoginTransition'
import { useAuthVerification } from './features/auth/useAuthVerification'
import { ApiError, AuthError, subscribeApiErrors } from './api/shared/http'
import { ToastViewport } from './components/ui/ToastViewport'
import type { ToastItem, ToastKind } from './components/ui/ToastViewport'
import {
  clearAuthSession,
  createAuthSession,
  getAuthSession,
  saveAuthSession,
  startAuthSessionLifecycle,
  subscribeAuthSession,
  AuthSessionStorageError,
} from './auth/session'
import { writeWorkspaceToLocation } from './lib/threadRoute'
import { clearActiveRunSession } from './features/conversation/stream/activeRunSession'
import { useI18n } from './i18n'

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
  const [phase, setPhase] = useState<AuthPhase>(() => (
    getAuthSession() ? 'checking' : 'signedOut'
  ))
  const [isLoginPending, setLoginPending] = useState(false)
  const [authCheckVersion, setAuthCheckVersion] = useState(0)
  const [toasts, setToasts] = useState<ToastItem[]>([])
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
        entry === 'manual'
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

  const pushToast = useCallback((kind: ToastKind, message: string) => {
    toastSequence += 1
    setToasts((current) => [
      ...current,
      { id: `toast-${toastSequence}`, kind, message },
    ].slice(-4))
  }, [])

  useEffect(() => subscribeApiErrors((error) => {
    pushToast('error', error.message)
  }), [pushToast])

  useEffect(() => subscribeAuthSession((session) => {
    if (session) return
    authEntry.current = 'restore'
    clearActiveRunSession()
    setToasts([])
    writeWorkspaceToLocation('conversation', '')
    setPhase('signedOut')
  }), [])

  useEffect(() => startAuthSessionLifecycle({
    onExternalSession: () => {
      authEntry.current = 'restore'
      setPhase((current) => current === 'signedOut' ? current : 'checking')
      setAuthCheckVersion((current) => current + 1)
    },
  }), [])

  useEffect(() => {
    if (phase === 'signedOut') writeWorkspaceToLocation('conversation', '')
  }, [phase])

  let content: ReactNode

  if (phase === 'checking') {
    content = (
      <main
        className="auth-checking"
        aria-label={isAuthRetrying ? t('正在重新验证登录状态') : t('正在检查登录状态')}
      >
        <span className="auth-checking__mark" aria-hidden="true" />
      </main>
    )
  } else if (phase === 'signedOut') {
    content = (
      <AuthScreen
        pending={isLoginPending}
        onLogin={async (credentials) => {
          if (loginRequest.current) return
          const controller = new AbortController()
          loginRequest.current = controller
          setLoginPending(true)
          try {
            const payload = await login(credentials, controller.signal)
            if (controller.signal.aborted) return
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
        }}
      />
    )
  } else if (phase === 'transitioning') {
    content = <LoginTransition onComplete={() => setPhase('signedIn')} />
  } else {
    const session = getAuthSession()
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
          <span className="auth-checking__mark" aria-hidden="true" />
        </main>
      )}>
        {content}
      </Suspense>
      <ToastViewport
        toasts={toasts}
        onDismiss={(id) => setToasts((current) => current.filter((toast) => toast.id !== id))}
      />
    </>
  )
}
