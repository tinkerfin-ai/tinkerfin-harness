import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'

import App from './App'
import { setServerAddress } from './api/shared/config'
import {
  AUTH_SESSION_STORAGE_KEY,
  clearAuthSession,
  saveAuthSession,
} from './auth/session'
import {
  clearActiveRunSession,
  readActiveRunSessions,
  writeActiveRunSession,
} from './features/conversation/stream/activeRunSession'
import { emptyTraceGraph } from './test/traceFixtures'
import * as notifications from './api/notifications'

const user = {
  user_id: 7,
  username: 'yunsan',
  avatar_url: null,
  roles: [],
  disabled: false,
}

function envelope(data: unknown, status = 200, code = 0, message = 'success') {
  return new Response(JSON.stringify({ code, message, data }), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function loginPayload(accessToken = 'fresh-token') {
  return {
    access_token: accessToken,
    token_type: 'Bearer',
    expires_at: '2099-01-01T00:00:00.000Z',
    user,
  }
}

function sessionPayload(expiresAt = '2099-01-01T00:00:00.000Z') {
  return {
    expires_at: expiresAt,
    user,
  }
}

function seedSession(
  token = 'token-123',
  expiresAt = '2099-01-01T00:00:00.000Z',
) {
  saveAuthSession({
    token,
    serverAddress: 'http://127.0.0.1:8090', tokenType: 'Bearer',
    expiresAt,
    user,
  })
}

describe('App authentication boundary', () => {
  beforeAll(async () => {
    // 认证用例验证挂载时机，先完成真实工作区模块加载以隔离测试转译耗时
    await import('./features/workspace/WorkspaceScreen')
  })

  afterEach(() => {
    cleanup()
    clearAuthSession()
    clearActiveRunSession()
    window.history.replaceState(null, '', '/')
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
    vi.useRealTimers()
  })

  it('设备授权在外部账号登录后重新校验，保留原授权意图', async () => {
    const id = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
    window.history.replaceState(null, '', `/?device_authorization=${id}#token=${'c'.repeat(64)}`)
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(input instanceof Request ? input.url : String(input))
      if (url.pathname.endsWith('/api/auth/me')) return envelope(sessionPayload())
      if (url.pathname.endsWith('/preview')) return envelope({ authorization_id: id, name: 'Test Mac', state: 'waiting', expires_at: '2099-01-01T00:00:00Z' })
      throw new Error(`unexpected request: ${url.pathname}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    render(<App />)
    expect(await screen.findByRole('heading', { name: '登录' })).toBeInTheDocument()
    await act(async () => {
      const value = JSON.stringify({ token: 'external-token', tokenType: 'Bearer', serverAddress: 'http://127.0.0.1:8090', expiresAt: '2099-01-01T00:00:00.000Z', user })
      localStorage.setItem(AUTH_SESSION_STORAGE_KEY, value)
      window.dispatchEvent(new StorageEvent('storage', { key: AUTH_SESSION_STORAGE_KEY, newValue: value }))
    })
    expect(await screen.findByRole('heading', { name: '允许连接这台 Mac？' })).toBeInTheDocument()
    expect(await screen.findByText('Test Mac')).toBeInTheDocument()
    expect(window.location.search).toContain(id)
  })

  it('shows the login screen without mounting the workspace when no session exists', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    window.history.replaceState(null, '', '/?thread=private-thread')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '欢迎回来' })).toBeInTheDocument()
    expect(screen.queryByLabelText('对话内容')).not.toBeInTheDocument()
    expect(window.location.search).toBe('')
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it.each(['authorized', 'conflict'] as const)('授权入口为 %s 时保留对应的成功或恢复页面', async outcome => {
    const id = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
    seedSession()
    window.history.replaceState(null, '', `/?device_authorization=${id}#token=${'c'.repeat(64)}`)
    vi.spyOn(notifications, 'startNotificationFeed').mockReturnValue(() => {})
    const open = vi.spyOn(window, 'open').mockReturnValue(null)
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const path = new URL(input instanceof Request ? input.url : String(input)).pathname
      if (path === '/api/auth/me') return envelope(sessionPayload())
      if (path.endsWith('/preview')) return outcome === 'conflict'
        ? envelope(null, 409, 1_001_012_002, '另一个账号的信息不能展示')
        : envelope({ authorization_id: id, name: 'Current Mac', state: 'authorized', expires_at: '2099-01-01T00:00:00Z' })
      if (path === '/api/projects') return envelope([])
      if (path === '/api/models') return envelope({ items: [], defaultModelId: null })
      if (path === '/api/devices') return envelope([{ device_id: 'current', name: 'Current Mac', online: true, revoked: false,
        paired_at: '2030-01-01T00:00:00Z', last_seen_at: null, capabilities: { browser: false, desktop: false, mcp: false } }])
      if (path === '/api/automation/runs/active') return envelope({ items: [], nextCursor: null })
      throw new Error(`unexpected request: ${path}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    render(<App />)
    if (outcome === 'authorized') {
      expect(await screen.findByText('授权成功，你可以关闭此页面')).toHaveAttribute('role', 'status')
      expect(screen.queryByRole('dialog', { name: '设置' })).not.toBeInTheDocument()
      expect(screen.queryByText('Current Mac')).not.toBeInTheDocument()
      expect(screen.queryByText(user.username)).not.toBeInTheDocument()
      expect(screen.queryAllByRole('button')).toHaveLength(0)
      expect(window.location.search).toBe(`?device_authorization=${id}`)
      expect(window.location.hash).toBe(`#token=${'c'.repeat(64)}`)
      expect(open).not.toHaveBeenCalled()
      expect(fetchMock.mock.calls.every(([input]) => {
        const path = new URL(input instanceof Request ? input.url : String(input)).pathname
        return path === '/api/auth/me' || path.endsWith('/preview')
      })).toBe(true)
      return
    }
    expect(await screen.findByRole('heading', { name: '本地设备' })).toBeInTheDocument()
    expect(screen.getByRole('dialog', { name: '设置' })).toBeInTheDocument()
    expect(screen.queryByText('另一个账号的信息不能展示')).not.toBeInTheDocument()
    expect(window.location.search).toBe('?settings=devices')
    expect(window.location.hash).toBe('')
    expect(open).toHaveBeenCalledOnce()
    expect(open).toHaveBeenCalledWith('tinkerfin://connect', '_self')
    await userEvent.setup().click(screen.getByRole('button', { name: '打开客户端' }))
    expect(open).toHaveBeenLastCalledWith('tinkerfin://connect', '_self')
    expect(fetchMock.mock.calls.some(([input]) => new URL(input instanceof Request ? input.url : String(input)).pathname === '/api/devices/authorizations')).toBe(false)
  })

  it('未登录的无效入口退出授权后保留安全设置目的地', async () => {
    const id = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
    window.history.replaceState(null, '', `/?device_authorization=${id}#token=${'c'.repeat(64)}`)
    const open = vi.spyOn(window, 'open').mockReturnValue(null)
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const path = new URL(input instanceof Request ? input.url : String(input)).pathname
      if (path.endsWith('/deny')) return envelope(null, 404, 1_001_012_000)
      throw new Error(`unexpected request: ${path}`)
    }))
    render(<App />)
    await userEvent.setup().click(await screen.findByRole('button', { name: '取消' }))
    expect(await screen.findByRole('heading', { name: '欢迎回来' })).toBeInTheDocument()
    expect(window.location.search).toBe('?settings=devices')
    expect(window.location.hash).toBe('')
    expect(open).toHaveBeenCalledWith('tinkerfin://connect', '_self')
  })

  it('mounts the workspace only after the stored session is verified', async () => {
    seedSession()
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(input instanceof Request ? input.url : String(input), 'http://localhost')
      if (url.pathname.endsWith('/api/auth/me')) return envelope(sessionPayload())
      if (url.pathname === '/api/projects') return envelope([{ id: 'project-1', name: '测试项目', createdAt: '2030-01-01', updatedAt: '2030-01-01' }])
      if (url.pathname.endsWith('/api/conversation/config')) return envelope({ dayRanges: [7, 30] })
      if (url.pathname.endsWith('/api/conversation/history')) {
        return envelope({ items: [], nextCursor: null })
      }
      throw new Error(`unexpected request: ${url.pathname}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<App />)

    await waitFor(() => expect(screen.getByLabelText('对话内容')).toBeInTheDocument())
    expect(screen.queryByRole('heading', { name: '欢迎回来' })).not.toBeInTheDocument()
  })

  it('keeps the session blocked and retries when /me is temporarily unavailable', async () => {
    vi.useFakeTimers()
    seedSession('retry-token')
    let sessionRequests = 0
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(input instanceof Request ? input.url : String(input), 'http://localhost')
      if (url.pathname.endsWith('/api/auth/me')) {
        sessionRequests += 1
        return sessionRequests === 1
          ? new Response(null, { status: 503 })
          : envelope(sessionPayload())
      }
      if (url.pathname === '/api/projects') return envelope([{ id: 'project-1', name: '测试项目', createdAt: '2030-01-01', updatedAt: '2030-01-01' }])
      if (url.pathname.endsWith('/api/conversation/config')) return envelope({ dayRanges: [7, 30] })
      if (url.pathname.endsWith('/api/conversation/history')) {
        return envelope({ items: [], nextCursor: null })
      }
      throw new Error(`unexpected request: ${url.pathname}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    await act(async () => { render(<App />) })

    expect(screen.getByLabelText('正在重新验证登录状态')).toBeInTheDocument()
    expect(window.localStorage.getItem(AUTH_SESSION_STORAGE_KEY)).not.toBeNull()
    expect(screen.queryByLabelText('对话内容')).not.toBeInTheDocument()
    expect(screen.queryByRole('list', { name: '系统提示' })).not.toBeInTheDocument()
    expect(sessionRequests).toBe(1)
    await act(async () => { await vi.advanceTimersByTimeAsync(1_000) })
    expect(screen.getByLabelText('对话内容')).toBeInTheDocument()
    expect(sessionRequests).toBe(2)
  })

  it('silently unmounts an active workspace when an authenticated request returns 401', async () => {
    seedSession()
    window.history.replaceState(null, '', '/?thread=private-thread')
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(input instanceof Request ? input.url : String(input), 'http://localhost')
      if (url.pathname.endsWith('/api/auth/me')) return envelope(sessionPayload())
      if (url.pathname === '/api/projects') return envelope([{ id: 'project-1', name: '测试项目', createdAt: '2030-01-01', updatedAt: '2030-01-01' }])
      if (url.pathname.endsWith('/api/conversation/config')) return envelope({ dayRanges: [7, 30] })
      if (url.pathname.endsWith('/api/conversation/history')) {
        return envelope(null, 401, 1_001_001_000, '登录已过期')
      }
      throw new Error(`unexpected request: ${url.pathname}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<App />)

    expect(await screen.findByRole('heading', { name: '欢迎回来' })).toBeInTheDocument()
    expect(screen.queryByLabelText('对话内容')).not.toBeInTheDocument()
    expect(screen.queryByRole('list', { name: '系统提示' })).not.toBeInTheDocument()
    expect(window.location.search).toBe('')
    expect(window.localStorage.getItem(AUTH_SESSION_STORAGE_KEY)).toBeNull()
  })

  it('卸载登录页面后取消请求且不保存晚到的登录结果', async () => {
    const browserUser = userEvent.setup()
    let respond!: (value: Response) => void
    let signal: AbortSignal | undefined
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
      const request = input instanceof Request ? input : new Request(input)
      signal = request.signal
      return new Promise<Response>(resolve => { respond = resolve })
    }))
    const view = render(<App />)
    await browserUser.type(await screen.findByLabelText('用户名'), 'yunsan')
    await browserUser.type(screen.getByLabelText('密码'), 'password')
    await browserUser.click(screen.getByRole('button', {name: '登录'}))
    await waitFor(() => expect(signal).toBeDefined())
    view.unmount()
    expect(signal?.aborted).toBe(true)
    await act(async () => { respond(envelope(loginPayload())) })
    expect(window.localStorage.getItem(AUTH_SESSION_STORAGE_KEY)).toBeNull()
  })

  it('cancels login when the server changes and ignores its late response', async () => {
    const browserUser = userEvent.setup()
    let respond!: (value: Response) => void
    let signal: AbortSignal | undefined
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
      signal = (input instanceof Request ? input : new Request(input)).signal
      return new Promise<Response>(resolve => { respond = resolve })
    }))
    render(<App />)
    await browserUser.type(await screen.findByLabelText('用户名'), 'yunsan')
    await browserUser.type(screen.getByLabelText('密码'), 'password')
    await browserUser.click(screen.getByRole('button', { name: '登录' }))
    await waitFor(() => expect(signal).toBeDefined())
    act(() => { setServerAddress('https://second.example') })
    expect(signal?.aborted).toBe(true)
    await act(async () => { respond(envelope(loginPayload())) })
    expect(window.localStorage.getItem(AUTH_SESSION_STORAGE_KEY)).toBeNull()
    expect(screen.getByLabelText('服务器地址')).toHaveValue('https://second.example')
    expect(screen.getByRole('button', { name: '登录' })).toBeEnabled()
  })

  it('rejects login atomically when the browser cannot persist the session', async () => {
    const browserUser = userEvent.setup()
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const request = input instanceof Request ? input : new Request(input)
      if (new URL(request.url).pathname.endsWith('/api/auth/login')) {
        return envelope(loginPayload())
      }
      throw new Error(`unexpected request: ${request.url}`)
    }))
    render(<App />)
    vi.spyOn(window.localStorage, 'setItem').mockImplementation(() => {
      throw new DOMException('storage disabled', 'SecurityError')
    })

    await browserUser.type(screen.getByLabelText('用户名'), 'yunsan')
    await browserUser.type(screen.getByLabelText('密码'), 'password')
    await browserUser.click(screen.getByRole('button', { name: '登录' }))

    expect(await screen.findByRole('status')).toHaveTextContent(
      '浏览器无法保存登录状态，请检查隐私或存储设置后重试',
    )
    expect(screen.getAllByRole('status')).toHaveLength(1)
    expect(screen.queryByLabelText('正在检查登录状态')).not.toBeInTheDocument()
    expect(window.localStorage.getItem(AUTH_SESSION_STORAGE_KEY)).toBeNull()
  })

  it('calls the real logout endpoint, clears the workspace, and removes the thread route', async () => {
    seedSession()
    window.history.replaceState(null, '', '/?thread=private-thread')
    const browserUser = userEvent.setup()
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const request = input instanceof Request ? input : new Request(input)
      const url = new URL(request.url)
      if (url.pathname.endsWith('/api/auth/me')) return envelope(sessionPayload())
      if (url.pathname.endsWith('/api/auth/logout')) return envelope(null)
      if (url.pathname === '/api/projects') return envelope([{ id: 'project-1', name: '测试项目', createdAt: '2030-01-01', updatedAt: '2030-01-01' }])
      if (url.pathname.endsWith('/api/conversation/config')) return envelope({ dayRanges: [7, 30] })
      if (url.pathname.endsWith('/api/conversation/history')) return envelope({ items: [], nextCursor: null })
      if (url.pathname.endsWith('/api/conversation/private-thread/history')) {
        return envelope({projectId: 'project-1', archived: false,
          id: 1,
          threadId: 'private-thread',
          title: '私密会话',
          lastModel: 'main',
          asOfSeq: 1,
          headRunId: 'private-run',
          runFailures: [],
          availableHeads: ['private-run'],
          historyCursor: null,
          messageCount: 0,
          toolCallCount: 0,
          pinned: false,
          messages: [],
          reasoning: [],
          graph: emptyTraceGraph(1),
          state: { root: {}, subgraphs: {} },
          interactions: [],
          status: { execution: 'succeeded', headRunId: 'private-run' },
          completeness: {
            missingPrefix: false,
            missingTail: false,
            payloadOmitted: false,
          },
          createdAt: '2026-08-10T00:00:00.000Z',
          updatedAt: '2026-08-10T00:00:00.000Z',
        })
      }
      throw new Error(`unexpected request: ${url.pathname}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<App />)
    await waitFor(() => expect(screen.getByLabelText('对话内容')).toBeInTheDocument())
    writeActiveRunSession({projectId: 'project-1',
      threadId: 'other-private-thread',
      payload: {
        threadId: 'other-private-thread',
        runId: 'private-run',
        state: {},
        messages: [{ id: 'request-private-run', role: 'user', content: 'private prompt' }],
        tools: [],
        context: [],
        forwardedProps: {projectId: 'project-1',  skillIds: [], accessMode: 'write_approval', model: 'main', command: { plan: 'off' } },
      },
      mode: 'start',
      lastSeq: 7,
    })
    expect((readActiveRunSessions()[0] ?? null)).not.toBeNull()
    await browserUser.click(screen.getByRole('button', { name: '打开用户菜单' }))
    await browserUser.click(screen.getByRole('menuitem', { name: '退出登录' }))

    expect(await screen.findByRole('heading', { name: '欢迎回来' })).toBeInTheDocument()
    expect(screen.queryByLabelText('对话内容')).not.toBeInTheDocument()
    expect(window.location.search).toBe('')
    expect((readActiveRunSessions()[0] ?? null)).toBeNull()
    expect(fetchMock.mock.calls.some(([input]) => {
      const request = input instanceof Request ? input : new Request(input)
      return new URL(request.url).pathname.endsWith('/api/auth/logout') && request.method === 'POST'
    })).toBe(true)
  })
})
