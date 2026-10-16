import { useEffect, useRef, useState } from 'react'

import { bootstrapAuthSession } from '../../api/auth/client'

const AUTH_RETRY_DELAYS_MS = [1000, 2000, 4000, 8000, 10000] as const

interface AuthVerificationOptions {
  enabled: boolean
  version: number
  onAuthenticated: () => void
  onUnauthenticated: () => void
}

/** 在阻塞认证界面管理服务端校验、封顶退避与恢复触发 */
export function useAuthVerification({
  enabled,
  version,
  onAuthenticated,
  onUnauthenticated,
}: AuthVerificationOptions): boolean {
  const [retryingVersion, setRetryingVersion] = useState<number | null>(null)
  const callbacks = useRef({ onAuthenticated, onUnauthenticated })
  callbacks.current = { onAuthenticated, onUnauthenticated }

  useEffect(() => {
    if (!enabled) return
    let isActive = true
    let isChecking = false
    let isTerminal = false
    let retryRequested = false
    let retryAttempt = 0
    let retryTimer: number | null = null

    const clearRetryTimer = () => {
      if (retryTimer == null) return
      window.clearTimeout(retryTimer)
      retryTimer = null
    }

    const verify = () => {
      if (!isActive) return
      if (isChecking) {
        retryRequested = true
        return
      }
      clearRetryTimer()
      isChecking = true
      void bootstrapAuthSession().then((result) => {
        if (!isActive) return
        if (result.status === 'authenticated') {
          isTerminal = true
          callbacks.current.onAuthenticated()
          return
        }
        if (result.status === 'unauthenticated') {
          isTerminal = true
          callbacks.current.onUnauthenticated()
          return
        }
        setRetryingVersion(version)
        const delay = AUTH_RETRY_DELAYS_MS[
          Math.min(retryAttempt, AUTH_RETRY_DELAYS_MS.length - 1)
        ]
        retryAttempt += 1
        retryTimer = window.setTimeout(verify, delay)
      }).finally(() => {
        isChecking = false
        if (retryRequested && isActive && !isTerminal) {
          retryRequested = false
          verify()
        }
      })
    }

    const retryNow = () => {
      clearRetryTimer()
      verify()
    }
    const retryWhenVisible = () => {
      if (document.visibilityState === 'visible') retryNow()
    }

    window.addEventListener('focus', retryNow)
    window.addEventListener('online', retryNow)
    document.addEventListener('visibilitychange', retryWhenVisible)
    verify()

    return () => {
      isActive = false
      clearRetryTimer()
      window.removeEventListener('focus', retryNow)
      window.removeEventListener('online', retryNow)
      document.removeEventListener('visibilitychange', retryWhenVisible)
    }
  }, [enabled, version])

  return enabled && retryingVersion === version
}
