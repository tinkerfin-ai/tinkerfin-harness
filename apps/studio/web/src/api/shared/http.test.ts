import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  AUTH_SESSION_STORAGE_KEY,
  clearAuthSession,
  saveAuthSession,
  subscribeAuthFailure,
} from '../../auth/session'
import {
  ApiError,
  AuthError,
  apiClient,
  requestEventStream,
  requestJson,
  subscribeApiErrors,
  type ApiAxiosRequestConfig,
} from './http'
import { LANGUAGE_STORAGE_KEY } from '../../i18n'

function jsonResponse(data: unknown, code = 0, message = 'success', status = 200) {
  return new Response(
    JSON.stringify({
      code,
      message,
      data,
    }),
    {
      status,
      headers: {
        'Content-Type': 'application/json',
      },
    },
  )
}

function seedAuthSession(token = 'token-123') {
  saveAuthSession({
    token,
    tokenType: 'Bearer',
    expiresAt: '2099-01-01T00:00:00.000Z',
    user: {
      user_id: 7,
      username: 'yunsan',
      display_name: 'Yunsan',
      avatar_url: null,
      roles: [],
      disabled: false,
    },
  })
}

describe('shared HTTP client', () => {
  afterEach(() => {
    vi.useRealTimers()
    clearAuthSession()
    vi.unstubAllGlobals()
    vi.unstubAllEnvs()
  })

  it('bounds response-header waiting without presenting timeout as caller cancellation', async () => {
    vi.useFakeTimers()
    const caller = new AbortController()
    let requestSignal: AbortSignal | undefined
    vi.stubGlobal('fetch', vi.fn((_input, init: RequestInit) => new Promise((_resolve, reject) => {
      requestSignal = init.signal ?? undefined
      requestSignal?.addEventListener('abort', () => reject(requestSignal?.reason), { once: true })
    })))
    const request = requestEventStream('/api/conversation/chat', {
      requiresAuth: false, signal: caller.signal, suppressGlobalError: true,
    })
    const assertion = expect(request).rejects.toMatchObject({ name: 'ApiError', status: 0 })
    await vi.advanceTimersByTimeAsync(45_000)
    await assertion
    expect(requestSignal?.aborted).toBe(true)
    expect(caller.signal.aborted).toBe(false)
    expect(vi.getTimerCount()).toBe(0)
  })

  it('clears the header deadline while keeping caller cancellation connected to a long response', async () => {
    vi.useFakeTimers()
    const caller = new AbortController()
    const addListener = vi.spyOn(caller.signal, 'addEventListener')
    let requestSignal: AbortSignal | undefined
    const body = new ReadableStream<Uint8Array>()
    vi.stubGlobal('fetch', vi.fn(async (_input, init: RequestInit) => {
      requestSignal = init.signal ?? undefined
      return new Response(body, { headers: { 'Content-Type': 'text/event-stream' } })
    }))
    const response = await requestEventStream('/api/conversation/chat', { requiresAuth: false, signal: caller.signal })
    await vi.advanceTimersByTimeAsync(180_000)
    expect(requestSignal?.aborted).toBe(false)
    expect(vi.getTimerCount()).toBe(0)
    // 原生组合信号不在长寿命调用方上累积手动转发监听器
    expect(addListener).not.toHaveBeenCalled()
    caller.abort()
    expect(requestSignal?.aborted).toBe(true)
    await response.body?.cancel()
  })

  it.each([false, true])('preserves caller abort before headers (already aborted: %s)', async (alreadyAborted) => {
    vi.useFakeTimers()
    const caller = new AbortController()
    const reason = new Error('调用方结束接收')
    if (alreadyAborted) caller.abort(reason)
    vi.stubGlobal('fetch', vi.fn((_input, init: RequestInit) => new Promise((_resolve, reject) => {
      if (init.signal?.aborted) reject(init.signal.reason)
      else init.signal?.addEventListener('abort', () => reject(init.signal?.reason), { once: true })
    })))
    const request = requestEventStream('/api/conversation/chat', { requiresAuth: false, signal: caller.signal })
    const assertion = expect(request).rejects.toBe(reason)
    caller.abort(reason)
    await assertion
    expect(vi.getTimerCount()).toBe(0)
  })

  it('unwraps a successful JSON envelope through the Axios instance', async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ id: 7 }))
    vi.stubGlobal('fetch', fetchMock)

    await expect(requestJson<{ id: number }>('/api/user/7', {
      requiresAuth: false,
    })).resolves.toEqual({ id: 7 })
    expect(fetchMock).toHaveBeenCalledOnce()
  })

  it('unwraps a successful null JSON result', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(null)))

    await expect(requestJson<null>('/api/conversation/thread-idle', {
      method: 'DELETE',
      requiresAuth: false,
    })).resolves.toBeNull()
  })

  it('preserves file bodies for blob requests', async () => {
    const fileResponse = new Response('file content', {
      headers: { 'Content-Type': 'text/plain' },
    })
    // 使用与测试环境原生 Response 相同的 Blob，避免混用 jsdom 的构造器
    vi.stubGlobal('Blob', (await fileResponse.clone().blob()).constructor)
    vi.stubGlobal('fetch', vi.fn(async () => fileResponse))
    const config: ApiAxiosRequestConfig = {
      url: 'https://objects.example/file',
      responseType: 'blob',
      requiresAuth: false,
    }

    const response = await apiClient.request<Blob>(config)

    expect(response.data).toBeInstanceOf(Blob)
    expect(await response.data.text()).toBe('file content')
  })

  it('preserves backend messages verbatim while the frontend language is English', async () => {
    window.localStorage.setItem(LANGUAGE_STORAGE_KEY, 'en')
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(
      null,
      1_001_004_003,
      '后端原始消息：会话仍在运行',
      409,
    )))

    await expect(requestJson('/api/conversation/thread-1', { requiresAuth: false }))
      .rejects.toMatchObject({ message: '后端原始消息：会话仍在运行' })
  })

  it('adds the stored authorization header in the request interceptor', async () => {
    seedAuthSession()
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const request = input instanceof Request ? input : new Request(input)
      expect(request.headers.get('Authorization')).toBe('Bearer token-123')
      return jsonResponse({ id: 7 })
    })
    vi.stubGlobal('fetch', fetchMock)

    await expect(requestJson('/api/user/7')).resolves.toEqual({ id: 7 })
    expect(fetchMock).toHaveBeenCalledOnce()
  })

  it('does not let a delayed JSON 401 from an old token clear a newer session', async () => {
    seedAuthSession('old-token')
    let resolveOldRequest!: (response: Response) => void
    const fetchMock = vi.fn(() => new Promise<Response>((resolve) => {
      resolveOldRequest = resolve
    }))
    vi.stubGlobal('fetch', fetchMock)

    const oldRequest = requestJson('/api/old-session')
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledOnce())
    seedAuthSession('new-token')
    resolveOldRequest(jsonResponse(null, 1_001_001_000, '旧登录已过期', 401))

    await expect(oldRequest).rejects.toBeInstanceOf(AuthError)
    expect(JSON.parse(window.localStorage.getItem(AUTH_SESSION_STORAGE_KEY) ?? '{}')).toMatchObject({
      token: 'new-token',
    })
  })

  it('does not broadcast a delayed service error from an old token into a newer session', async () => {
    seedAuthSession('old-token')
    const listener = vi.fn()
    const unsubscribe = subscribeApiErrors(listener)
    let resolveOldRequest!: (response: Response) => void
    vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>((resolve) => {
      resolveOldRequest = resolve
    })))

    const oldRequest = requestJson('/api/old-session')
    await vi.waitFor(() => expect(globalThis.fetch).toHaveBeenCalledOnce())
    seedAuthSession('new-token')
    resolveOldRequest(new Response(null, { status: 503 }))

    await expect(oldRequest).rejects.toMatchObject({ status: 503 })
    expect(listener).not.toHaveBeenCalled()

    unsubscribe()
  })

  it('does not let a delayed stream 401 from an old token clear a newer session', async () => {
    seedAuthSession('old-token')
    let resolveOldRequest!: (response: Response) => void
    const fetchMock = vi.fn(() => new Promise<Response>((resolve) => {
      resolveOldRequest = resolve
    }))
    vi.stubGlobal('fetch', fetchMock)

    const oldRequest = requestEventStream('/api/conversation/chat')
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledOnce())
    seedAuthSession('new-token')
    resolveOldRequest(jsonResponse(null, 1_001_001_000, '旧登录已过期', 401))

    await expect(oldRequest).rejects.toBeInstanceOf(AuthError)
    expect(JSON.parse(window.localStorage.getItem(AUTH_SESSION_STORAGE_KEY) ?? '{}')).toMatchObject({
      token: 'new-token',
    })
  })

  it('intercepts a non-success business code for direct Axios callers exactly once', async () => {
    const listener = vi.fn()
    const unsubscribe = subscribeApiErrors(listener)
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(
      null,
      1_001_004_003,
      '会话仍在运行，请先停止并等待运行结束',
      409,
    )))
    const config: ApiAxiosRequestConfig = { requiresAuth: false }

    await expect(apiClient.get('/api/conversation/thread-running', config)).rejects.toMatchObject({
      message: '会话仍在运行，请先停止并等待运行结束',
      code: 1_001_004_003,
    })
    expect(listener).toHaveBeenCalledOnce()
    expect(listener).toHaveBeenCalledWith(expect.objectContaining({
      code: 1_001_004_003,
    }))

    unsubscribe()
  })

  it('rejects a business error sent with HTTP 200 as an invalid success response', async () => {
    const listener = vi.fn()
    const unsubscribe = subscribeApiErrors(listener)
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(
      null,
      1_001_004_003,
      '不应继续信任这条旧协议消息',
    )))

    await expect(requestJson('/api/conversation/thread-running', {
      requiresAuth: false,
    })).rejects.toMatchObject({
      message: '接口返回格式不合法',
      code: 500,
      status: 200,
      isAuthError: false,
    })
    expect(listener).toHaveBeenCalledOnce()

    unsubscribe()
  })

  it('preserves the business error from a non-success HTTP response', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(
      null,
      1_001_004_003,
      '会话仍在运行，请先停止并等待运行结束',
      409,
    )))

    await expect(requestJson('/api/conversation/thread-running', {
      requiresAuth: false,
    })).rejects.toMatchObject({
      message: '会话仍在运行，请先停止并等待运行结束',
      code: 1_001_004_003,
      status: 409,
    })
  })

  it('supports request-scoped suppression without bypassing business-code rejection', async () => {
    const listener = vi.fn()
    const unsubscribe = subscribeApiErrors(listener)
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(
      null,
      1_001_004_001,
      '无效的分页游标',
      422,
    )))

    await expect(requestJson('/api/conversation/history', {
      requiresAuth: false,
      suppressGlobalError: true,
    })).rejects.toBeInstanceOf(ApiError)
    expect(listener).not.toHaveBeenCalled()

    unsubscribe()
  })

  it('treats JSON auth envelopes from stream endpoints as auth failures instead of SSE', async () => {
    seedAuthSession()
    const failureListener = vi.fn()
    const unsubscribe = subscribeAuthFailure(failureListener)

    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(
      null,
      1_001_001_000,
      '登录已过期，请重新登录',
      401,
    )))

    await expect(requestEventStream('/api/conversation/chat', {
      method: 'POST',
      body: { hello: 'world' },
    })).rejects.toBeInstanceOf(AuthError)

    expect(window.localStorage.getItem(AUTH_SESSION_STORAGE_KEY)).toBeNull()
    expect(failureListener).toHaveBeenCalledWith({
      code: 1_001_001_000,
      message: '登录已过期，请重新登录',
    })

    unsubscribe()
  })

  it('does not clear the session from a body code when HTTP reports success', async () => {
    seedAuthSession()
    const failureListener = vi.fn()
    const unsubscribe = subscribeAuthFailure(failureListener)
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(
      null,
      401,
      '不应按响应体判断登录失效',
    )))

    const error = await requestJson('/api/user/7').catch((reason: unknown) => reason)

    expect(error).toBeInstanceOf(ApiError)
    expect(error).not.toBeInstanceOf(AuthError)
    expect(window.localStorage.getItem(AUTH_SESSION_STORAGE_KEY)).not.toBeNull()
    expect(failureListener).not.toHaveBeenCalled()

    unsubscribe()
  })

  it('keeps the authenticated session for forbidden business responses', async () => {
    seedAuthSession()
    const listener = vi.fn()
    const unsubscribe = subscribeApiErrors(listener)
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(
      null,
      1_001_001_001,
      '没有该操作权限',
      403,
    )))

    const error = await requestJson('/api/forbidden').catch((reason: unknown) => reason)

    expect(error).toBeInstanceOf(ApiError)
    expect(error).not.toBeInstanceOf(AuthError)
    expect(window.localStorage.getItem(AUTH_SESSION_STORAGE_KEY)).not.toBeNull()
    expect(listener).toHaveBeenCalledOnce()

    unsubscribe()
  })

  it('does not accept an HTTP 200 JSON error envelope from a stream endpoint', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(
      null,
      1_001_004_002,
      '不应继续兼容旧的聊天错误响应',
    )))

    await expect(requestEventStream('/api/conversation/chat', {
      requiresAuth: false,
    })).rejects.toMatchObject({
      message: '聊天接口返回了非流式成功响应',
      code: 200,
      status: 200,
      isAuthError: false,
    })
  })

  it('uses the HTTP status when a stream returns a contradictory success envelope', async () => {
    seedAuthSession()
    const failureListener = vi.fn()
    const unsubscribe = subscribeAuthFailure(failureListener)
    vi.stubGlobal('fetch', vi.fn(async () => new Response(
      JSON.stringify({ code: 0, message: 'success', data: null }),
      {
        status: 401,
        headers: { 'Content-Type': 'application/json' },
      },
    )))

    await expect(requestEventStream('/api/conversation/chat')).rejects.toMatchObject({
      message: '请先登录',
      code: 401,
      status: 401,
    })
    expect(window.localStorage.getItem(AUTH_SESSION_STORAGE_KEY)).toBeNull()
    expect(failureListener).toHaveBeenCalledWith({ code: 401, message: '请先登录' })

    unsubscribe()
  })

  it('rejects successful HTTP responses that do not use the API envelope', async () => {
    const listener = vi.fn()
    const unsubscribe = subscribeApiErrors(listener)
    vi.stubGlobal('fetch', vi.fn(async () => new Response(
      JSON.stringify({ id: 7 }),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    )))

    await expect(requestJson('/api/user/7', { requiresAuth: false })).rejects.toMatchObject({
      message: '接口返回格式不合法',
      status: 200,
    })
    expect(listener).toHaveBeenCalledOnce()

    unsubscribe()
  })

  it('normalizes network failures and emits the shared error channel', async () => {
    const listener = vi.fn()
    const unsubscribe = subscribeApiErrors(listener)
    vi.stubGlobal('fetch', vi.fn(async () => {
      throw new TypeError('Failed to fetch')
    }))

    await expect(requestJson('/api/user/7', { requiresAuth: false })).rejects.toMatchObject({
      message: '网络请求失败，请稍后重试',
      status: 0,
    })
    expect(listener).toHaveBeenCalledOnce()

    unsubscribe()
  })

  it('normalizes non-JSON gateway failures through the same error channel', async () => {
    const listener = vi.fn()
    const unsubscribe = subscribeApiErrors(listener)
    vi.stubGlobal('fetch', vi.fn(async () => new Response(
      'upstream=private.internal token=must-not-escape',
      { status: 502 },
    )))

    await expect(requestJson('/api/user/7', { requiresAuth: false })).rejects.toMatchObject({
      message: '服务暂不可用，请稍后重试',
      code: 502,
      status: 502,
    })
    expect(listener).toHaveBeenCalledOnce()
    expect(listener.mock.calls[0]?.[0].message).not.toContain('private.internal')

    unsubscribe()
  })

  it('does not expose a non-envelope stream transport body', async () => {
    const listener = vi.fn()
    const unsubscribe = subscribeApiErrors(listener)
    vi.stubGlobal('fetch', vi.fn(async () => new Response(
      '<html>proxy=private.internal</html>',
      { status: 502, headers: { 'Content-Type': 'text/html' } },
    )))

    await expect(requestEventStream('/api/conversation/chat', {
      requiresAuth: false,
    })).rejects.toMatchObject({
      message: '服务暂不可用，请稍后重试',
      code: 502,
      status: 502,
    })
    expect(listener).toHaveBeenCalledOnce()
    expect(listener.mock.calls[0]?.[0].message).not.toContain('private.internal')

    unsubscribe()
  })

  it.each([
    ['text/event-stream'],
    ['text/html'],
  ])('cancels an unread %s error body before rejecting', async (contentType) => {
    const cancel = vi.fn()
    const body = new ReadableStream<Uint8Array>({ cancel })
    vi.stubGlobal('fetch', vi.fn(async () => new Response(body, {
      status: 502,
      headers: { 'Content-Type': contentType },
    })))

    await expect(requestEventStream('/api/conversation/chat', {
      requiresAuth: false,
    })).rejects.toMatchObject({ status: 502 })

    expect(cancel).toHaveBeenCalledOnce()
  })

  it('cancels an unread successful body when the stream content type is unsupported', async () => {
    const cancel = vi.fn()
    const body = new ReadableStream<Uint8Array>({ cancel })
    vi.stubGlobal('fetch', vi.fn(async () => new Response(body, {
      status: 200,
      headers: { 'Content-Type': 'text/html' },
    })))

    await expect(requestEventStream('/api/conversation/chat', {
      requiresAuth: false,
    })).rejects.toMatchObject({
      message: '聊天接口返回了不支持的响应类型',
      status: 200,
    })

    expect(cancel).toHaveBeenCalledOnce()
  })

  it('rejects a successful response without a JSON envelope', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(null, { status: 204 })))

    await expect(requestJson<null>('/api/conversation/thread-idle', {
      method: 'DELETE',
      requiresAuth: false,
    })).rejects.toMatchObject({ status: 204 })
  })

  it.each([
    ['', '/api/user/7', null],
    ['/backend', '/backend/api/user/7', null],
    ['backend', '/backend/api/user/7', null],
    [
      'https://api.example.test/backend',
      '/backend/api/user/7',
      'https://api.example.test',
    ],
  ])(
    'applies the Axios API base exactly once: %s',
    async (apiBase, expectedPath, configuredOrigin) => {
      vi.stubEnv('VITE_API_BASE_URL', apiBase)
      vi.resetModules()
      const { requestJson: requestWithConfiguredBase } = await import('./http')
      let requestedUrl = ''
      vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
        requestedUrl = input instanceof Request ? input.url : String(input)
        return jsonResponse({ id: 7 })
      }))

      await expect(requestWithConfiguredBase('/api/user/7', {
        requiresAuth: false,
      })).resolves.toEqual({ id: 7 })

      const parsedUrl = new URL(requestedUrl)
      expect(parsedUrl.origin).toBe(configuredOrigin ?? window.location.origin)
      expect(parsedUrl.pathname).toBe(expectedPath)
    },
  )
})
