import { afterEach, expect, it, vi } from 'vitest'

import { fetchModelCatalog } from './client'
import { clearAuthSession, saveAuthSession } from '../../auth/session'

afterEach(() => {
  clearAuthSession()
  vi.unstubAllGlobals()
})

it('loads the safe backend model catalog with authentication', async () => {
  saveAuthSession({
    token: 'model-token',
    serverAddress: 'http://127.0.0.1:8090', tokenType: 'Bearer',
    expiresAt: '2099-01-01T00:00:00.000Z',
    user: { user_id: 7, username: 'alice', display_name: 'Alice', avatar_url: null, roles: [], disabled: false },
  })
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = input instanceof Request ? input : new Request(input, init)
    expect(request.headers.get('Authorization')).toBe('Bearer model-token')
    return new Response(JSON.stringify({
      code: 0,
      message: 'success',
      data: {
        items: [{ modelId: 'main', displayName: 'Main Model', connectionId: 'test-provider', connectionDisplayName: '测试提供方', reasoningEnabled: true, isDefault: true }],
        defaultModelId: 'main',
      },
    }), { status: 200, headers: { 'Content-Type': 'application/json' } })
  })
  vi.stubGlobal('fetch', fetchMock)

  await expect(fetchModelCatalog()).resolves.toEqual({
    items: [{ modelId: 'main', displayName: 'Main Model', connectionId: 'test-provider', connectionDisplayName: '测试提供方', reasoningEnabled: true, isDefault: true }],
    defaultModelId: 'main',
  })
  const request = fetchMock.mock.calls[0]?.[0]
  expect(request).toBeInstanceOf(Request)
  expect(new URL((request as Request).url).pathname).toBe('/api/models')
})
