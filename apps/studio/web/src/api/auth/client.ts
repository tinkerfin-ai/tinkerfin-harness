import type { ApiError } from '../shared/http'
import { AuthError, requestJson } from '../shared/http'
import type { AuthSessionResponse, LoginRequest, LoginResponse } from './types'
import type { AuthSession } from '../../auth/session'
import { getAuthorizationHeader, getAuthSession, updateAuthSession } from '../../auth/session'

export interface BootstrapAuthResult {
  status: 'authenticated' | 'unauthenticated' | 'stale'
  session: AuthSession | null
  error?: ApiError
}

const bootstrapRequests = new Map<string, Promise<BootstrapAuthResult>>()

export function login(input: LoginRequest, signal?: AbortSignal) {
  return requestJson<LoginResponse>('/api/auth/login', {
    method: 'POST',
    body: input,
    signal,
    requiresAuth: false,
    suppressAuthFailure: true,
  })
}

export function getCurrentSession(
  signal?: AbortSignal,
  options?: { suppressAuthFailure?: boolean; authorization?: string },
) {
  return requestJson<AuthSessionResponse>('/api/auth/me', {
    signal,
    headers: options?.authorization
      ? { Authorization: options.authorization }
      : undefined,
    suppressGlobalError: true,
    suppressAuthFailure: options?.suppressAuthFailure ?? false,
  })
}

export function logout(signal?: AbortSignal) {
  const authorization = getAuthorizationHeader()
  return requestJson<null>('/api/auth/logout', {
    method: 'POST',
    headers: authorization ? { Authorization: authorization } : undefined,
    signal,
    suppressGlobalError: true,
    suppressAuthFailure: true,
  })
}

export function bootstrapAuthSession(): Promise<BootstrapAuthResult> {
  const session = getAuthSession()
  if (!session) {
    return Promise.resolve({ status: 'unauthenticated', session: null })
  }

  const requestKey = JSON.stringify([session.serverAddress, session.token])
  const isCurrentSession = () => {
    const current = getAuthSession()
    return current?.serverAddress === session.serverAddress && current.token === session.token
  }
  const requestToken = session.token
  const requestAuthorization = `${session.tokenType || 'Bearer'} ${requestToken}`
  const inFlight = bootstrapRequests.get(requestKey)
  if (inFlight) return inFlight

  const staleResult = (): BootstrapAuthResult => ({
    status: 'stale',
    session: getAuthSession(),
  })
  const request = getCurrentSession(undefined, {
    suppressAuthFailure: true,
    authorization: requestAuthorization,
  })
    .then((payload) => {
      if (!isCurrentSession()) return staleResult()
      const updated = updateAuthSession(payload, requestToken)
      if (!updated) {
        return staleResult()
      }
      return {
        status: 'authenticated' as const,
        session: updated,
      }
    })
    .catch((error) => {
      if (!isCurrentSession()) return staleResult()
      if (error instanceof AuthError) {
        return {
          status: 'unauthenticated' as const,
          session: null,
        }
      }

      return {
        status: 'stale' as const,
        session: getAuthSession(),
        error: error as ApiError,
      }
    })
    .finally(() => {
      queueMicrotask(() => {
        if (bootstrapRequests.get(requestKey) === request) {
          bootstrapRequests.delete(requestKey)
        }
      })
    })

  bootstrapRequests.set(requestKey, request)
  return request
}
