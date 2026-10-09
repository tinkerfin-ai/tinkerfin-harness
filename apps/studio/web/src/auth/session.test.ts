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

  it('stores the exact absolute deadline returned by the backend', () => {
    const session = createAuthSession(loginPayload('2026-08-23T10:00:00Z'))

    expect(session.expiresAt).toBe('2026-08-23T10:00:00.000Z')
  })

  it('rejects a stored session that does not match the current user shape', () => {
    const stored = createAuthSession(loginPayload('2099-01-01T00:00:00Z'))
    const incompleteUser = { ...stored.user } as Partial<typeof stored.user>
    delete incompleteUser.avatar_url
    const serialized = JSON.stringify({
      ...stored,
      user: incompleteUser,
    })
    const stop = startAuthSessionLifecycle()
    window.localStorage.setItem(AUTH_SESSION_STORAGE_KEY, serialized)
    window.dispatchEvent(new StorageEvent('storage', {
      key: AUTH_SESSION_STORAGE_KEY,
      newValue: serialized,
    }))

    expect(getAuthSession()).toBeNull()
    expect(window.localStorage.getItem(AUTH_SESSION_STORAGE_KEY)).toBeNull()
    stop()
  })

  it('rejects a login response without a valid absolute deadline', () => {
    expect(() => createAuthSession(loginPayload('not-a-date'))).toThrow(
      '登录接口返回的固定到期时间无效',
    )
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

  it('rechecks an overdue session when the page regains focus', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-08-22T10:00:00Z'))
    saveAuthSession(createAuthSession(loginPayload('2026-08-22T10:00:01Z')))
    const stop = startAuthSessionLifecycle()

    vi.setSystemTime(new Date('2026-08-22T10:00:02Z'))
    window.dispatchEvent(new Event('focus'))

    expect(getAuthSession()).toBeNull()
    stop()
  })

  it('rechecks an overdue session when a background page becomes visible', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-08-22T10:00:00Z'))
    saveAuthSession(createAuthSession(loginPayload('2026-08-22T10:00:01Z')))
    const stop = startAuthSessionLifecycle()
    vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible')

    vi.setSystemTime(new Date('2026-08-22T10:00:02Z'))
    document.dispatchEvent(new Event('visibilitychange'))

    expect(getAuthSession()).toBeNull()
    stop()
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

  it('keeps a profile-only external update in the same authenticated workspace', () => {
    const initial = createAuthSession(loginPayload('2099-01-01T00:00:00Z'))
    saveAuthSession(initial)
    const onExternalSession = vi.fn()
    const onSession = vi.fn()
    const unsubscribe = subscribeAuthSession(onSession)
    const stop = startAuthSessionLifecycle({ onExternalSession })
    const changed = { ...initial, user: { ...initial.user, avatar_url: 'https://files.example.test/avatars/image.jpg' } }
    window.localStorage.setItem(AUTH_SESSION_STORAGE_KEY, JSON.stringify(changed))
    window.dispatchEvent(new StorageEvent('storage', { key: AUTH_SESSION_STORAGE_KEY }))
    expect(getAuthSession()?.user.avatar_url).toBe(changed.user.avatar_url)
    expect(onSession).toHaveBeenCalledWith(changed)
    expect(onExternalSession).not.toHaveBeenCalled()
    const authorizationChanged = { ...changed, user: { ...changed.user, roles: ['admin'] } }
    window.localStorage.setItem(AUTH_SESSION_STORAGE_KEY, JSON.stringify(authorizationChanged))
    window.dispatchEvent(new StorageEvent('storage', { key: AUTH_SESSION_STORAGE_KEY }))
    expect(onExternalSession).toHaveBeenCalledWith(authorizationChanged)
    unsubscribe(); stop()
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
