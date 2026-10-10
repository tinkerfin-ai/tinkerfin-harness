import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  AUTH_SESSION_STORAGE_KEY,
  clearAuthSession,
  saveAuthSession,
} from '../../auth/session'
import {
  ApiError,
  AuthError,
  requestEventStream,
  requestJson,
  subscribeApiErrors,
} from './http'

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
    serverAddress: 'http://127.0.0.1:8090', tokenType: 'Bearer',
    expiresAt: '2099-01-01T00:00:00.000Z',
    user: {
      user_id: 7,
      username: 'yunsan',
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
})
