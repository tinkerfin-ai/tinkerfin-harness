import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  clearAuthSession,
  getAuthSession,
  saveAuthSession,
} from '../../auth/session'
import type { AuthUser } from './types'
import { bootstrapAuthSession } from './client'
import { getServerAddress, setServerAddress } from '../shared/config'

const user = (id: number, name: string): AuthUser => ({
  user_id: id,
  username: name,
  display_name: name,
  avatar_url: null,
  roles: [],
  disabled: false,
})

const save = (token: string, currentUser: AuthUser) => saveAuthSession({
  token,
  serverAddress: getServerAddress(), tokenType: 'Bearer',
  expiresAt: '2099-01-01T00:00:00.000Z',
  user: currentUser,
})

const response = (currentUser: AuthUser, status = 200) => new Response(JSON.stringify({
  code: status === 200 ? 0 : 401,
  message: status === 200 ? 'success' : 'unauthorized',
  data: status === 200
    ? { expires_at: '2099-02-01T00:00:00.000Z', user: currentUser }
    : null,
}), {
  status,
  headers: { 'Content-Type': 'application/json' },
})

function deferred<T>() {
  let resolve: (value: T) => void = () => undefined
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise
  })
  return { promise, resolve }
}

describe('bootstrapAuthSession', () => {
  beforeEach(() => {
    window.localStorage.clear()
    clearAuthSession()
  })

  afterEach(() => {
    clearAuthSession()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('keeps old token responses from replacing a newer session', async () => {
    const tokenAResponse = deferred<Response>()
    const tokenBResponse = deferred<Response>()
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const request = input instanceof Request ? input : new Request(input, init)
      const authorization = request.headers.get('Authorization')
      if (authorization === 'Bearer token-a') return tokenAResponse.promise
      if (authorization === 'Bearer token-b') return tokenBResponse.promise
      throw new Error(`unexpected request: ${String(input)}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    save('token-a', user(1, 'user-a'))
    const verifyingA = bootstrapAuthSession()
    save('token-b', user(2, 'user-b'))
    const verifyingB = bootstrapAuthSession()

    tokenAResponse.resolve(response(user(1, 'verified-a')))
    await expect(verifyingA).resolves.toMatchObject({ status: 'stale' })
    expect(getAuthSession()).toMatchObject({
      token: 'token-b',
      user: { username: 'user-b' },
    })

    expect(bootstrapAuthSession()).toBe(verifyingB)
    tokenBResponse.resolve(response(user(2, 'verified-b')))
    await expect(verifyingB).resolves.toMatchObject({
      status: 'authenticated',
      session: { token: 'token-b', user: { username: 'verified-b' } },
    })
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('does not treat an old token authentication failure as logout for a new token', async () => {
    const tokenAResponse = deferred<Response>()
    vi.stubGlobal('fetch', vi.fn(() => tokenAResponse.promise))

    save('token-a', user(1, 'user-a'))
    const verifyingA = bootstrapAuthSession()
    save('token-b', user(2, 'user-b'))
    tokenAResponse.resolve(response(user(1, 'user-a'), 401))

    await expect(verifyingA).resolves.toMatchObject({ status: 'stale' })
    expect(getAuthSession()).toMatchObject({
      token: 'token-b',
      user: { username: 'user-b' },
    })
  })
  it.each([200, 401])('isolates equal tokens on different servers for a late %s response', async (status) => {
    const first = deferred<Response>()
    const second = deferred<Response>()
    const requested: string[] = []
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
      const request = input instanceof Request ? input : new Request(input)
      requested.push(new URL(request.url).origin)
      return request.url.startsWith('http://127.0.0.1:8090') ? first.promise : second.promise
    }))
    save('same-token', user(1, 'first'))
    const verifyingFirst = bootstrapAuthSession()
    setServerAddress('https://second.example')
    save('same-token', user(2, 'second'))
    const verifyingSecond = bootstrapAuthSession()
    expect(verifyingSecond).not.toBe(verifyingFirst)
    first.resolve(response(user(1, 'wrong-server'), status))
    await expect(verifyingFirst).resolves.toMatchObject({ status: 'stale' })
    expect(getAuthSession()?.user.username).toBe('second')
    second.resolve(response(user(2, 'verified-second')))
    await expect(verifyingSecond).resolves.toMatchObject({ status: 'authenticated' })
    expect(requested).toEqual(['http://127.0.0.1:8090', 'https://second.example'])
  })

})
