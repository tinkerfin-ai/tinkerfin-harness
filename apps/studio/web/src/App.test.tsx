import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { StrictMode, useCallback, useEffect, useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { toolReviewInterrupts } from './test/aguiFixtures'

import type {
  ConversationHistoryDetail,
  ConversationHistoryListItem,
} from './api/conversation/history'
import type { ChatRequestPayload, ConversationAgUiEvent } from './api/conversation/types'
import type { AgentModelCatalog } from './api/models/types'
import { subscribeApiErrors } from './api/shared/http'
import { clearAuthSession, saveAuthSession } from './auth/session'
import type { ToastItem, ToastKind } from './components/ui/ToastViewport'
import { ToastViewport } from './components/ui/ToastViewport'
import { readActiveRunSessions } from './features/conversation/stream/activeRunSession'
import type { InstalledSkill } from './features/skills/model'
import { WorkspaceScreen } from './features/workspace/WorkspaceScreen'
import {
  emptyTraceGraph,
  traceGraphNode,
  traceGraphWithNodes,
} from './test/traceFixtures'

const TEST_USER = {
  user_id: 7,
  username: 'yunsan',
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
    connectionId: 'test-provider', connectionDisplayName: '测试提供方', reasoningEnabled: false,
    isDefault: true,
  }],
  defaultModelId: 'main',
}

const historyItem = (
  overrides: Partial<ConversationHistoryListItem> = {},
): ConversationHistoryListItem => ({projectId: 'project-1', archived: false,  accessMode: 'write_approval',
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
): ConversationHistoryDetail => ({projectId: 'project-1', archived: false,  accessMode: 'write_approval',
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
  submissionResult: null, planResults: [], interactionAvailability: [], interactions: [],
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
  historyReady?: Promise<void>
  onChat?: (payload: ChatRequestPayload) => void
} = {}) {
  const details = options.details ?? {}
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = input instanceof Request
      ? input
      : new Request(new URL(String(input), window.location.origin), init)
    const url = new URL(request.url)
    if (url.pathname === '/api/projects') return jsonResponse([{ id: 'project-1', name: '测试项目', createdAt: BASE_TIME, updatedAt: BASE_TIME }])
    if (url.pathname.endsWith('/api/models')) return jsonResponse(MODEL_CATALOG)
    if (url.pathname.endsWith('/api/skills/installations') || url.pathname.endsWith('/api/skills/selection')) return jsonResponse([])
    if (url.pathname.endsWith('/api/conversation/config')) {
      return jsonResponse({ dayRanges: [7, 30] })
    }
    if (url.pathname.endsWith('/api/conversation/history')) {
      return jsonResponse({ items: options.list ?? [], nextCursor: null })
    }
    const history = url.pathname.match(/\/api\/conversation\/([^/]+)\/history$/)
    if (history) {
      await options.historyReady
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
      const snapshot = { ...detail.graph, nextCursor: null, generation: detail.generation, headRunId: detail.headRunId }
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
    // 业务集成验证使用减少动态效果，提示关闭不依赖真实动画帧
    const matchMedia = window.matchMedia
    vi.stubGlobal('matchMedia', (query: string) => ({
      ...matchMedia(query),
      matches: query.includes('prefers-reduced-motion')
        ? query === '(prefers-reduced-motion: reduce)'
        : matchMedia(query).matches,
    }))
    saveAuthSession({
      token: 'app-token',
      serverAddress: 'http://127.0.0.1:8090', tokenType: 'Bearer',
      expiresAt: '2099-01-01T00:00:00.000Z',
      user: TEST_USER,
    })
    window.history.replaceState({}, '', '/')
  })

  afterEach(() => {
    cleanup()
    clearAuthSession()
    window.localStorage.clear()
    window.sessionStorage.clear()
    vi.unstubAllGlobals()
    vi.useRealTimers()
  })

  it('停用已选技能后返回对话，权威列表核验期间不发包并保留草稿', async () => {
    let installed: InstalledSkill = {project_id: null, overridden: false,  id: 'reports', name: 'reports', description: '整理报告', source_kind: 'zip',
      source_id: null, source_name: 'ZIP', external_id: null, enabled: true, author: null, topics: [],
      file_count: 1, byte_size: 3, created_at: BASE_TIME, updated_at: BASE_TIME }
    const onChat = vi.fn()
    const fetch = installFetch({ list: [historyItem()], details: { [THREAD_ID]: traceDetail() }, onChat })
    const defaultFetch = fetch.getMockImplementation()!
    let heldRead: Promise<Response> | undefined
    fetch.mockImplementation(async (input, init) => {
      const request = input instanceof Request ? input : new Request(new URL(String(input), window.location.origin), init)
      const path = new URL(request.url).pathname
      if (path === '/api/skills/sources') return jsonResponse([])
      if (path === '/api/skills/installations') return heldRead ?? jsonResponse([installed])
      if (path === '/api/skills/installations/reports' && request.method === 'PATCH') {
        installed = { ...installed, enabled: false }
        return jsonResponse(installed)
      }
      return defaultFetch(input, init)
    })
    window.history.replaceState({}, '', '/?project=project-1&thread=' + THREAD_ID)
    render(<App />)
    await screen.findByText('来自 Trace 的历史回复')
    fireEvent.change(screen.getByRole('textbox', { name: '消息输入' }), { target: { value: '保留正文' } })
    fireEvent.click(screen.getByRole('button', { name: '打开命令和技能' }))
    fireEvent.click(await screen.findByRole('option', { name: /reports 整理报告/ }))
    fireEvent.click(screen.getByRole('button', { name: '技能库' }))
    fireEvent.click(await screen.findByRole('tab', { name: '我的' }))
    fireEvent.click(await screen.findByRole('switch', { name: '启用技能：reports' }))
    await waitFor(() => expect(screen.getByRole('switch')).toHaveAttribute('aria-checked', 'false'))

    let rejectRead: (reason: Error) => void = () => undefined
    heldRead = new Promise((_, reject) => { rejectRead = reject })
    act(() => {
      window.history.replaceState({}, '', '/?project=project-1&thread=' + THREAD_ID)
      window.dispatchEvent(new PopStateEvent('popstate'))
    })
    const input = screen.getByRole('textbox', { name: '消息输入' })
    expect(input).toHaveValue('保留正文/reports')
    expect(screen.getByText('正在加载技能')).toBeVisible()
    expect(screen.getByRole('button', { name: '发送消息' })).toBeDisabled()
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onChat).not.toHaveBeenCalled()

    await act(async () => rejectRead(new Error('offline')))
    expect(await screen.findByText('技能列表暂不可用')).toBeVisible()
    expect(screen.getByRole('button', { name: '发送消息' })).toBeDisabled()
    heldRead = undefined
    fireEvent.click(screen.getByRole('button', { name: '打开命令和技能' }))
    fireEvent.click(screen.getByRole('option', { name: /重试加载技能/ }))
    expect(await screen.findByText('所选技能已停用或卸载，请移除后再发送')).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: '打开命令和技能' }))
    fireEvent.keyDown(input, { key: 'Backspace' })
    expect(input).toHaveValue('保留正文')
    fireEvent.click(screen.getByRole('button', { name: '发送消息' }))
    await waitFor(() => expect(onChat).toHaveBeenCalledOnce())
    expect(onChat.mock.calls[0][0]).toMatchObject({ messages: [{ content: '保留正文' }], forwardedProps: { skillIds: [] } })
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
    expect(screen.getAllByRole('status')).toHaveLength(1)
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
    expect(screen.getByRole('status')).toHaveTextContent('连接已中断，尚无法确认任务状态，请恢复连接')
    expect((readActiveRunSessions()[0] ?? null)?.payload.runId).toBe(submitted[0]?.runId)
    expect(submitted).toHaveLength(1)
    await user.click(reconnect)
    await waitFor(() => expect(submitted).toHaveLength(2))
    expect(submitted[1]).toEqual(submitted[0])
    await waitFor(() => expect(screen.queryByRole('button', { name: '恢复连接' })).not.toBeInTheDocument())
  })

  it('submits one resume request after the final decision in a multi-action approval', async () => {
    const user = userEvent.setup()
    let releaseHistory!: () => void
    const historyReady = new Promise<void>(resolve => { releaseHistory = resolve })
    const waiting = traceDetail({
      submissionResult: null, planResults: [], interactionAvailability: ['native-review-multi#0', 'native-review-multi#1']
        .map(interruptId => ({ interruptId, state: 'available', submissionRunId: null })),
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
      historyReady,
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
          asOfSeq: 6,
          runFailures: [],
          availableHeads: [payload.runId],
          status: { execution: 'succeeded', headRunId: payload.runId },
          interactions: [],
          submissionResult: null, planResults: [], interactionAvailability: ['native-review-multi#0', 'native-review-multi#1']
            .map(interruptId => ({ interruptId, state: 'resolved', submissionRunId: payload.runId })),
          graph: emptyTraceGraph(6),
        })
      },
    })
    await act(async () => {
      render(<StrictMode><App /></StrictMode>)
    })
    expect(screen.queryByRole('button', { name: '允许' })).not.toBeInTheDocument()
    await act(async () => { releaseHistory() })

    await user.click(screen.getByRole('button', { name: '允许' }))
    await waitFor(() => expect(screen.getAllByText('/b.txt').length).toBeGreaterThan(0))
    await user.click(screen.getByRole('button', { name: '允许' }))
    await waitFor(() => expect(chatPayloads).toHaveLength(1))
    await waitFor(() => expect(screen.getByRole('textbox', { name: '消息输入' })).toBeEnabled())
    expect(screen.queryByRole('region', { name: '等待审批' })).not.toBeInTheDocument()

    expect(chatPayloads).toHaveLength(1)
    expect(chatPayloads[0]?.resume).toHaveLength(2)
  })
})
