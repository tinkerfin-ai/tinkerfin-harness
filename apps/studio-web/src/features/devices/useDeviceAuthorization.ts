import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError } from '../../api/shared/http'
import { getAuthSession, type AuthSession } from '../../auth/session'
import { denyDeviceAuthorization, requestDeviceAuthorization, type DeviceAuthorizationPreview } from './api'
import type { DeviceAuthorizationIntent } from './authorizationRoute'

type AuthorizationAction = 'preview' | 'approve' | 'deny'
type AuthorizationState =
  | { phase: 'idle' | 'expired' | 'unavailable' }
  | { phase: 'loading'; action: AuthorizationAction; preview: DeviceAuthorizationPreview | null }
  | { phase: 'ready'; action: AuthorizationAction; preview: DeviceAuthorizationPreview }
  | { phase: 'retry'; action: 'preview' | 'deny'; preview: DeviceAuthorizationPreview | null }

const initialState = (authenticated: boolean): AuthorizationState => authenticated
  ? { phase: 'loading', action: 'preview', preview: null } : { phase: 'idle' }

const failedState = (error: unknown): 'expired' | 'unavailable' | 'retry' => {
  if (!(error instanceof ApiError)) return 'unavailable'
  if (error.code === 1_001_012_001) return 'expired'
  if ([1_001_012_000, 1_001_012_002].includes(error.code)) return 'unavailable'
  return error.status === 0 || error.status === 408 || error.status === 429 || error.status >= 500
    ? 'retry' : 'unavailable'
}

/** 预览与提交共享一个请求所有者；身份或入口变化后旧响应不能改写当前授权 */
export function useDeviceAuthorization(intent: DeviceAuthorizationIntent, session: AuthSession | null, checking: boolean) {
  const { id, browserToken } = intent
  const token = session?.token
  const tokenType = session?.tokenType
  const userId = session?.user.user_id
  const serverAddress = session?.serverAddress
  const owner = JSON.stringify([id, browserToken, serverAddress, token, tokenType, userId])
  const current = useRef({ owner, checking })
  current.current = { owner, checking }
  const [snapshot, setSnapshot] = useState<{ owner: string; state: AuthorizationState }>(() => ({ owner, state: initialState(Boolean(token)) }))
  const state = snapshot.owner === owner ? snapshot.state : initialState(Boolean(token))
  const latest = useRef(state)
  latest.current = state
  const operation = useRef<{ owner: string; controller: AbortController } | null>(null)

  const request = useCallback(async (action: AuthorizationAction) => {
    const matchesSession = () => {
      const active = getAuthSession()
      return token === undefined ? active === null : active?.token === token
        && active.tokenType === tokenType && active.user.user_id === userId && active.serverAddress === serverAddress
    }
    if (operation.current || current.current.owner !== owner || current.current.checking || !matchesSession()) return
    if (action !== 'deny' && (token === undefined || tokenType === undefined || userId === undefined)) return
    const previous = latest.current
    if (action === 'approve' && (previous.phase !== 'ready' || previous.preview.state !== 'waiting')) return
    const preview = 'preview' in previous ? previous.preview : null
    const owned = { owner, controller: new AbortController() }
    operation.current = owned
    const ownsRequest = () => operation.current === owned && !owned.controller.signal.aborted
      && current.current.owner === owner && !current.current.checking && matchesSession()
    setSnapshot({ owner, state: { phase: 'loading', action, preview } })
    try {
      const result = token !== undefined && tokenType !== undefined && userId !== undefined
        ? await requestDeviceAuthorization(id, action, browserToken, `${tokenType} ${token}`, userId, owned.controller.signal)
        : await denyDeviceAuthorization(id, browserToken, owned.controller.signal)
      if (ownsRequest()) setSnapshot({ owner, state: { phase: 'ready', action, preview: result } })
    } catch (error) {
      if (!ownsRequest()) return
      const phase = failedState(error)
      setSnapshot({ owner, state: phase === 'retry'
        ? { phase, action: action === 'deny' ? 'deny' : 'preview', preview }
        : { phase } })
    } finally {
      if (operation.current === owned) operation.current = null
    }
  }, [owner, id, browserToken, token, tokenType, userId, serverAddress])

  useEffect(() => {
    if (!checking && token !== undefined) void request('preview')
    return () => {
      const active = operation.current
      if (active?.owner === owner) {
        active.controller.abort()
        operation.current = null
      }
    }
  }, [owner, checking, token, request])

  return { state, request }
}
