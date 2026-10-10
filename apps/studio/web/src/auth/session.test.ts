import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { LoginResponse } from '../api/auth/types'
import {
  AUTH_SESSION_STORAGE_KEY,
  AuthSessionStorageError,
  clearAuthSession,
  createAuthSession,
  getAuthorizationHeader,
  getAuthSession,
  saveAuthSession,
  startAuthSessionLifecycle,
  subscribeAuthSession,
} from './session'

const user = {
  user_id: 7,
  username: 'yunsan',
  avatar_url: null,
  roles: [],
  disabled: false,
}

function loginPayload(expiresAt: string): LoginResponse {
  return {
    access_token: 'token-123',
    token_type: 'Bearer',
    expires_at: expiresAt,
    user,
  }
}

describe('auth session lifecycle', () => {
  beforeEach(() => {
    window.localStorage.clear()
    clearAuthSession()
  })

  afterEach(() => {
    clearAuthSession()
    vi.restoreAllMocks()
    vi.useRealTimers()
  })

  it('clears the session exactly when its fixed deadline is reached', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-08-22T10:00:00Z'))
    saveAuthSession(createAuthSession(loginPayload('2026-08-22T10:00:01Z')))
    const stop = startAuthSessionLifecycle()

    await vi.advanceTimersByTimeAsync(999)
    expect(getAuthSession()).not.toBeNull()

    await vi.advanceTimersByTimeAsync(1)
    expect(getAuthSession()).toBeNull()
    expect(window.localStorage.getItem(AUTH_SESSION_STORAGE_KEY)).toBeNull()
    stop()
  })

  it('rechecks the deadline before sending an authenticated request', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-08-22T10:00:00Z'))
    saveAuthSession(createAuthSession(loginPayload('2026-08-22T10:00:01Z')))

    vi.setSystemTime(new Date('2026-08-22T10:00:02Z'))

    expect(getAuthorizationHeader()).toBeNull()
    expect(window.localStorage.getItem(AUTH_SESSION_STORAGE_KEY)).toBeNull()
  })

  it('synchronizes external replacement internally and external removal as logout', () => {
    saveAuthSession(createAuthSession(loginPayload('2099-01-01T00:00:00Z')))
    const onExternalSession = vi.fn()
    const stop = startAuthSessionLifecycle({ onExternalSession })
    const replacement = {
      ...createAuthSession(loginPayload('2099-02-01T00:00:00Z')),
      token: 'new-token',
    }
    window.localStorage.setItem(AUTH_SESSION_STORAGE_KEY, JSON.stringify(replacement))

    window.dispatchEvent(new StorageEvent('storage', {
      key: AUTH_SESSION_STORAGE_KEY,
      newValue: JSON.stringify(replacement),
    }))

    expect(getAuthSession()?.token).toBe('new-token')
    expect(onExternalSession).toHaveBeenCalledWith(replacement)

    window.localStorage.removeItem(AUTH_SESSION_STORAGE_KEY)
    window.dispatchEvent(new StorageEvent('storage', {
      key: AUTH_SESSION_STORAGE_KEY,
      newValue: null,
    }))

    expect(getAuthSession()).toBeNull()
    stop()
  })

  it('does not commit memory state or notify listeners when persistence fails', () => {
    const original = createAuthSession(loginPayload('2099-01-01T00:00:00Z'))
    saveAuthSession(original)
    const listener = vi.fn()
    const unsubscribe = subscribeAuthSession(listener)
    vi.spyOn(window.localStorage, 'setItem').mockImplementation(() => {
      throw new DOMException('storage disabled', 'SecurityError')
    })

    const replacement = {
      ...createAuthSession(loginPayload('2099-02-01T00:00:00Z')),
      token: 'replacement-token',
    }

    expect(() => saveAuthSession(replacement)).toThrow(AuthSessionStorageError)
    expect(getAuthSession()).toEqual(original)
    expect(listener).not.toHaveBeenCalled()
    unsubscribe()
  })

  it('clears the in-memory session even when persistent removal is unavailable', () => {
    saveAuthSession(createAuthSession(loginPayload('2099-01-01T00:00:00Z')))
    vi.spyOn(window.localStorage, 'removeItem').mockImplementation(() => {
      throw new DOMException('storage disabled', 'SecurityError')
    })

    expect(() => clearAuthSession()).not.toThrow()
    expect(getAuthSession()).toBeNull()
  })
})
