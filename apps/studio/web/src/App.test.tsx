import { toolReviewInterrupts } from './test/aguiFixtures'
import { StrictMode, useCallback, useEffect, useState } from 'react'
import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type {
  ConversationHistoryDetail,
  ConversationHistoryListItem,
} from './api/conversation/history'
import type { ChatRequestPayload, ConversationAgUiEvent } from './api/conversation/types'
import type { AgentModelCatalog } from './api/models/types'
import { subscribeApiErrors } from './api/shared/http'
import { clearAuthSession, saveAuthSession } from './auth/session'
import { ToastViewport } from './components/ui/ToastViewport'
import type { ToastItem, ToastKind } from './components/ui/ToastViewport'
import { WorkspaceScreen } from './features/workspace/WorkspaceScreen'
import { readActiveRunSessions, writeActiveRunSession } from './features/conversation/stream/activeRunSession'
import {
  emptyTraceGraph,
  traceGraphNode,
  traceGraphWithNodes,
} from './test/traceFixtures'

const TEST_USER = {
  user_id: 7,
  username: 'yunsan',
  display_name: '云杉',
  avatar_url: null,
  roles: [],
  disabled: false,
}

function App() {
  const [toasts, setToasts] = useState<ToastItem[]>([])
  const onToast = useCallback((kind: ToastKind, message: string) => {
    setToasts((current) => [...current, {
      id: 'toast-' + (current.length + 1),
      kind,
      message,
    }])
  }, [])
  useEffect(() => subscribeApiErrors((error) => onToast('error', error.message)), [onToast])
  return (
    <>
      <WorkspaceScreen user={TEST_USER} onLogout={vi.fn()} onToast={onToast} />
      <ToastViewport
        toasts={toasts}
        onDismiss={(id) => setToasts((current) => current.filter((item) => item.id !== id))}
      />
    </>
  )
}

const THREAD_ID = 'thread-app'
const RUN_ID = 'run-app'
const BASE_TIME = '2026-08-28T00:00:00.000Z'

const MODEL_CATALOG: AgentModelCatalog = {
  items: [{
    modelId: 'main',
    displayName: 'Main Model',
    reasoningEnabled: false, imageSupport: 'unknown',
    isDefault: true,
  }],
  defaultModelId: 'main',
}

const historyItem = (
  overrides: Partial<ConversationHistoryListItem> = {},
): ConversationHistoryListItem => ({
  titleSource: 'default',
  titleGenerationStatus: 'idle',
  titleSeq: 0,
  id: 1,
  threadId: THREAD_ID,
  title: 'Trace 会话',
  status: 'idle',
  lastRunId: RUN_ID,
  lastModel: 'main',
  messageCount: 1,
  toolCallCount: 0,
  hasPendingInterrupt: false,
  pendingInteractionKind: null,
  pinned: false,
  createdAt: BASE_TIME,
  updatedAt: BASE_TIME,
  ...overrides,
})

const traceDetail = (
  overrides: Partial<ConversationHistoryDetail> = {},
): ConversationHistoryDetail => ({
  titleSource: 'default',
  titleGenerationStatus: 'idle',
  titleSeq: 0,
  id: 1,
  threadId: THREAD_ID,
  title: 'Trace 会话',
  lastModel: 'main',
  pinned: false,
  asOfSeq: 5,
  generation: 'generation-test',
  observedAt: '2026-09-05T00:00:00.000000Z',
  headRunId: RUN_ID,
  runFailures: [],
  availableHeads: [RUN_ID],
  historyCursor: null,
  messageCount: 1,
  toolCallCount: 0,
  messages: [{
    agui: null,
    id: 'message-history',
    traceSeq: 1,
    sourceId: 'assistant-history',
    graphNamespace: [],
    runId: RUN_ID,
    role: 'assistant',
    content: '来自 Trace 的历史回复',
    contentOmitted: false,
    status: 'completed',
    createdAt: BASE_TIME,
    completedAt: BASE_TIME,
  }],
  reasoning: [],
  state: { root: {}, subgraphs: {} },
  interactions: [],
  status: { execution: 'succeeded', headRunId: RUN_ID },
  completeness: { missingPrefix: false, missingTail: false, payloadOmitted: false },
  createdAt: BASE_TIME,
  updatedAt: BASE_TIME,
  ...overrides,
  graph: overrides.graph ?? emptyTraceGraph(overrides.asOfSeq ?? 5),
  taskTrace: overrides.taskTrace ?? { status: 'ready', todoGroups: [] },
})

const jsonResponse = (data: unknown) => new Response(
  JSON.stringify({ code: 0, message: 'success', data }),
  { headers: { 'Content-Type': 'application/json' } },
)

const sseResponse = (events: ConversationAgUiEvent[], startSeq = 1) => {
  const encoder = new TextEncoder()
  return new Response(new ReadableStream<Uint8Array>({
    start(controller) {
      events.forEach((event, index) => {
        controller.enqueue(encoder.encode(
          'id: ' + (startSeq + index) + '\ndata: ' + JSON.stringify(event) + '\n\n',
        ))
      })
      controller.close()
    },
  }), { headers: { 'Content-Type': 'text/event-stream' } })
}

const jsonSseResponse = (event: unknown) => {
  const encoder = new TextEncoder()
  return new Response(new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(encoder.encode(`data: ${JSON.stringify(event)}\n\n`))
      controller.close()
    },
  }), { headers: { 'Content-Type': 'text/event-stream' } })
}

function installFetch(options: {
  list?: ConversationHistoryListItem[]
  details?: Record<string, ConversationHistoryDetail>
  stream?: ConversationAgUiEvent[]
  streamStartSeq?: number
  onChat?: (payload: ChatRequestPayload) => void
} = {}) {
  const details = options.details ?? {}
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = input instanceof Request
      ? input
      : new Request(new URL(String(input), window.location.origin), init)
    const url = new URL(request.url)
    if (url.pathname.endsWith('/api/models')) return jsonResponse(MODEL_CATALOG)
    if (url.pathname.endsWith('/api/conversation/config')) {
      return jsonResponse({ dayRanges: [7, 30] })
    }
    if (url.pathname.endsWith('/api/conversation/history')) {
      return jsonResponse({ items: options.list ?? [], nextCursor: null })
    }
    const history = url.pathname.match(/\/api\/conversation\/([^/]+)\/history$/)
    if (history) {
      const threadId = decodeURIComponent(history[1] ?? '')
      const detail = details[threadId]
      if (!detail) throw new Error('missing Trace detail for ' + threadId)
      return jsonResponse({ ...detail, taskTrace: url.searchParams.get('includeTaskTrace') === 'false' ? null : detail.taskTrace })
    }
    const traceFollow = url.pathname.match(
      /\/api\/conversation\/([^/]+)\/trace\/graph(\/follow)?$/,
    )
    if (traceFollow) {
      const threadId = decodeURIComponent(traceFollow[1] ?? '')
      const detail = details[threadId]
      if (!detail) throw new Error('missing Trace detail for ' + threadId)
      const snapshot = { ...detail.graph, nextCursor: null }
      return traceFollow[2]
        ? jsonSseResponse({ type: 'snapshot', snapshot })
        : jsonResponse(snapshot)
    }
    if (request.method === 'POST' && url.pathname.endsWith('/api/conversation/chat')) {
      const payload = await request.clone().json() as ChatRequestPayload
      options.onChat?.(payload)
      return sseResponse((options.stream ?? []).map((event) => (
        'runId' in event && typeof event.runId === 'string'
          ? { ...event, runId: payload.runId }
          : event
      )), options.streamStartSeq)
    }
    throw new Error('unexpected fetch ' + request.method + ' ' + url.pathname)
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

describe('Studio Trace history integration', () => {
  beforeEach(() => {
    saveAuthSession({
      token: 'app-token',
      tokenType: 'Bearer',
      expiresAt: '2099-01-01T00:00:00.000Z',
      user: TEST_USER,
    })
    window.history.replaceState({}, '', '/')
  })

  afterEach(() => {
    clearAuthSession()
    window.localStorage.clear()
    window.sessionStorage.clear()
    vi.unstubAllGlobals()
  })

  it('新会话首页不显示会话视图切换', async () => {
    installFetch({ list: [], details: {} })
    render(<App />)

    expect(await screen.findByRole('heading', { name: '暂无消息' })).toBeInTheDocument()
    expect(screen.queryByRole('tablist', { name: '会话视图' })).not.toBeInTheDocument()
  })

  it.each([false, true])('提交时清空输入，迟到的开始事件保留新草稿（已有会话：%s）', async (existing) => {
    const user = userEvent.setup()
    const fetch = installFetch({
      list: existing ? [historyItem()] : [],
      details: { [THREAD_ID]: traceDetail() },
    })
    const defaultFetch = fetch.getMockImplementation()!
    let events: ReadableStreamDefaultController<Uint8Array> | undefined
    let submitted: ChatRequestPayload | undefined
    fetch.mockImplementation(async (input, init) => {
      if (String(input).endsWith('/api/conversation/chat')) {
        submitted = JSON.parse(String(init?.body)) as ChatRequestPayload
        return new Response(new ReadableStream<Uint8Array>({
          start(controller) { events = controller },
        }), { headers: { 'Content-Type': 'text/event-stream' } })
      }
      return defaultFetch(input, init)
    })
    const { unmount } = render(<App />)
    try {
      if (existing) await screen.findByText('来自 Trace 的历史回复')
      const input = await screen.findByRole('textbox', { name: '消息输入' })
      await waitFor(() => expect(input).toBeEnabled())
      await user.type(input, '你好{Enter}')
      expect(within(screen.getByRole('region', { name: '对话内容' })).getByText('你好')).toBeVisible()
      expect(input).toHaveValue('')
      await waitFor(() => expect(submitted).toBeDefined())
      await user.type(input, '下一条草稿')
      await act(async () => {
        const event = { type: 'RUN_STARTED', threadId: THREAD_ID, runId: submitted!.runId }
        events!.enqueue(new TextEncoder().encode(`data: ${JSON.stringify(event)}\n\n`))
      })
      expect(input).toHaveValue('下一条草稿')
    } finally {
      unmount()
    }
  })

  it('会话受理后可独立提交新会话，旧流结束不会重复提交仍待受理的草稿', async () => {
    const user = userEvent.setup()
    const details: Record<string, ConversationHistoryDetail> = {}
    const requests: { payload: ChatRequestPayload; events: ReadableStreamDefaultController<Uint8Array> }[] = []
    const fetch = installFetch({ details })
    const defaultFetch = fetch.getMockImplementation()!
    fetch.mockImplementation(async (input, init) => {
      if (String(input).endsWith('/api/conversation/chat')) {
        const payload = JSON.parse(String(init?.body)) as ChatRequestPayload
        return new Response(new ReadableStream<Uint8Array>({
          start(events) { requests.push({ payload, events }) },
        }), { headers: { 'Content-Type': 'text/event-stream' } })
      }
      return defaultFetch(input, init)
    })
    const view = render(<App />)
    try {
      const input = await screen.findByRole('textbox', { name: '消息输入' })
      await waitFor(() => expect(input).toBeEnabled())
      await user.type(input, '会话A{Enter}')
      await waitFor(() => expect(requests).toHaveLength(1))
      const first = requests[0]!
      details['thread-a'] = traceDetail({
        threadId: 'thread-a', headRunId: first.payload.runId,
        availableHeads: [first.payload.runId],
        status: { execution: 'succeeded', headRunId: first.payload.runId },
      })
      await act(async () => {
        first.events.enqueue(new TextEncoder().encode('id: 1\ndata: ' + JSON.stringify({
          type: 'RUN_STARTED', threadId: 'thread-a', runId: first.payload.runId,
        }) + '\n\n'))
      })
      await waitFor(() => expect(new URL(window.location.href).searchParams.get('thread')).toBe('thread-a'))
      await user.click(screen.getByRole('button', { name: '新会话' }))
      await user.type(input, '会话B{Enter}')
      await waitFor(() => expect(requests).toHaveLength(2))
      const second = requests[1]!
      expect(second.payload.runId).not.toBe(first.payload.runId)
      await act(async () => {
        first.events.enqueue(new TextEncoder().encode('id: 2\ndata: ' + JSON.stringify({
          type: 'RUN_FINISHED', threadId: 'thread-a', runId: first.payload.runId,
          outcome: { type: 'success' },
        }) + '\n\n'))
        first.events.close()
      })
      await waitFor(() => expect(readActiveRunSessions().map(item => item.payload.runId)).toEqual([second.payload.runId]))
      await user.type(input, '下一条草稿{Enter}{Enter}')
      expect(requests).toHaveLength(2)
      expect(input).toHaveValue('下一条草稿')
      await act(async () => {
        second.events.enqueue(new TextEncoder().encode('id: 1\ndata: ' + JSON.stringify({
          type: 'RUN_STARTED', threadId: 'thread-b', runId: second.payload.runId,
        }) + '\n\n'))
      })
      await waitFor(() => expect(new URL(window.location.href).searchParams.get('thread')).toBe('thread-b'))
      expect(requests).toHaveLength(2)
      expect(input).toHaveValue('下一条草稿')
    } finally {
      view.unmount()
    }
  })

  it('首帧前浏览器历史导航释放草稿选择权', async () => {
    const user = userEvent.setup()
    const fetch = installFetch({ list: [historyItem()], details: { [THREAD_ID]: traceDetail() } })
    const defaultFetch = fetch.getMockImplementation()!
    let events: ReadableStreamDefaultController<Uint8Array> | undefined
    let submitted: ChatRequestPayload | undefined
    fetch.mockImplementation(async (input, init) => {
      if (String(input).endsWith('/api/conversation/chat')) {
        submitted = JSON.parse(String(init?.body)) as ChatRequestPayload
        return new Response(new ReadableStream<Uint8Array>({ start(controller) { events = controller } }), { headers: { 'Content-Type': 'text/event-stream' } })
      }
      return defaultFetch(input, init)
    })
    const { unmount } = render(<App />)
    try {
      await screen.findByText('来自 Trace 的历史回复')
      await user.click(screen.getByRole('button', { name: /新会话/ }))
      const input = screen.getByRole('textbox', { name: '消息输入' })
      await user.type(input, '新草稿{Enter}')
      await waitFor(() => expect(submitted).toBeDefined())
      act(() => {
        window.history.pushState({}, '', '/?thread=' + THREAD_ID)
        window.dispatchEvent(new PopStateEvent('popstate'))
      })
      await screen.findByText('来自 Trace 的历史回复')
      await act(async () => {
        events!.enqueue(new TextEncoder().encode('id: 1\ndata: ' + JSON.stringify({ type: 'RUN_STARTED', threadId: 'late-draft', runId: submitted!.runId }) + '\n\n'))
      })
      expect(new URL(window.location.href).searchParams.get('thread')).toBe(THREAD_ID)
      expect(screen.getByText('来自 Trace 的历史回复')).toBeVisible()
    } finally {
      unmount()
    }
  })

  it('hydrates a selected historical conversation directly from Trace', async () => {
    installFetch({
      list: [historyItem()],
      details: { [THREAD_ID]: traceDetail() },
    })

    render(<App />)

    expect(await screen.findByText('来自 Trace 的历史回复')).toBeInTheDocument()
    expect(screen.getByText('Trace 会话')).toBeInTheDocument()
  })

  it.each([
    [false, false], [true, false], [false, true], [true, true],
  ])('服务端拒绝未受理提交时恢复文字但不覆盖新草稿（已有：%s，已编辑：%s）', async (existing, edited) => {
    const user = userEvent.setup()
    const fetch = installFetch({
      list: existing ? [historyItem()] : [], details: { [THREAD_ID]: traceDetail() },
    })
    const defaultFetch = fetch.getMockImplementation()!
    let rejectRequest: ((response: Response) => void) | undefined
    fetch.mockImplementation(async (input, init) => {
      if (String(input).endsWith('/api/conversation/chat')) {
        return new Promise<Response>((resolve) => { rejectRequest = resolve })
      }
      return defaultFetch(input, init)
    })
    render(<App />)
    if (existing) await screen.findByText('来自 Trace 的历史回复')
    const input = await screen.findByRole('textbox', { name: '消息输入' })
    await waitFor(() => expect(input).toBeEnabled())
    await user.type(input, '保留这次提交')
    await user.click(screen.getByRole('button', { name: '发送消息' }))
    expect(input).toHaveValue('')
    await waitFor(() => expect(rejectRequest).toBeDefined())
    if (edited) await user.type(input, '下一条草稿')
    await act(async () => {
      rejectRequest!(new Response(JSON.stringify({ code: 422, message: '请求参数无效', data: null }), {
        status: 422, headers: { 'Content-Type': 'application/json' },
      }))
    })
    await waitFor(() => expect(input).toHaveValue(edited ? '下一条草稿' : '保留这次提交'))
    expect((readActiveRunSessions()[0] ?? null)).toBeNull()
    expect(screen.getAllByRole('alert')).toHaveLength(1)
  })

  it.each([false, true])('offers explicit recovery for an unconfirmed submission without automatically resending (existing: %s)', async (existing) => {
    const user = userEvent.setup()
    const details = { [THREAD_ID]: traceDetail() }
    const submitted: ChatRequestPayload[] = []
    installFetch({
      list: existing ? [historyItem()] : [], details,
      streamStartSeq: existing ? 76 : 1,
      stream: [
        { type: 'RUN_STARTED', threadId: THREAD_ID, runId: RUN_ID },
        { type: 'RUN_FINISHED', threadId: THREAD_ID, runId: RUN_ID, outcome: { type: 'success' } },
      ],
      onChat: (payload) => {
        submitted.push(payload)
        if (submitted.length === 1) throw new TypeError('connection lost before headers')
        details[THREAD_ID] = traceDetail({
          asOfSeq: 6, observedAt: '2026-09-05T00:00:01.000000Z',
          headRunId: payload.runId, availableHeads: [payload.runId],
          status: { execution: 'succeeded', headRunId: payload.runId },
        })
      },
    })
    render(<App />)
    const input = await screen.findByRole('textbox', { name: '消息输入' })
    await waitFor(() => expect(input).toBeEnabled())
    await user.type(input, '请求连接恢复验证')
    await user.click(screen.getByRole('button', { name: '发送消息' }))
    const reconnect = await screen.findByRole('button', { name: '恢复连接' })
    expect(input).toHaveValue('')
    expect(screen.getByText('连接已中断，尚无法确认任务状态，请恢复连接')).toBeInTheDocument()
    expect((readActiveRunSessions()[0] ?? null)?.payload.runId).toBe(submitted[0]?.runId)
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 300)) })
    expect(submitted).toHaveLength(1)
    await user.click(reconnect)
    await waitFor(() => expect(submitted).toHaveLength(2))
    expect(submitted[1]).toEqual(submitted[0])
    await waitFor(() => expect(screen.queryByRole('button', { name: '恢复连接' })).not.toBeInTheDocument())
  })

  it('notifies again when an explicit recovery fails after the previous toast was dismissed', async () => {
    const user = userEvent.setup()
    let attempts = 0
    installFetch({ onChat: () => { attempts += 1; throw new TypeError('offline') } })
    render(<App />)
    const input = await screen.findByRole('textbox', { name: '消息输入' })
    await waitFor(() => expect(input).toBeEnabled())
    await user.type(input, '恢复同一提交')
    await user.click(screen.getByRole('button', { name: '发送消息' }))
    const message = '连接已中断，尚无法确认任务状态，请恢复连接'
    await user.click(await screen.findByRole('button', { name: `关闭提示：${message}` }))
    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: '恢复连接' }))
    expect(await screen.findByRole('alert', {}, { timeout: 4000 })).toHaveTextContent(message)
    expect(attempts).toBe(5)
  })

  it('初始化失败在会话中持久展示且不弹 Toast', async () => {
    const user = userEvent.setup()
    const details = { [THREAD_ID]: traceDetail() }
    installFetch({
      details,
      stream: [
        { type: 'RUN_STARTED', threadId: THREAD_ID, runId: RUN_ID },
        { type: 'RUN_ERROR', code: 'runtime_initialization_error', message: 'Agent run failed' },
      ],
      onChat: (payload) => {
        details[THREAD_ID] = traceDetail({
          headRunId: payload.runId, availableHeads: [payload.runId],
          status: { execution: 'failed', headRunId: payload.runId },
          messages: [{ ...traceDetail().messages[0]!, id: 'accepted-input', role: 'user', runId: payload.runId, content: '初始化验证' }],
          runFailures: [{ runId: payload.runId, errorCode: 'runtime_initialization_error', failedAt: BASE_TIME, retryable: true }],
        })
      },
    })
    render(<App />)
    const input = await screen.findByRole('textbox', { name: '消息输入' })
    await waitFor(() => expect(input).toBeEnabled())
    await user.type(input, '初始化验证')
    await user.click(screen.getByRole('button', { name: '发送消息' }))
    expect(await screen.findByText('会话异常')).toBeInTheDocument()
    await waitFor(() => expect((readActiveRunSessions()[0] ?? null)).toBeNull())
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 100)) })
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.queryByText('Agent run failed')).not.toBeInTheDocument()
    expect(screen.queryByText('任务遇到问题')).not.toBeInTheDocument()
    expect(screen.queryByText('对话运行失败')).not.toBeInTheDocument()
  })

  it('重试较早问题会追加新一轮，保留草稿、原失败及附件且只提交一次', async () => {
    const user = userEvent.setup()
    const attachment = { id: 'original-document', name: '说明.txt', mime_type: 'text/plain', size_bytes: 12 }
    const original = traceDetail().messages[0]!
    const source = traceDetail({
      messages: [
        { ...original, id: 'failed-question', role: 'user', runId: 'old-failed', content: [{ type: 'text', text: '原问题' }, { type: 'document', source: { type: 'url', value: 'attachment:original-document', mimeType: 'text/plain' }, metadata: attachment }] },
        { ...original, id: 'later-question', role: 'user', content: '后来的问题' },
        { ...original, id: 'later-answer', content: '后来的回答' },
      ],
      runFailures: [{ runId: 'old-failed', errorCode: 'runtime_initialization_error', failedAt: BASE_TIME, retryable: true }],
    })
    const details = { [THREAD_ID]: source }
    let submitted: ChatRequestPayload | undefined
    let submissions = 0
    const fetchMock = installFetch({ list: [historyItem()], details })
    const originalFetch = fetchMock.getMockImplementation()!
    fetchMock.mockImplementation(async (input, init) => {
      if (String(input).endsWith('/api/conversation/chat')) {
        submitted = JSON.parse(String(init?.body)) as ChatRequestPayload
        submissions += 1
        return new Response(new ReadableStream<Uint8Array>(), { headers: { 'Content-Type': 'text/event-stream' } })
      }
      return originalFetch(input, init)
    })
    window.history.replaceState({}, '', '/?thread=' + THREAD_ID)
    const view = render(<App />)
    try {
      const retry = await screen.findByRole('button', { name: '重试' })
      const input = screen.getByRole('textbox', { name: '消息输入' })
      await user.type(input, '还未发送的草稿')
      await user.dblClick(retry)
      await waitFor(() => expect(submissions).toBe(1))
      expect(input).toHaveValue('还未发送的草稿')
      expect(submitted?.runId).not.toBe('old-failed')
      expect(submitted?.parentRunId).toBeUndefined()
      expect(submitted?.messages[0]?.content).toEqual(source.messages[0]?.content)
      expect(submitted?.forwardedProps.model).toBe('main')
      expect(screen.getAllByText('原问题')).toHaveLength(2)
      expect(screen.getByText('后来的回答')).toBeInTheDocument()
      expect(retry).toBeDisabled()
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    } finally {
      view.unmount()
    }
  })

  it('三个历史失败会话切换及重新打开时不弹错误提示', async () => {
    const user = userEvent.setup()
    const list = [1, 2, 3].map(id => historyItem({ id, threadId: `failed-${id}`, title: `失败会话${id}`, status: 'error' }))
    const details = Object.fromEntries(list.map(item => [item.threadId, traceDetail({
      threadId: item.threadId, title: item.title,
      messages: [{ ...traceDetail().messages[0]!, role: 'user', content: '你好', id: `question-${item.id}` }],
      status: { execution: 'failed', headRunId: RUN_ID },
      runFailures: [{ runId: RUN_ID, errorCode: 'runtime_initialization_error', failedAt: BASE_TIME, retryable: true }],
    })]))
    installFetch({ list, details })
    const view = render(<App />)
    for (const item of list) {
      await user.click(await screen.findByRole('button', { name: `打开会话：${item.title}` }))
      expect(await screen.findByText('会话异常')).toBeInTheDocument()
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    }
    view.unmount()
    render(<App />)
    expect(await screen.findByText('会话异常')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('刷新后已受理的运行只跟随历史，断连和主动恢复都不重新提交', async () => {
    const user = userEvent.setup()
    const payload: ChatRequestPayload = {
      threadId: THREAD_ID, runId: RUN_ID, state: {}, messages: [], tools: [], context: [],
      forwardedProps: { model: 'main', command: { plan: 'off' } },
    }
    writeActiveRunSession({ threadId: THREAD_ID, payload, mode: 'start', lastSeq: 0 })
    let submitted = 0
    const fetchMock = installFetch({
      list: [historyItem({ status: 'running' })],
      details: { [THREAD_ID]: traceDetail({ status: { execution: 'running', headRunId: RUN_ID } }) },
      onChat: () => { submitted += 1; throw new TypeError('offline') },
    })
    const originalFetch = fetchMock.getMockImplementation()!
    let followed = 0
    fetchMock.mockImplementation(async (input, init) => {
      const path = new URL(input instanceof Request ? input.url : String(input), window.location.origin).pathname
      if (path === `/api/conversation/${THREAD_ID}/trace`) {
        followed += 1
        throw new TypeError('offline')
      }
      return originalFetch(input, init)
    })
    const view = render(<App />)
    const reconnect = await screen.findByRole('button', { name: '恢复连接' })
    expect(followed).toBe(1)
    expect(submitted).toBe(0)
    expect(readActiveRunSessions()).toEqual([])
    view.rerender(<App />)
    expect(followed).toBe(1)
    await user.click(reconnect)
    await waitFor(() => expect(followed).toBe(2))
    expect(submitted).toBe(0)
  })

  it('returns to chat when selecting another conversation from the Trace view', async () => {
    const user = userEvent.setup()
    const secondThreadId = 'thread-second'
    installFetch({
      list: [
        historyItem(),
        historyItem({
          id: 2,
          threadId: secondThreadId,
          title: '第二个会话',
          lastRunId: 'run-second',
        }),
      ],
      details: {
        [THREAD_ID]: traceDetail(),
        [secondThreadId]: traceDetail({
          id: 2,
          threadId: secondThreadId,
          title: '第二个会话',
          headRunId: 'run-second',
          runFailures: [],
          availableHeads: ['run-second'],
          messages: [{
            ...traceDetail().messages[0]!,
            id: 'message-second',
            runId: 'run-second',
            content: '第二个会话的聊天内容',
          }],
        }),
      },
    })

    render(<App />)

    await user.click(await screen.findByRole('tab', { name: '链路' }))
    expect(await screen.findByRole('tabpanel', { name: '链路' })).toBeVisible()
    await user.click(screen.getByRole('button', { name: '打开会话：第二个会话' }))

    expect(await screen.findByText('第二个会话的聊天内容')).toBeVisible()
    expect(screen.getByRole('tab', { name: '对话' })).toHaveAttribute('aria-selected', 'true')
    await user.click(screen.getByRole('button', { name: /新会话/ }))
    expect(screen.queryByRole('tablist', { name: '会话视图' })).not.toBeInTheDocument()
  })

  it.each(['succeeded', 'failed', 'cancelled', 'abandoned', 'waiting', 'unknown'] as const)(
    '运行转为 %s 后链路关闭跟随并读取最终快照',
    async (execution) => {
      const user = userEvent.setup()
      const initial = traceDetail({ status: { execution: 'running', headRunId: RUN_ID } })
      const details = { [THREAD_ID]: initial }
      const fetch = installFetch({ list: [historyItem({ status: 'running' })], details })
      const defaultFetch = fetch.getMockImplementation()!
      let traceController: ReadableStreamDefaultController<Uint8Array> | undefined
      const graphClosed = vi.fn()
      const requests: Request[] = []
      fetch.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
        const request = input instanceof Request
          ? input : new Request(new URL(String(input), window.location.origin), init)
        requests.push(request)
        const path = new URL(request.url).pathname
        if (path === `/api/conversation/${THREAD_ID}/trace`) {
          return new Response(new ReadableStream<Uint8Array>({
            start(controller) {
              traceController = controller
              controller.enqueue(new TextEncoder().encode(
                `event: trace\ndata: ${JSON.stringify({ type: 'snapshot', snapshot: initial })}\n\n`,
              ))
            },
          }), { headers: { 'Content-Type': 'text/event-stream' } })
        }
        if (path === `/api/conversation/${THREAD_ID}/trace/graph/follow`) {
          return new Response(new ReadableStream<Uint8Array>({
            start(controller) {
              controller.enqueue(new TextEncoder().encode(`event: trace\ndata: ${JSON.stringify({
                type: 'snapshot', snapshot: { ...initial.graph, nextCursor: null },
              })}\n\n`))
            },
            cancel: graphClosed,
          }), { headers: { 'Content-Type': 'text/event-stream' } })
        }
        return defaultFetch(input, init)
      })
      const { unmount } = render(<App />)
      await screen.findByText('来自 Trace 的历史回复')
      await user.click(screen.getByRole('tab', { name: '链路' }))
      const graphRequests = () => requests.filter((request) => (
        new URL(request.url).pathname.endsWith('/trace/graph/follow')
      ))
      await waitFor(() => expect(graphRequests()).toHaveLength(1))
      const finalGraph = traceGraphWithNodes([traceGraphNode({
        id: 'final-answer', kind: 'assistant_message', name: 'AssistantMessage',
        content: '最终链路内容', startedSeq: 6, updatedSeq: 7,
      })], 7)
      details[THREAD_ID] = traceDetail({
        asOfSeq: 7,
        observedAt: '2026-09-05T00:00:02.000000Z',
        graph: finalGraph,
        status: { execution, headRunId: RUN_ID },
        completeness: { missingPrefix: false, missingTail: execution === 'unknown', payloadOmitted: false },
      })
      await act(async () => traceController!.enqueue(new TextEncoder().encode(
        `event: trace\ndata: ${JSON.stringify({
          type: 'update', runFailures: [],
          update: {
            asOfSeq: 6, generation: initial.generation, observedAt: '2026-09-05T00:00:01.000000Z',
            events: [], facts: [], messages: { upserts: [], removes: [] },
            reasoning: { upserts: [], removes: [] }, interactions: { upserts: [], removes: [] },
            graph: {
              asOfSeq: 6, nextCursor: null, turnUpserts: [], turnRemoves: [], nodeUpserts: [],
              nodeRemoves: [], orderedNodeIds: [], matchedNodeIds: [], completeness: initial.graph.completeness,
            },
            state: initial.state, status: { execution, headRunId: RUN_ID },
            completeness: details[THREAD_ID].completeness,
            messageCount: initial.messageCount, toolCallCount: 0, projections: {}, runFailures: [],
          },
          taskTrace: null,
        })}\n\n`,
      )))
      await waitFor(() => expect(graphClosed).toHaveBeenCalledTimes(1))
      expect(graphRequests()[0]!.signal.aborted).toBe(true)
      expect(await screen.findAllByText('最终链路内容')).not.toHaveLength(0)
      expect(graphRequests()).toHaveLength(1)
      expect(requests.some((request) => new URL(request.url).pathname.endsWith('/trace/graph'))).toBe(true)
      unmount()
    },
  )

  it('静态链路重新激活只刷新一次会话，并发现其他标签页启动的新 Run', async () => {
    const user = userEvent.setup()
    const details = { [THREAD_ID]: traceDetail() }
    const fetch = installFetch({ list: [historyItem()], details })
    const defaultFetch = fetch.getMockImplementation()!
    const requests: Request[] = []
    fetch.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = input instanceof Request
        ? input : new Request(new URL(String(input), window.location.origin), init)
      requests.push(request)
      const path = new URL(request.url).pathname
      if (path.endsWith('/trace') || path.endsWith('/trace/graph/follow')) {
        const snapshot = path.endsWith('/trace')
          ? details[THREAD_ID]
          : { ...details[THREAD_ID].graph, nextCursor: null }
        return new Response(new ReadableStream<Uint8Array>({
          start(controller) {
            controller.enqueue(new TextEncoder().encode(
              `event: trace\ndata: ${JSON.stringify({ type: 'snapshot', snapshot })}\n\n`,
            ))
          },
        }), { headers: { 'Content-Type': 'text/event-stream' } })
      }
      return defaultFetch(input, init)
    })
    const { unmount } = render(<App />)
    await screen.findByText('来自 Trace 的历史回复')
    await user.click(screen.getByRole('tab', { name: '链路' }))
    await waitFor(() => expect(requests.some((request) => new URL(request.url).pathname.endsWith('/trace/graph'))).toBe(true))
    const historyCount = () => requests.filter((request) => (
      new URL(request.url).pathname === `/api/conversation/${THREAD_ID}/history`
    )).length
    const before = historyCount()
    details[THREAD_ID] = traceDetail({
      headRunId: 'run-other-tab', availableHeads: [RUN_ID, 'run-other-tab'],
      observedAt: '2026-09-05T00:00:01.000000Z',
      status: { execution: 'running', headRunId: 'run-other-tab' },
    })
    const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden')
    act(() => {
      window.dispatchEvent(new Event('blur'))
      document.dispatchEvent(new Event('visibilitychange'))
    })
    visibility.mockReturnValue('visible')
    act(() => document.dispatchEvent(new Event('visibilitychange')))
    await waitFor(() => expect(historyCount()).toBe(before + 1))
    act(() => window.dispatchEvent(new Event('focus')))
    await waitFor(() => expect(requests.some((request) => new URL(request.url).pathname.endsWith('/trace/graph/follow'))).toBe(true))
    expect(historyCount()).toBe(before + 1)
    unmount()
    visibility.mockRestore()
  })

  it('restores an approval from public Trace interaction references', async () => {
    const waiting = traceDetail({
      status: { execution: 'waiting', headRunId: RUN_ID },
      interactions: [{
      agui: toolReviewInterrupts('native-review', [{ toolCallId: 'call-write', args: { file_path: '/root-hitl.txt', content: 'ROOT_HITL' } }]),
        id: 'interaction-1',
        traceSeq: 4,
        sourceId: 'native-review',
        graphNamespace: [],
        runId: RUN_ID,
        kind: 'tool_approval',
        toolCallIds: ['call-write'],
        status: 'pending',
        payloadOmitted: false,
        payload: {
          action_requests: [{
            name: 'write_file',
            arguments: {
              disposition: 'inline',
              safeSizeBytes: 55,
              value: { file_path: '/root-hitl.txt', content: 'ROOT_HITL' },
            },
          }],
          review_configs: [{
            action_name: 'write_file',
            allowed_decisions: ['approve', 'reject'],
          }],
        },
        openedAt: BASE_TIME,
        resolvedAt: null,
      }],
      graph: traceGraphWithNodes([traceGraphNode({
        id: 'tool-node',
        startedSeq: 3,
        name: 'write_file',
        runId: RUN_ID,
        sourceId: 'call-write',
        request: { file_path: '/root-hitl.txt', content: 'ROOT_HITL' },
        resultOmitted: true,
        status: 'waiting',
        startedAt: BASE_TIME,
        completedAt: null,
      })], 5),
    })
    installFetch({
      list: [historyItem({
        status: 'waiting_approval',
        hasPendingInterrupt: true,
        pendingInteractionKind: 'tool_approval',
      })],
      details: { [THREAD_ID]: waiting },
    })

    render(<App />)

    expect(await screen.findByRole('region', { name: '等待审批' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '允许' })).toBeEnabled()
    expect(screen.getByRole('button', { name: '拒绝' })).toBeEnabled()
    expect(screen.getAllByText('/root-hitl.txt')).toHaveLength(2)
  })

  it('keeps a pending child Tool inside its SubAgent instead of the main timeline', async () => {
    const childNamespace = ['tools:child-task']
    const waiting = traceDetail({
      status: { execution: 'waiting', headRunId: RUN_ID },
      interactions: [{
      agui: toolReviewInterrupts('child-review', [{ toolCallId: 'call-child-write', args: { file_path: '/ui-subagent-hitl.txt', content: 'UI_SUBAGENT_HITL' } }]),
        id: 'interaction-child',
        traceSeq: 6,
        sourceId: 'child-review',
        graphNamespace: childNamespace,
        runId: RUN_ID,
        kind: 'tool_approval',
        toolCallIds: ['call-child-write'],
        status: 'pending',
        payloadOmitted: false,
        payload: {
          action_requests: [{
            name: 'write_file',
            arguments: {
              disposition: 'inline',
              safeSizeBytes: 68,
              value: {
                file_path: '/ui-subagent-hitl.txt',
                content: 'UI_SUBAGENT_HITL',
              },
            },
          }],
          review_configs: [{
            action_name: 'write_file',
            allowed_decisions: ['approve', 'reject'],
          }],
        },
        openedAt: BASE_TIME,
        resolvedAt: null,
      }],
      graph: traceGraphWithNodes([
        traceGraphNode({
          id: 'subagent-node',
          agui: { kind: 'subagent', parentToolCallId: 'call-task', subagentInvocationId: 'public-child' },
          startedSeq: 3,
          parentSubagentId: null,
          kind: 'subagent',
          name: 'general-purpose',
          runId: RUN_ID,
          graphNamespace: childNamespace,
          sourceId: 'call-task',
          request: {
            description: '直接调用 write_file',
            subagent_type: 'general-purpose',
          },
          status: 'waiting',
          startedAt: BASE_TIME,
          completedAt: null,
        }),
        traceGraphNode({
          id: 'child-tool',
          agui: { kind: 'tool', toolCallId: 'call-child-write' },
          startedSeq: 4,
          parentSubagentId: 'subagent-node',
          name: 'write_file',
          runId: RUN_ID,
          graphNamespace: childNamespace,
          sourceId: 'call-child-write',
          request: {
            file_path: '/ui-subagent-hitl.txt',
            content: 'UI_SUBAGENT_HITL',
          },
          status: 'waiting',
          startedAt: BASE_TIME,
          completedAt: null,
        }),
      ], 5),
    })
    installFetch({
      list: [historyItem({
        status: 'waiting_approval',
        hasPendingInterrupt: true,
        pendingInteractionKind: 'tool_approval',
      })],
      details: { [THREAD_ID]: waiting },
    })

    render(<App />)

    expect(await screen.findByRole('region', { name: '等待审批' })).toBeInTheDocument()
    expect(screen.getByText('SubAgent')).toBeInTheDocument()
    expect(screen.getAllByText('/ui-subagent-hitl.txt')).toHaveLength(2)
  })

  it('submits one resume request after the final decision in a multi-action approval', async () => {
    const user = userEvent.setup()
    const waiting = traceDetail({
      status: { execution: 'waiting', headRunId: RUN_ID },
      interactions: [{
      agui: toolReviewInterrupts('native-review-multi', [{ toolCallId: 'call-a', args: { file_path: '/a.txt', content: 'A' }, description: '写入 A' }, { toolCallId: 'call-b', args: { file_path: '/b.txt', content: 'B' }, description: '写入 B' }]),
        id: 'interaction-multi',
        traceSeq: 4,
        sourceId: 'native-review-multi',
        graphNamespace: [],
        runId: RUN_ID,
        kind: 'tool_approval',
        toolCallIds: ['call-a', 'call-b'],
        status: 'pending',
        payloadOmitted: false,
        payload: {
          action_requests: [
            {
              name: 'write_file',
              description: '写入 A',
              arguments: {
                disposition: 'inline',
                safeSizeBytes: 24,
                value: { '': { file_path: '/a.txt', content: 'A' } },
              },
            },
            {
              name: 'write_file',
              description: '写入 B',
              arguments: {
                disposition: 'inline',
                safeSizeBytes: 24,
                value: { '': { file_path: '/b.txt', content: 'B' } },
              },
            },
          ],
          review_configs: [
            { action_name: 'write_file', allowed_decisions: ['approve', 'reject'] },
            { action_name: 'write_file', allowed_decisions: ['approve', 'reject'] },
          ],
        },
        openedAt: BASE_TIME,
        resolvedAt: null,
      }],
      graph: traceGraphWithNodes([
        traceGraphNode({
          id: 'tool-a',
          startedSeq: 2,
          name: 'write_file',
          runId: RUN_ID,
          sourceId: 'call-a',
          request: { file_path: '/a.txt', content: 'A' },
          status: 'waiting',
          startedAt: BASE_TIME,
          completedAt: null,
        }),
        traceGraphNode({
          id: 'tool-b',
          startedSeq: 3,
          name: 'write_file',
          runId: RUN_ID,
          sourceId: 'call-b',
          request: { file_path: '/b.txt', content: 'B' },
          status: 'waiting',
          startedAt: BASE_TIME,
          completedAt: null,
        }),
      ], 5),
    })
    const details: Record<string, ConversationHistoryDetail> = { [THREAD_ID]: waiting }
    const chatPayloads: ChatRequestPayload[] = []
    installFetch({
      list: [historyItem({
        status: 'waiting_approval',
        hasPendingInterrupt: true,
        pendingInteractionKind: 'tool_approval',
      })],
      details,
      stream: [
        { type: 'RUN_STARTED', threadId: THREAD_ID, runId: 'server-run' },
        {
          type: 'RUN_FINISHED',
          threadId: THREAD_ID,
          runId: 'server-run',
          outcome: { type: 'success' },
        },
      ],
      streamStartSeq: 76,
      onChat: (payload) => {
        chatPayloads.push(payload)
        details[THREAD_ID] = traceDetail({
          headRunId: payload.runId,
          runFailures: [],
          availableHeads: [payload.runId],
          status: { execution: 'succeeded', headRunId: payload.runId },
          interactions: [],
          graph: emptyTraceGraph(5),
        })
      },
    })
    render(
      <StrictMode>
        <App />
      </StrictMode>,
    )

    await user.click(await screen.findByRole('button', { name: '允许' }))
    await waitFor(() => expect(screen.getAllByText('/b.txt').length).toBeGreaterThan(0))
    await user.click(screen.getByRole('button', { name: '允许' }))
    await waitFor(() => expect(chatPayloads).toHaveLength(1))
    await act(async () => {
      await new Promise((resolve) => window.setTimeout(resolve, 100))
    })

    expect(chatPayloads).toHaveLength(1)
    expect(chatPayloads[0]?.resume).toHaveLength(2)
  })
})
