import { act, cleanup, renderHook } from '@testing-library/react'
import { useLayoutEffect, useRef, useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ConversationHistoryDetail } from '../../api/conversation/history'
import { ApiError } from '../../api/shared/http'
import { upsertConversation } from '../../lib/workspace'
import { mockResourceNotices } from '../../test/resourceNotices'
import { emptyTraceGraph } from '../../test/traceFixtures'
import type { Conversation } from '../../types'
import { useChainTrace } from '../conversation/chainTrace/useChainTrace'
import { restoreConversationFromTrace } from '../conversation/trace/runtime'
import {
  historyItemFromDetail,
  useWorkspaceHistory,
} from './useWorkspaceHistory'
import { useWorkspaceState } from './useWorkspaceState'

const historyMocks = vi.hoisted(() => ({
  detail: vi.fn(),
  groupConfig: vi.fn(),
  list: vi.fn(),
  graph: vi.fn(),
  followGraph: vi.fn(),
  title: vi.fn(),
}))

vi.mock('../../api/conversation/titles', () => ({ fetchConversationTitle: historyMocks.title }))

vi.mock(import('../../api/conversation/traceGraph'), async (importOriginal) => ({
  ...await importOriginal(),
  queryTraceGraph: historyMocks.graph,
  followTraceGraph: historyMocks.followGraph,
}))

vi.mock(import('../../api/conversation/history'), async (importOriginal) => ({
  ...await importOriginal(),
  fetchConversationHistoryDetail: historyMocks.detail,
  fetchConversationHistoryGroupConfig: historyMocks.groupConfig,
  fetchConversationHistoryList: historyMocks.list,
}))

const THREAD_ID = 'thread-history-race'
const RUN_ID = 'run-history-race'
const BASE_TIME = '2026-08-28T00:00:00.000Z'



const detail = (
  overrides: Partial<ConversationHistoryDetail> = {},
): ConversationHistoryDetail => ({projectId: 'project-1', archived: false,  accessMode: 'write_approval',
  titleSource: 'default',
  titleGenerationStatus: 'idle',
  titleSeq: 0,
  id: 1,
  threadId: THREAD_ID,
  title: '分页竞态',
  lastModel: 'main',
  pinned: false,
  asOfSeq: 5,
  generation: 'generation-test',
  observedAt: '2026-09-05T00:00:00.000000Z',
  headRunId: RUN_ID,
  runFailures: [],
  availableHeads: [RUN_ID],
  historyCursor: 'cursor-1',
  messageCount: 1,
  toolCallCount: 0,
  messages: [{
    agui: null,
    id: 'message-1',
    traceSeq: 1,
    sourceId: 'assistant-1',
    graphNamespace: [],
    runId: RUN_ID,
    role: 'assistant',
    content: '初始内容',
    contentOmitted: false,
    status: 'completed',
    createdAt: BASE_TIME,
    completedAt: BASE_TIME,
  }],
  reasoning: [],
  state: { root: {}, subgraphs: {} },
  submissionResult: null, planResults: [], interactionAvailability: [], interactions: [],
  status: { execution: 'running', headRunId: RUN_ID },
  completeness: { missingPrefix: false, missingTail: false, payloadOmitted: false },
  createdAt: BASE_TIME,
  updatedAt: BASE_TIME,
  ...overrides,
  graph: overrides.graph ?? emptyTraceGraph(overrides.asOfSeq ?? 5),
  taskTrace: overrides.taskTrace ?? { status: 'ready', todoGroups: [] },
})

function deferred<T>() {
  let resolve: (value: T) => void = () => undefined
  let reject: (reason?: unknown) => void = () => undefined
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, reject, resolve }
}

function queueHistoryResponse() {
  const requested = deferred<AbortSignal>()
  const response = deferred<ConversationHistoryDetail>()
  historyMocks.detail.mockImplementationOnce((_threadId: string, { signal }: { signal: AbortSignal }) => {
    requested.resolve(signal)
    return response.promise
  })
  return { requested, response }
}

function useHarness(
  initial: ConversationHistoryDetail,
  options: {
    catalogReady?: boolean
    initiallyHydrated?: boolean
    onToast?: (kind: 'error', message: string) => void
    prepareTaskTraceOwner?: (threadId: string) => Promise<void>
    traceActive?: boolean
  } = {},
) {
  const [searchOpen, setSearchOpen] = useState(false)
  const [searchScope, setSearchScope] = useState<'project' | 'all'>('project')
  const followDetachedConversation = useRef(vi.fn()).current
  const defaultPrepareTaskTraceOwner = useRef(vi.fn(async () => undefined)).current
  const defaultOnToast = useRef(vi.fn()).current
  const { workspace, setWorkspace, retainConversationDetails } = useWorkspaceState()
  const initialWorkspace = useRef({
    conversations: [{
      ...restoreConversationFromTrace(initial, { model: 'main', includeTaskTrace: true }),
      isHydrated: options.initiallyHydrated ?? true,
    }],
    currentThreadId: initial.threadId,
  }).current
  useLayoutEffect(() => { setWorkspace(initialWorkspace) }, [initialWorkspace, setWorkspace])
  const history = useWorkspaceHistory({projectId: 'project-1', archived: false, searchScope, searchOpen,
    workspace,
    setWorkspace,
    retainConversationDetails,
    defaultModelId: 'main',
    modelCatalogStatus: options.catalogReady ? 'ready' : 'loading',
    refreshOnActivation: options.traceActive,
    followDetachedConversation,
    prepareTaskTraceOwner: options.prepareTaskTraceOwner ?? defaultPrepareTaskTraceOwner,
    onToast: options.onToast ?? defaultOnToast,
  })
  const selected = workspace.conversations.find((item) => item.threadId === workspace.currentThreadId)
  const graph = useChainTrace({
    threadId: workspace.currentThreadId,
    active: options.traceActive ?? false,
    live: selected?.runStatus === 'streaming' || selected?.runStatus === 'detached',
    liveRunId: selected?.activeRunId,
    observation: selected?.trace,
    historyRefresh: history.historyRefresh,
    onRecheckHistory: history.recheckConversationHistory,
    filter: {},
    limit: 1000,
  })
  const advanceTrace = (next: ConversationHistoryDetail) => {
    setWorkspace((state) => upsertConversation(
      state,
      { ...restoreConversationFromTrace(next, { model: 'main', includeTaskTrace: true }), isHydrated: true },
    ))
  }
  const startOwnedRun = (taskTrace?: Conversation['taskTrace']) => {
    setWorkspace((state) => ({
      ...state,
      conversations: state.conversations.map((conversation) => (
        conversation.threadId === initial.threadId
          ? {
              ...conversation,
              runStatus: 'streaming',
              isHydrated: true,
              activeRunId: 'run-owned-new',
              messages: [{
                id: 'message-owned-new',
                role: 'user',
                content: '本次新输入',
                createdAt: BASE_TIME,
              }],
              taskTrace: taskTrace ?? conversation.taskTrace,
            }
          : conversation
      )),
    }))
  }
  const advanceDelivery = (taskTrace: Conversation['taskTrace']) => {
    setWorkspace((state) => ({
      ...state,
      conversations: state.conversations.map((conversation) => (
        conversation.threadId === initial.threadId
          ? {
              ...conversation,
              lastSeq: (conversation.lastSeq ?? 0) + 1,
              messages: [{
                id: 'message-delivered-new',
                role: 'assistant',
                content: '同一 Run 的新投递',
                createdAt: BASE_TIME,
              }],
              taskTrace,
            }
          : conversation
      )),
    }))
  }
  const switchThread = (threadId: string) => {
    history.cancelInitialSelection()
    setWorkspace((state) => ({ ...state, currentThreadId: threadId }))
  }
  return {
    advanceDelivery,
    advanceTrace,
    history,
    setSearchOpen,
    setSearchScope,
    graph,
    followDetachedConversation,
    startOwnedRun,
    switchThread,
    workspace,
    setWorkspace,
  }
}

describe('useWorkspaceHistory Trace pagination authority', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    historyMocks.list.mockResolvedValue({ items: [], nextCursor: null })
    historyMocks.groupConfig.mockResolvedValue({ dayRanges: [] })
  })

  it.each(['hydrate', 'refresh', 'taskTrace', 'older'] as const)('%s 删除后迟到的详情不会重建会话', async (operation) => {
    const response = deferred<ConversationHistoryDetail>()
    const requested = deferred<void>()
    const initial = detail({
      historyCursor: 'older-page',
      taskTrace: { status: 'unavailable', todoGroups: [], errorCode: 'trace_incomplete' },
    })
    historyMocks.detail.mockImplementation(() => { requested.resolve(); return response.promise })
    const { result } = renderHook(() => useHarness(initial, { initiallyHydrated: operation !== 'hydrate' }))
    let loading: Promise<unknown> | undefined
    await act(async () => {
      if (operation === 'refresh') loading = result.current.history.hydrateConversation(THREAD_ID, { refresh: true })
      if (operation === 'taskTrace') loading = result.current.history.hydrateTaskTrace(THREAD_ID, true)
      if (operation === 'older') loading = result.current.history.loadOlderTrace(THREAD_ID)
      await requested.promise
    })
    act(() => result.current.setWorkspace({ conversations: [], currentThreadId: '' }))
    await act(async () => { response.resolve(initial); await loading })
    expect(result.current.workspace.conversations).toEqual([])
    expect(result.current.history.hydrationState).toBeNull()
  })

  it('刷新共享完成信号，切换会话后取消旧请求且忽略迟到详情', async () => {
    const response = deferred<ConversationHistoryDetail>()
    const requested = deferred<AbortSignal>()
    historyMocks.detail.mockImplementationOnce((_threadId: string, { signal }: { signal: AbortSignal }) => {
      requested.resolve(signal)
      return response.promise
    })
    const initial = detail({ status: { execution: 'succeeded', headRunId: RUN_ID } })
    const { result } = renderHook(() => useHarness(initial))
    let refreshing: Promise<void> = Promise.resolve()
    let requestSignal!: AbortSignal
    await act(async () => {
      refreshing = result.current.history.hydrateConversation(THREAD_ID, { refresh: true })
      requestSignal = await requested.promise
    })
    expect(result.current.history.hydrateConversation(THREAD_ID, { refresh: true })).toBe(refreshing)
    expect(historyMocks.detail).toHaveBeenCalledOnce()
    act(() => result.current.switchThread('another-thread'))
    expect(requestSignal.aborted).toBe(true)
    act(() => result.current.switchThread(THREAD_ID))
    historyMocks.detail.mockResolvedValueOnce(detail({
      asOfSeq: 6, observedAt: '2026-09-05T00:00:03.000000Z',
      status: { execution: 'succeeded', headRunId: RUN_ID },
    }))
    await act(() => result.current.history.hydrateConversation(THREAD_ID, { refresh: true }))
    expect(result.current.workspace.conversations[0]?.trace?.asOfSeq).toBe(6)
    await act(async () => {
      response.resolve(detail({ asOfSeq: 8, observedAt: '2026-09-05T00:00:04.000000Z' }))
      await refreshing
    })
    expect(result.current.workspace.conversations[0]?.trace?.asOfSeq).toBe(6)
    expect(result.current.history.hydrationState).toBeNull()
  })

  it('discards an old fixed-as-of page after follow advances the Trace', async () => {
    const { response, requested } = queueHistoryResponse()
    const initial = detail()
    const newer = detail({
      asOfSeq: 6,
      historyCursor: null,
      status: { execution: 'succeeded', headRunId: RUN_ID },
      messages: [{ ...initial.messages[0]!, content: '并发终态内容' }],
    })
    const olderPage = detail({
      historyCursor: 'cursor-2',
      messages: [{ ...initial.messages[0]!, content: '旧分页内容' }],
    })
    const { result } = renderHook(() => useHarness(initial))
    let loading: Promise<boolean> = Promise.resolve(false)

    act(() => {
      loading = result.current.history.loadOlderTrace(THREAD_ID)
    })
    await act(async () => { await requested.promise })
    expect(historyMocks.detail).toHaveBeenCalledOnce()
    act(() => result.current.advanceTrace(newer))
    expect(result.current.workspace.conversations[0]?.trace?.asOfSeq).toBe(6)

    let loaded = true
    await act(async () => {
      response.resolve(olderPage)
      loaded = await loading
    })

    const current = result.current.workspace.conversations[0]
    expect(loaded).toBe(false)
    expect(current?.trace?.asOfSeq).toBe(6)
    expect(current?.runStatus).toBe('idle')
    expect(current?.messages[0]?.content).toBe('并发终态内容')
  })

  it('does not apply a retry response after a same-batch thread switch', async () => {
    const { response, requested } = queueHistoryResponse()
    const initial = detail({
      taskTrace: {
        status: 'unavailable',
        todoGroups: [],
        errorCode: 'trace_incomplete',
      },
    })
    const { result } = renderHook(() => useHarness(initial))

    act(() => result.current.history.retryTaskTrace(THREAD_ID))
    await act(async () => { await requested.promise })
    expect(historyMocks.detail).toHaveBeenCalledOnce()
    await act(async () => {
      result.current.switchThread('thread-other')
      response.resolve(detail({
        taskTrace: { status: 'ready', todoGroups: [] },
      }))
    })

    expect(result.current.workspace.currentThreadId).toBe('thread-other')
    expect(result.current.workspace.conversations[0]?.taskTrace.phase).toBe('loading')
  })
})

describe('所选会话与链路共享激活刷新', () => {
  const initial = detail({ status: { execution: 'succeeded', headRunId: RUN_ID } })
  const page = { ...emptyTraceGraph(initial.asOfSeq), nextCursor: null, generation: initial.generation, headRunId: RUN_ID }

  beforeEach(() => {
    vi.resetAllMocks()
    historyMocks.list.mockResolvedValue({ items: [], nextCursor: null })
    historyMocks.groupConfig.mockResolvedValue({ dayRanges: [] })
    historyMocks.graph.mockResolvedValue(page)
    vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible')
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it.each([401, 403, 404, 503])('历史返回 %s 时区分无权访问与暂时读取失败', async (status) => {
    const request = queueHistoryResponse()
    const hook = renderHook(() => useHarness(initial, { traceActive: true }))
    await act(async () => { await request.requested.promise })
    expect(hook.result.current.graph.state.phase).toBe('ready')
    const completion = hook.result.current.history.hydrateConversation(THREAD_ID, { refresh: true })
    await act(async () => { request.response.reject(new ApiError('read failed', { status })); await completion })
    expect(hook.result.current.history.historyRefresh?.phase).toBe(status === 503 ? 'failed' : 'unavailable')
    expect(hook.result.current.graph.state.phase).toBe(status === 503 ? 'ready' : 'error')
    expect(historyMocks.graph).toHaveBeenCalledOnce()
  })
})


function useBootstrapHarness(catalogReady = true, archived = false, preferDraft = false, projectId = 'project-1') {
  const [searchOpen, setSearchOpen] = useState(false)
  const [searchScope, setSearchScope] = useState<'project' | 'all'>('project')
  const { workspace, setWorkspace, retainConversationDetails } = useWorkspaceState()
  const callbacks = useRef({
    followDetachedConversation: vi.fn(),
    prepareTaskTraceOwner: vi.fn<(threadId: string) => Promise<void>>(async () => undefined),
    onToast: vi.fn(),
  }).current
  const history = useWorkspaceHistory({projectId, archived, preferDraft, searchScope, searchOpen,
    workspace, setWorkspace, retainConversationDetails,
    defaultModelId: 'main',
    modelCatalogStatus: catalogReady ? 'ready' : 'loading',
    ...callbacks,
  })
  return { workspace, setWorkspace, history, setSearchOpen, setSearchScope, prepareTaskTraceOwner: callbacks.prepareTaskTraceOwner }
}

describe('会话列表变化通知', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.resetAllMocks()
    mockResourceNotices()
    historyMocks.groupConfig.mockResolvedValue({ dayRanges: [] })
    historyMocks.detail.mockResolvedValue(detail())
    historyMocks.title.mockResolvedValue(detail())
  })
  afterEach(() => {
    cleanup()
    window.history.replaceState(null, '', '/')
    vi.restoreAllMocks()
    vi.useRealTimers()
  })

  it('快速返回普通列表取消归档读取，迟到归档结果不能改变列表或当前会话', async () => {
    const normal = historyItemFromDetail(detail())
    const archived = historyItemFromDetail(detail({ threadId: 'archived-thread', archived: true }))
    const response = deferred<{ items: typeof archived[]; nextCursor: null }>()
    let archiveSignal: AbortSignal | undefined
    historyMocks.list.mockImplementation(options => {
      if (!options.archived) return Promise.resolve({ items: [normal], nextCursor: null })
      archiveSignal = options.signal
      return response.promise
    })
    const { result, rerender } = renderHook(({ archived }) => useBootstrapHarness(true, archived), { initialProps: { archived: false } })
    await act(async () => vi.advanceTimersByTimeAsync(0))
    rerender({ archived: true })
    await act(async () => vi.advanceTimersByTimeAsync(0))
    expect(archiveSignal?.aborted).toBe(false)
    rerender({ archived: false })
    await act(async () => vi.advanceTimersByTimeAsync(0))
    expect(archiveSignal?.aborted).toBe(true)
    await act(async () => { response.resolve({ items: [archived], nextCursor: null }); await vi.advanceTimersByTimeAsync(0) })
    expect(result.current.workspace.currentThreadId).toBe(normal.threadId)
    expect(result.current.history.historyConversations.map(item => item.threadId)).toEqual([normal.threadId])
  })
})


describe('弹框搜索的独立窗口', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.resetAllMocks()
    mockResourceNotices()
    historyMocks.groupConfig.mockResolvedValue({ dayRanges: [] })
  })
  afterEach(() => {
    cleanup()
    window.history.replaceState(null, '', '/')
    vi.restoreAllMocks()
    vi.useRealTimers()
  })

  it('改查询和关闭撤销请求，迟到搜索不能替换结果或侧栏', async () => {
    const initial = historyItemFromDetail(detail())
    const pending = deferred<{ items: typeof initial[]; nextCursor: null }>()
    let signal: AbortSignal | undefined
    historyMocks.list.mockImplementation(params => {
      if (params.query === '旧查询') { signal = params.signal; return pending.promise }
      return Promise.resolve({ items: [initial], nextCursor: null })
    })
    const { result, unmount } = renderHook(() => useHarness(detail(), { catalogReady: true }))
    await act(async () => vi.advanceTimersByTimeAsync(0))
    act(() => { result.current.setSearchOpen(true); result.current.history.setHistoryQuery('旧查询') })
    await act(async () => vi.advanceTimersByTimeAsync(300))
    expect(signal?.aborted).toBe(false)
    act(() => result.current.history.setHistoryQuery('新查询'))
    expect(signal?.aborted).toBe(true)
    expect(result.current.history.searchConversations).toEqual([])
    await act(async () => pending.resolve({ items: [{ ...initial, threadId: 'old-result' }], nextCursor: null }))
    expect(result.current.history.searchConversations).toEqual([])
    await act(async () => vi.advanceTimersByTimeAsync(300))
    expect(result.current.history.searchConversations.map(item => item.threadId)).toEqual([initial.threadId])
    act(() => result.current.setSearchOpen(false))
    expect(result.current.history.searchConversations).toEqual([])
    expect(result.current.history.historyConversations.map(item => item.threadId)).toEqual([initial.threadId])
    unmount()
  })
})
