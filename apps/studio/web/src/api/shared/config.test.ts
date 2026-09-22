import { describe, expect, it, vi } from 'vitest'
import { buildApiUrl, DEFAULT_SERVER_ADDRESS, getServerAddress, normalizeServerAddress, SERVER_ADDRESS_STORAGE_KEY, setServerAddress, subscribeServerAddress } from './config'
import { getAuthSession, clearAuthSession, createAuthSession, getAuthorizationHeader, saveAuthSession } from '../../auth/session'

const session = () => createAuthSession({ access_token: 'secret', token_type: 'Bearer', expires_at: '2099-01-01T00:00:00Z', user: { user_id: 1, username: 'test', display_name: 'Test', avatar_url: null, roles: [], disabled: false } })

describe('服务器地址', () => {
  it('uses the default when blank and persists a normalized custom address', () => {
    expect(getServerAddress()).toBe(DEFAULT_SERVER_ADDRESS)
    setServerAddress(' https://server.example/studio/ ')
    expect(getServerAddress()).toBe('https://server.example/studio')
    expect(localStorage.getItem(SERVER_ADDRESS_STORAGE_KEY)).toBe('https://server.example/studio')
    expect(buildApiUrl('/api/auth/login')).toBe('https://server.example/studio/api/auth/login')
    setServerAddress('')
    expect(localStorage.getItem(SERVER_ADDRESS_STORAGE_KEY)).toBeNull()
    expect(getServerAddress()).toBe(DEFAULT_SERVER_ADDRESS)
  })

  it.each(['localhost:8090', '/backend', 'ftp://server.test', 'https://u:p@server.test', 'https://server.test?x=1', 'https://server.test#x'])('rejects invalid addresses without replacing storage: %s', (value) => {
    setServerAddress('https://server.example')
    expect(() => setServerAddress(value)).toThrow()
    expect(getServerAddress()).toBe('https://server.example')
  })

  it('reports storage failures instead of silently using another address', () => {
    const spy = vi.spyOn(localStorage, 'setItem').mockImplementation(() => { throw new Error('blocked') })
    try { expect(() => setServerAddress('https://server.example')).toThrow() }
    finally { spy.mockRestore() }
    localStorage.setItem(SERVER_ADDRESS_STORAGE_KEY, 'invalid')
    expect(() => getServerAddress()).toThrow()
  })

  it('notifies same-page and external changes and unsubscribes', () => {
    const listener = vi.fn()
    const stop = subscribeServerAddress(listener)
    setServerAddress('https://server.example')
    window.dispatchEvent(new StorageEvent('storage', { key: SERVER_ADDRESS_STORAGE_KEY }))
    expect(listener).toHaveBeenCalledTimes(2)
    stop()
    setServerAddress('')
    expect(listener).toHaveBeenCalledTimes(2)
  })

  it('never sends a saved token to another server or accepts a late session from it', () => {
    clearAuthSession()
    const original = session()
    saveAuthSession(original)
    expect(getAuthorizationHeader()).toBe('Bearer secret')
    setServerAddress('https://another.example')
    expect(getAuthorizationHeader()).toBeNull()
    saveAuthSession(original)
    expect(getAuthorizationHeader()).toBeNull()
    expect(normalizeServerAddress('')).toBe(DEFAULT_SERVER_ADDRESS)
  })
  it('clears the in-memory session when its server configuration cannot be read', () => {
    clearAuthSession()
    saveAuthSession(session())
    const spy = vi.spyOn(localStorage, 'getItem').mockImplementation(() => { throw new Error('blocked') })
    try { expect(getAuthSession()).toBeNull() }
    finally { spy.mockRestore() }
    expect(getAuthorizationHeader()).toBeNull()
  })

})
