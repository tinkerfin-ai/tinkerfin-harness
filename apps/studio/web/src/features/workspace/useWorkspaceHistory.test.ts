import { act, cleanup, renderHook } from '@testing-library/react'
import { useLayoutEffect, useRef } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ConversationHistoryDetail } from '../../api/conversation/history'
import { ApiError } from '../../api/shared/http'
import { upsertConversation } from '../../lib/workspace'
import { emptyTraceGraph } from '../../test/traceFixtures'
import type { Conversation } from '../../types'
import { restoreConversationFromTrace } from '../conversation/trace/runtime'
import { useChainTrace } from '../conversation/chainTrace/useChainTrace'
import { useWorkspaceState } from './useWorkspaceState'
import {
  historyItemFromDetail,
  mergeHistoryConversations,
  useWorkspaceHistory,
} from './useWorkspaceHistory'

const historyMocks = vi.hoisted(() => ({
  detail: vi.fn(),
  groupConfig: vi.fn(),
  list: vi.fn(),
  graph: vi.fn(),
  followGraph: vi.fn(),
}))

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

type ReadyTaskTrace = Extract<Conversation['taskTrace'], { phase: 'ready' }>

const readyTaskTrace = (suffix: string): ReadyTaskTrace => ({
  phase: 'ready',
  snapshot: {
    status: 'ready',
    todoGroups: [{
      id: `todo-group:${suffix}`,
      userMessageId: `message:${suffix}`,
      userMessagePreview: `任务 ${suffix}`,
      groupToolCallId: `tool:${suffix}`,
      createdAt: BASE_TIME,
      status: 'running',
      todos: [],
    }],
  },
})

const detail = (
  overrides: Partial<ConversationHistoryDetail> = {},
): ConversationHistoryDetail => ({ accessMode: 'write_approval',
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
  interactions: [],
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
    initiallyHydrated?: boolean
    onToast?: (kind: 'error', message: string) => void
    prepareTaskTraceOwner?: (threadId: string) => Promise<void>
    traceActive?: boolean
  } = {},
) {
  const followDetachedConversation = useRef(vi.fn()).current
  const defaultPrepareTaskTraceOwner = useRef(vi.fn(async () => undefined)).current
  const { workspace, setWorkspace, retainConversationDetails } = useWorkspaceState()
  const initialWorkspace = useRef({
    conversations: [{
      ...restoreConversationFromTrace(initial, { model: 'main', includeTaskTrace: true }),
      isHydrated: options.initiallyHydrated ?? true,
    }],
    currentThreadId: initial.threadId,
  }).current
  useLayoutEffect(() => { setWorkspace(initialWorkspace) }, [initialWorkspace, setWorkspace])
  const history = useWorkspaceHistory({
    workspace,
    setWorkspace,
    retainConversationDetails,
    defaultModelId: 'main',
    modelCatalogStatus: 'loading',
    refreshOnActivation: options.traceActive,
    followDetachedConversation,
    prepareTaskTraceOwner: options.prepareTaskTraceOwner ?? defaultPrepareTaskTraceOwner,
    onToast: options.onToast ?? vi.fn(),
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
    setWorkspace((state) => ({ ...state, currentThreadId: threadId }))
  }
  return {
    advanceDelivery,
    advanceTrace,
    history,
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

  it.each(['refresh', 'taskTrace', 'older'] as const)('%s 接口遇到同一观测的矛盾正文时保留已有内容并报告失败', async (operation) => {
    const initial = detail({
      status: { execution: 'succeeded', headRunId: RUN_ID },
      taskTrace: { status: 'unavailable', todoGroups: [], errorCode: 'trace_incomplete' },
    })
    const inconsistent = structuredClone(initial)
    inconsistent.messages[0]!.content = '相同观测中的另一份正文'
    const { response, requested } = queueHistoryResponse()
    const onToast = vi.fn()
    const { result } = renderHook(() => useHarness(initial, { onToast }))
    let loading: Promise<void | boolean> = Promise.resolve()
    act(() => {
      if (operation === 'refresh') {
        loading = result.current.history.hydrateConversation(THREAD_ID, { refresh: true })
      } else if (operation === 'taskTrace') {
        loading = result.current.history.hydrateTaskTrace(THREAD_ID, true)
      } else {
        loading = result.current.history.loadOlderTrace(THREAD_ID)
      }
    })
    await act(async () => { await requested.promise })
    expect(historyMocks.detail).toHaveBeenCalledOnce()
    await act(async () => {
      response.resolve(inconsistent)
      const loaded = await loading
      if (operation !== 'refresh') expect(loaded).toBe(false)
    })
    expect(result.current.workspace.conversations[0]?.messages[0]?.content).toBe('初始内容')
    expect(historyMocks.detail).toHaveBeenCalledOnce()
    if (operation === 'taskTrace') {
      expect(result.current.history.taskTraceLoadFailed).toBe(true)
    } else {
      expect(onToast).toHaveBeenCalledWith('error', '会话加载失败，请重试')
    }
  })

  it('重新激活的历史详情不能覆盖等待期间新启动的本地 Run', async () => {
    const { response, requested } = queueHistoryResponse()
    const initial = detail({ status: { execution: 'succeeded', headRunId: RUN_ID } })
    const { result } = renderHook(() => useHarness(initial))
    let refreshing: Promise<void> = Promise.resolve()
    act(() => { refreshing = result.current.history.hydrateConversation(THREAD_ID, { refresh: true }) })
    await act(async () => { await requested.promise })
    expect(historyMocks.detail).toHaveBeenCalledOnce()
    act(() => result.current.startOwnedRun())
    await act(async () => {
      response.resolve(detail({ observedAt: '2026-09-05T00:00:02.000000Z' }))
      await refreshing
    })
    const current = result.current.workspace.conversations[0]!
    expect(current.runStatus).toBe('streaming')
    expect(current.activeRunId).toBe('run-owned-new')
    expect(current.messages[0]!.content).toBe('本次新输入')
    expect(result.current.history.hydrationState).toBeNull()
    await act(() => result.current.history.hydrateConversation(THREAD_ID, { refresh: true }))
    expect(historyMocks.detail).toHaveBeenCalledOnce()
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

  it('刷新失败保持已有会话并提示可重试，下一次刷新可以恢复', async () => {
    historyMocks.detail.mockRejectedValueOnce(new Error('offline'))
    const onToast = vi.fn()
    const initial = detail({ status: { execution: 'unknown', headRunId: RUN_ID } })
    const { result } = renderHook(() => useHarness(initial, { onToast }))
    await act(() => result.current.history.hydrateConversation(THREAD_ID, { refresh: true }))
    expect(onToast).toHaveBeenCalledWith('error', '会话加载失败，请重试')
    expect(result.current.workspace.conversations[0]?.trace?.status.execution).toBe('unknown')
    historyMocks.detail.mockResolvedValueOnce(detail({ observedAt: '2026-09-05T00:00:01.000000Z' }))
    await act(() => result.current.history.hydrateConversation(THREAD_ID, { refresh: true }))
    expect(result.current.workspace.conversations[0]?.runStatus).toBe('detached')
    expect(result.current.history.hydrationState).toBeNull()
  })

  it('重新激活时重验同一运行中的 Run，并重新连接已结束的会话观察', async () => {
    const { response, requested } = queueHistoryResponse()
    const { result } = renderHook(() => useHarness(detail()))
    expect(result.current.followDetachedConversation).toHaveBeenCalledOnce()
    let refreshing: Promise<void> = Promise.resolve()
    act(() => { refreshing = result.current.history.hydrateConversation(THREAD_ID, { refresh: true }) })
    await act(async () => { await requested.promise })
    expect(historyMocks.detail).toHaveBeenCalledOnce()
    await act(async () => {
      response.resolve(detail({ observedAt: '2026-09-05T00:00:01.000000Z' }))
      await refreshing
    })
    expect(result.current.followDetachedConversation).toHaveBeenCalledTimes(2)
  })

  it('同一持久化前缀的刷新保留已展开历史，Run 前进后使用新的权威窗口', async () => {
    const currentMessages = [{ ...detail().messages[0]!, traceSeq: 2 }]
    const initial = detail({
      historyCursor: null,
      status: { execution: 'succeeded', headRunId: RUN_ID },
      messages: [{ ...detail().messages[0]!, id: 'older-message', traceSeq: 1 }, ...currentMessages],
    })
    historyMocks.detail.mockResolvedValueOnce(detail({
      observedAt: '2026-09-05T00:00:01.000000Z',
      status: { execution: 'succeeded', headRunId: RUN_ID },
      messages: currentMessages,
    }))
    const { result } = renderHook(() => useHarness(initial))
    await act(() => result.current.history.hydrateConversation(THREAD_ID, { refresh: true }))
    expect(result.current.workspace.conversations[0]?.trace?.messages).toHaveLength(2)
    expect(result.current.workspace.conversations[0]?.trace?.historyCursor).toBeNull()
    expect(result.current.workspace.conversations[0]?.trace?.observedAt).toBe('2026-09-05T00:00:01.000000Z')
    historyMocks.detail.mockResolvedValueOnce(detail({
      asOfSeq: 6,
      observedAt: '2026-09-05T00:00:02.000000Z',
      status: { execution: 'succeeded', headRunId: RUN_ID },
      messages: currentMessages,
    }))
    await act(() => result.current.history.hydrateConversation(THREAD_ID, { refresh: true }))
    expect(result.current.workspace.conversations[0]?.trace?.messages).toHaveLength(1)
    expect(result.current.workspace.conversations[0]?.trace?.asOfSeq).toBe(6)
  })

  it('刷新拒绝同一持久化前缀中的实体冲突，并保留已知内容', async () => {
    const initial = detail({ status: { execution: 'succeeded', headRunId: RUN_ID } })
    historyMocks.detail.mockResolvedValueOnce(detail({
      observedAt: '2026-09-05T00:00:01.000000Z',
      messages: [{ ...initial.messages[0]!, content: '冲突内容' }],
    }))
    const onToast = vi.fn()
    const { result } = renderHook(() => useHarness(initial, { onToast }))
    await act(() => result.current.history.hydrateConversation(THREAD_ID, { refresh: true }))
    expect(result.current.workspace.conversations[0]?.trace?.messages).toEqual(initial.messages)
    expect(onToast).toHaveBeenCalledWith('error', '会话加载失败，请重试')
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

  it('adds fixed-prefix history without reverting a same-sequence ownership update', async () => {
    const { response, requested } = queueHistoryResponse()
    const initial = detail()
    const newer = detail({
      observedAt: '2026-09-05T00:00:00.000001Z',
      status: { execution: 'unknown', headRunId: RUN_ID },
      completeness: { ...initial.completeness, missingTail: true },
    })
    const olderPage = detail({
      historyCursor: 'cursor-2',
      messages: [{ ...initial.messages[0]!, id: 'older-message', content: '历史内容' }, ...initial.messages],
    })
    const { result } = renderHook(() => useHarness(initial))
    let loading: Promise<boolean> = Promise.resolve(false)
    act(() => { loading = result.current.history.loadOlderTrace(THREAD_ID) })
    await act(async () => { await requested.promise })
    expect(historyMocks.detail).toHaveBeenCalledOnce()
    act(() => result.current.advanceTrace(newer))
    let loaded = false
    await act(async () => {
      response.resolve(olderPage)
      loaded = await loading
    })
    const current = result.current.workspace.conversations[0]
    expect(loaded).toBe(true)
    expect(current?.trace?.observedAt).toBe(newer.observedAt)
    expect(current?.runStatus).toBe('error')
    expect(current?.trace?.completeness.missingTail).toBe(true)
    expect(current?.trace?.historyCursor).toBe('cursor-2')
    expect(current?.messages[0]?.content).toBe('历史内容')
  })

  it('discards an old page after an owned run starts before Trace advances', async () => {
    const { response, requested } = queueHistoryResponse()
    const initial = detail()
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
    act(() => result.current.startOwnedRun())
    expect(result.current.workspace.conversations[0]?.runStatus).toBe('streaming')

    let loaded = true
    await act(async () => {
      response.resolve(olderPage)
      loaded = await loading
    })

    const current = result.current.workspace.conversations[0]
    expect(loaded).toBe(false)
    expect(current?.runStatus).toBe('streaming')
    expect(current?.activeRunId).toBe('run-owned-new')
    expect(current?.messages[0]?.content).toBe('本次新输入')
    expect(current?.trace?.asOfSeq).toBe(5)
  })

  it('forwards caller cancellation to an older Trace page request', async () => {
    const requested = deferred<void>()
    historyMocks.detail.mockImplementation((
      _threadId: string,
      options: { signal?: AbortSignal },
    ) => new Promise<ConversationHistoryDetail>((_resolve, reject) => {
      requested.resolve()
      options.signal?.addEventListener(
        'abort',
        () => reject(new DOMException('aborted', 'AbortError')),
        { once: true },
      )
    }))
    const { result } = renderHook(() => useHarness(detail()))
    const controller = new AbortController()
    let loading: Promise<boolean> = Promise.resolve(false)

    act(() => {
      loading = result.current.history.loadOlderTrace(THREAD_ID, {
        signal: controller.signal,
      })
    })
    await act(async () => { await requested.promise })
    expect(historyMocks.detail).toHaveBeenCalledOnce()
    const requestSignal = historyMocks.detail.mock.calls[0]?.[1]?.signal as
      | AbortSignal
      | undefined

    await act(async () => {
      controller.abort()
      await expect(loading).resolves.toBe(false)
    })
    expect(requestSignal?.aborted).toBe(true)
  })

  it('reloads an unavailable task trace once when the user explicitly retries', async () => {
    const { response, requested } = queueHistoryResponse()
    const initial = detail({
      taskTrace: {
        status: 'unavailable',
        todoGroups: [],
        errorCode: 'trace_incomplete',
      },
    })
    const refreshed = detail({
      asOfSeq: 6,
      taskTrace: { status: 'ready', todoGroups: [] },
    })
    const prepareTaskTraceOwner = vi.fn(async () => undefined)
    const { result } = renderHook(() => useHarness(initial, { prepareTaskTraceOwner }))

    expect(result.current.workspace.conversations[0]?.taskTrace.phase).toBe('unavailable')
    act(() => {
      result.current.history.retryTaskTrace(THREAD_ID)
      result.current.history.retryTaskTrace(THREAD_ID)
    })

    await act(async () => { await requested.promise })
    expect(historyMocks.detail).toHaveBeenCalledOnce()
    expect(prepareTaskTraceOwner).toHaveBeenCalledOnce()
    expect(prepareTaskTraceOwner).toHaveBeenCalledWith(THREAD_ID)
    expect(historyMocks.detail).toHaveBeenCalledWith(THREAD_ID, expect.objectContaining({
      includeTaskTrace: true,
      suppressGlobalError: true,
    }))
    expect(result.current.workspace.conversations[0]?.taskTrace.phase).toBe('loading')

    await act(async () => response.resolve(refreshed))
    expect(result.current.workspace.conversations[0]?.taskTrace).toEqual({
      phase: 'ready',
      snapshot: { status: 'ready', todoGroups: [] },
    })
  })

  it('keeps conversation hydration recoverable and emits one toast', async () => {
    const onToast = vi.fn()
    const prepareTaskTraceOwner = vi.fn(async () => undefined)
    const { response, requested } = queueHistoryResponse()
    const { result } = renderHook(() => useHarness(detail(), {
      initiallyHydrated: false,
      onToast,
      prepareTaskTraceOwner,
    }))

    await act(async () => { await requested.promise })
    const loading = result.current.history.hydrateConversation(THREAD_ID)
    await act(async () => { response.reject(new Error('会话恢复失败')); await loading })
    expect(result.current.history.hydrationState).toEqual({
      threadId: THREAD_ID,
      status: 'failed',
      unavailable: false,
    })
    expect(onToast).toHaveBeenCalledExactlyOnceWith('error', '会话加载失败，请重试')
  })

  it('does not let a retry response replace an owned run started after the request', async () => {
    const { response, requested } = queueHistoryResponse()
    const initial = detail({
      taskTrace: {
        status: 'unavailable',
        todoGroups: [],
        errorCode: 'trace_incomplete',
      },
    })
    const { result } = renderHook(() => useHarness(initial))
    const ownedTaskTrace = readyTaskTrace('owned-new')

    act(() => result.current.history.retryTaskTrace(THREAD_ID))
    await act(async () => { await requested.promise })
    expect(historyMocks.detail).toHaveBeenCalledOnce()
    act(() => result.current.startOwnedRun(ownedTaskTrace))
    expect(result.current.workspace.conversations[0]?.activeRunId).toBe('run-owned-new')

    await act(async () => response.resolve(detail({
      status: { execution: 'succeeded', headRunId: RUN_ID },
      taskTrace: { status: 'ready', todoGroups: [] },
    })))

    const current = result.current.workspace.conversations[0]
    expect(current?.runStatus).toBe('streaming')
    expect(current?.activeRunId).toBe('run-owned-new')
    expect(current?.messages).toMatchObject([{
      id: 'message-owned-new',
      content: '本次新输入',
    }])
    expect(current?.taskTrace).toEqual(ownedTaskTrace)
  })

  it('does not let a retry response roll back a newer followed Trace', async () => {
    const { response, requested } = queueHistoryResponse()
    const initial = detail({
      taskTrace: {
        status: 'unavailable',
        todoGroups: [],
        errorCode: 'trace_incomplete',
      },
    })
    const followedTaskTrace = readyTaskTrace('followed-newer')
    const newer = detail({
      asOfSeq: 6,
      messages: [{ ...initial.messages[0]!, content: 'follow 推进后的内容' }],
      status: { execution: 'succeeded', headRunId: RUN_ID },
      taskTrace: followedTaskTrace.snapshot,
    })
    const { result } = renderHook(() => useHarness(initial))

    act(() => result.current.history.retryTaskTrace(THREAD_ID))
    await act(async () => { await requested.promise })
    expect(historyMocks.detail).toHaveBeenCalledOnce()
    act(() => result.current.advanceTrace(newer))
    expect(result.current.workspace.conversations[0]?.trace?.asOfSeq).toBe(6)

    await act(async () => response.resolve(detail({
      taskTrace: { status: 'ready', todoGroups: [] },
    })))

    const current = result.current.workspace.conversations[0]
    expect(current?.trace?.asOfSeq).toBe(6)
    expect(current?.messages[0]?.content).toBe('follow 推进后的内容')
    expect(current?.taskTrace).toEqual(followedTaskTrace)
  })

  it('does not let a retry response overwrite a newer delivery in the same Run', async () => {
    const { response, requested } = queueHistoryResponse()
    const initial = detail({
      taskTrace: {
        status: 'unavailable',
        todoGroups: [],
        errorCode: 'trace_incomplete',
      },
    })
    const { result } = renderHook(() => useHarness(initial))
    const deliveredTaskTrace = readyTaskTrace('delivered-newer')

    act(() => result.current.history.retryTaskTrace(THREAD_ID))
    await act(async () => { await requested.promise })
    expect(historyMocks.detail).toHaveBeenCalledOnce()
    act(() => result.current.advanceDelivery(deliveredTaskTrace))
    expect(result.current.workspace.conversations[0]?.lastSeq).toBe(1)

    await act(async () => response.resolve(detail({
      taskTrace: { status: 'ready', todoGroups: [] },
    })))

    const current = result.current.workspace.conversations[0]
    expect(current?.lastSeq).toBe(1)
    expect(current?.messages).toMatchObject([{
      id: 'message-delivered-new',
      content: '同一 Run 的新投递',
    }])
    expect(current?.taskTrace).toEqual(deliveredTaskTrace)
  })

  it('does not report a stale retry failure after an owned run is queued', async () => {
    const { response, requested } = queueHistoryResponse()
    const initial = detail({
      taskTrace: {
        status: 'unavailable',
        todoGroups: [],
        errorCode: 'trace_incomplete',
      },
    })
    const onToast = vi.fn()
    const { result } = renderHook(() => useHarness(initial, { onToast }))
    const ownedTaskTrace = readyTaskTrace('owned-after-failure')

    act(() => result.current.history.retryTaskTrace(THREAD_ID))
    await act(async () => { await requested.promise })
    expect(historyMocks.detail).toHaveBeenCalledOnce()
    await act(async () => {
      result.current.startOwnedRun(ownedTaskTrace)
      response.reject(new Error('旧请求失败'))
    })

    expect(result.current.workspace.conversations[0]?.activeRunId).toBe('run-owned-new')
    expect(result.current.workspace.conversations[0]?.taskTrace).toEqual(ownedTaskTrace)
    expect(result.current.history.taskTraceLoadFailed).toBe(false)
    expect(onToast).not.toHaveBeenCalled()
  })

  it('keeps a current retry failure recoverable without a duplicate toast', async () => {
    const { response, requested } = queueHistoryResponse()
    const initial = detail({
      taskTrace: {
        status: 'unavailable',
        todoGroups: [],
        errorCode: 'trace_incomplete',
      },
    })
    const onToast = vi.fn()
    const { result } = renderHook(() => useHarness(initial, { onToast }))

    act(() => result.current.history.retryTaskTrace(THREAD_ID))
    await act(async () => { await requested.promise })
    expect(historyMocks.detail).toHaveBeenCalledOnce()
    await act(async () => response.reject(new Error('当前请求失败')))

    expect(result.current.history.taskTraceLoadFailed).toBe(true)
    expect(result.current.workspace.conversations[0]?.taskTrace.phase).toBe('unloaded')
    expect(onToast).toHaveBeenCalledExactlyOnceWith('error', '任务轨迹不可用')
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

  it('does not apply or retain a retry failure after a same-batch thread switch', async () => {
    const { response, requested } = queueHistoryResponse()
    const initial = detail({
      taskTrace: {
        status: 'unavailable',
        todoGroups: [],
        errorCode: 'trace_incomplete',
      },
    })
    const onToast = vi.fn()
    const { result } = renderHook(() => useHarness(initial, { onToast }))

    act(() => result.current.history.retryTaskTrace(THREAD_ID))
    await act(async () => { await requested.promise })
    expect(historyMocks.detail).toHaveBeenCalledOnce()
    await act(async () => {
      result.current.switchThread('thread-other')
      response.reject(new Error('切换后的旧失败'))
    })

    expect(result.current.workspace.currentThreadId).toBe('thread-other')
    expect(result.current.workspace.conversations[0]?.taskTrace.phase).toBe('loading')
    expect(result.current.history.taskTraceLoadFailed).toBe(false)
    expect(onToast).not.toHaveBeenCalled()
  })

  it('lets a newer externally owned page request replace an aborting locator request', async () => {
    const initial = detail()
    const olderPage = detail({
      historyCursor: null,
      messages: [{ ...initial.messages[0]!, id: 'message-older', content: '更早内容' }],
      taskTrace: null,
    })
    historyMocks.detail
      .mockImplementationOnce((
        _threadId: string,
        options: { signal?: AbortSignal },
      ) => new Promise<ConversationHistoryDetail>((_resolve, reject) => {
        options.signal?.addEventListener(
          'abort',
          () => reject(new DOMException('aborted', 'AbortError')),
          { once: true },
        )
      }))
      .mockResolvedValueOnce(olderPage)
    const { result } = renderHook(() => useHarness(initial))
    const firstController = new AbortController()
    const secondController = new AbortController()
    let first: Promise<boolean> = Promise.resolve(false)
    let second: Promise<boolean> = Promise.resolve(false)

    act(() => {
      first = result.current.history.loadOlderTrace(THREAD_ID, {
        signal: firstController.signal,
      })
    })
    expect(historyMocks.detail).toHaveBeenCalledTimes(1)
    firstController.abort()
    act(() => {
      second = result.current.history.loadOlderTrace(THREAD_ID, {
        signal: secondController.signal,
      })
    })

    let firstLoaded = true
    let secondLoaded = false
    await act(async () => {
      firstLoaded = await first
      secondLoaded = await second
    })
    expect(firstLoaded).toBe(false)
    expect(secondLoaded).toBe(true)
    expect(historyMocks.detail).toHaveBeenCalledTimes(2)
    expect(result.current.workspace.conversations[0]?.messages[0]?.content)
      .toBe('更早内容')
  })

  it('does not let a stale list summary roll back a hydrated Trace terminal', () => {
    const initial = detail()
    const terminal = detail({
      asOfSeq: 6,
      status: { execution: 'succeeded', headRunId: RUN_ID },
      updatedAt: '2026-08-28T00:00:10.000Z',
      lastModel: 'trace-model',
    })
    const current = {
      ...restoreConversationFromTrace(terminal, { model: 'fallback', includeTaskTrace: true }),
      isHydrated: true,
    }
    const stale = {
      ...historyItemFromDetail(initial),
      title: '列表新标题',
      pinned: true,
      status: 'running',
      lastRunId: 'run-stale',
      lastModel: 'stale-model',
      updatedAt: '2026-08-28T00:00:01.000Z',
    }

    const merged = mergeHistoryConversations([current], [stale], 'fallback')[0]

    expect(merged).toMatchObject({
      title: '列表新标题',
      pinned: true,
      runStatus: 'idle',
      model: 'trace-model',
      updatedAt: terminal.updatedAt,
    })
    expect(merged?.activeRunId).toBeUndefined()
    expect(merged?.trace?.asOfSeq).toBe(6)
  })

  it('accepts a newer waiting summary and requires fresh Trace hydration', () => {
    const terminal = detail({
      asOfSeq: 6,
      status: { execution: 'succeeded', headRunId: RUN_ID },
      updatedAt: '2026-08-28T00:00:10.000Z',
    })
    const current = {
      ...restoreConversationFromTrace(terminal, { model: 'fallback', includeTaskTrace: true }),
      isHydrated: true,
    }
    const waiting = {
      ...historyItemFromDetail(terminal),
      status: 'waiting_approval',
      pendingInteractionKind: 'plan_review' as const,
      hasPendingInterrupt: true,
      updatedAt: '2026-08-28T00:00:11.000Z',
    }

    const merged = mergeHistoryConversations([current], [waiting], 'fallback')[0]

    expect(merged).toMatchObject({
      runStatus: 'waiting_approval',
      pendingInteractionKind: 'plan_review',
      updatedAt: waiting.updatedAt,
      isHydrated: false,
    })
  })

  it('keeps multiple same-kind Tool groups visible in a synthesized summary', () => {
    const source = detail({
      interactions: [
        {
          agui: null,
          id: 'interaction-a',
          traceSeq: 5,
          sourceId: 'interrupt-a',
          graphNamespace: [],
          runId: RUN_ID,
          kind: 'tool_approval',
          toolCallIds: ['call-a'],
          status: 'pending',
          payloadOmitted: false,
          payload: {},
          openedAt: BASE_TIME,
        },
        {
          agui: null,
          id: 'interaction-b',
          traceSeq: 6,
          sourceId: 'interrupt-b',
          graphNamespace: ['tools:child'],
          runId: RUN_ID,
          kind: 'tool_approval',
          toolCallIds: ['call-b'],
          status: 'pending',
          payloadOmitted: false,
          payload: {},
          openedAt: BASE_TIME,
        },
      ],
    })

    const item = historyItemFromDetail(source)

    expect(item.hasPendingInterrupt).toBe(true)
    expect(item.pendingInteractionKind).toBe('tool_approval')
  })
})

describe('所选会话与链路共享激活刷新', () => {
  const initial = detail({ status: { execution: 'succeeded', headRunId: RUN_ID } })
  const page = { ...emptyTraceGraph(initial.asOfSeq), nextCursor: null, generation: initial.generation, headRunId: RUN_ID }
  const observed = (second: number): ConversationHistoryDetail => ({
    ...initial,
    observedAt: `2026-09-05T00:00:0${second}.000000Z`,
  })

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

  it('首次进入并发读取，链路先显示且会话观测到达不重复查询', async () => {
    const request = queueHistoryResponse()
    const hook = renderHook(() => useHarness(initial, { traceActive: true }), { reactStrictMode: true })
    await act(async () => { await request.requested.promise })
    expect(historyMocks.detail).toHaveBeenCalledOnce()
    expect(historyMocks.graph).toHaveBeenCalledOnce()
    expect(hook.result.current.graph.state).toEqual({ phase: 'ready', page })
    expect(hook.result.current.graph.historyStatus).toBe('pending')
    const completion = hook.result.current.history.hydrateConversation(THREAD_ID, { refresh: true })
    await act(async () => { request.response.resolve(observed(1)); await completion })
    expect(hook.result.current.workspace.conversations[0]?.trace?.observedAt).toBe(observed(1).observedAt)
    expect(hook.result.current.history.historyRefresh?.phase).toBe('ready')
    expect(hook.result.current.graph.state).toEqual({ phase: 'ready', page })
    expect(historyMocks.graph).toHaveBeenCalledOnce()
  })

  it('切回对话保留共享刷新，再进入链路并发读取图且复用会话请求', async () => {
    const request = queueHistoryResponse()
    const hook = renderHook(({ traceActive }) => useHarness(initial, { traceActive }), {
      initialProps: { traceActive: true },
    })
    let signal!: AbortSignal
    await act(async () => { signal = await request.requested.promise })
    hook.rerender({ traceActive: false })
    expect(signal.aborted).toBe(false)
    expect(historyMocks.detail).toHaveBeenCalledOnce()
    await act(async () => hook.rerender({ traceActive: true }))
    expect(historyMocks.detail).toHaveBeenCalledOnce()
    expect(historyMocks.graph).toHaveBeenCalledTimes(2)
    const completion = hook.result.current.history.hydrateConversation(THREAD_ID, { refresh: true })
    await act(async () => { request.response.resolve(observed(1)); await completion })
    expect(historyMocks.graph).toHaveBeenCalledTimes(2)
    expect(signal.aborted).toBe(false)
    hook.rerender({ traceActive: false })
    expect(historyMocks.detail).toHaveBeenCalledOnce()
  })

  it('刷新在对话页完成时仍应用历史，后续进入链路重新重验', async () => {
    const request = queueHistoryResponse()
    const hook = renderHook(({ traceActive }) => useHarness(initial, { traceActive }), {
      initialProps: { traceActive: true },
    })
    await act(async () => { await request.requested.promise })
    const completion = hook.result.current.history.hydrateConversation(THREAD_ID, { refresh: true })
    hook.rerender({ traceActive: false })
    await act(async () => { request.response.resolve(observed(1)); await completion })
    expect(hook.result.current.workspace.conversations[0]?.trace?.observedAt).toBe(observed(1).observedAt)
    expect(historyMocks.graph).toHaveBeenCalledOnce()
    const next = queueHistoryResponse()
    hook.rerender({ traceActive: true })
    await act(async () => { await next.requested.promise })
    expect(historyMocks.graph).toHaveBeenCalledTimes(2)
    const nextCompletion = hook.result.current.history.hydrateConversation(THREAD_ID, { refresh: true })
    await act(async () => { next.response.resolve(observed(2)); await nextCompletion })
    expect(historyMocks.detail).toHaveBeenCalledTimes(2)
    expect(historyMocks.graph).toHaveBeenCalledTimes(2)
  })

  it.each([
    { leaveTrace: true, failed: false },
    { leaveTrace: false, failed: false },
    { leaveTrace: false, failed: true },
  ])('恢复焦点后重验在途读取，离开链路=$leaveTrace、旧读取失败=$failed', async ({ leaveTrace, failed }) => {
    const old = queueHistoryResponse()
    const next = queueHistoryResponse()
    const hook = renderHook(({ traceActive }) => useHarness(initial, { traceActive }), {
      initialProps: { traceActive: true },
    })
    let signal!: AbortSignal
    await act(async () => { signal = await old.requested.promise })
    act(() => { window.dispatchEvent(new Event('blur')) })
    act(() => { window.dispatchEvent(new Event('focus')) })
    const completion = hook.result.current.history.hydrateConversation(THREAD_ID, { refresh: true })
    if (leaveTrace) hook.rerender({ traceActive: false })
    await act(async () => {
      if (failed) old.response.reject(new Error('失焦前的读取失败'))
      else old.response.resolve(observed(1))
      await next.requested.promise
    })
    expect(signal.aborted).toBe(false)
    expect(hook.result.current.workspace.conversations[0]?.trace?.observedAt).toBe(initial.observedAt)
    expect(historyMocks.detail).toHaveBeenCalledTimes(2)
    expect(historyMocks.graph).toHaveBeenCalledTimes(leaveTrace ? 1 : 2)
    await act(async () => { next.response.resolve(observed(2)); await completion })
    expect(hook.result.current.workspace.conversations[0]?.trace?.observedAt).toBe(observed(2).observedAt)
    expect(historyMocks.graph).toHaveBeenCalledTimes(leaveTrace ? 1 : 2)
  })

  it('焦点与可见性同时恢复只重验一次，同序号状态变化复用并发读到的图', async () => {
    const first = queueHistoryResponse()
    const hook = renderHook(() => useHarness(initial, { traceActive: true }))
    await act(async () => { await first.requested.promise })
    const firstCompletion = hook.result.current.history.hydrateConversation(THREAD_ID, { refresh: true })
    await act(async () => { first.response.resolve(observed(1)); await firstCompletion })
    expect(hook.result.current.graph.state).toEqual({ phase: 'ready', page })
    const next = queueHistoryResponse()
    act(() => { window.dispatchEvent(new Event('blur')) })
    act(() => {
      window.dispatchEvent(new Event('focus'))
      document.dispatchEvent(new Event('visibilitychange'))
    })
    await act(async () => { await next.requested.promise })
    expect(historyMocks.detail).toHaveBeenCalledTimes(2)
    expect(historyMocks.graph).toHaveBeenCalledTimes(2)
    expect(hook.result.current.graph.state).toEqual({ phase: 'ready', page })
    const completion = hook.result.current.history.hydrateConversation(THREAD_ID, { refresh: true })
    const changed = { ...observed(2), status: { execution: 'abandoned' as const, headRunId: RUN_ID },
      completeness: { missingPrefix: false, missingTail: true, payloadOmitted: false } }
    await act(async () => { next.response.resolve(changed); await completion })
    expect(hook.result.current.workspace.conversations[0]?.trace).toMatchObject({
      asOfSeq: initial.asOfSeq, status: changed.status, completeness: changed.completeness,
    })
    expect(historyMocks.graph).toHaveBeenCalledTimes(2)
  })

  it('隐藏页面首次进入不读取，显示后按新观测加载一次', async () => {
    const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden')
    const request = queueHistoryResponse()
    const hook = renderHook(() => useHarness(initial, { traceActive: true }))
    expect(historyMocks.detail).not.toHaveBeenCalled()
    expect(historyMocks.graph).not.toHaveBeenCalled()
    visibility.mockReturnValue('visible')
    act(() => { document.dispatchEvent(new Event('visibilitychange')) })
    await act(async () => { await request.requested.promise })
    const completion = hook.result.current.history.hydrateConversation(THREAD_ID, { refresh: true })
    await act(async () => { request.response.resolve(observed(1)); await completion })
    expect(historyMocks.detail).toHaveBeenCalledOnce()
    expect(historyMocks.graph).toHaveBeenCalledOnce()
  })

  it('历史刷新失败保留会话并提示，链路不会永久等待且下次激活可恢复', async () => {
    const request = queueHistoryResponse()
    const onToast = vi.fn()
    const hook = renderHook(({ traceActive }) => useHarness(initial, { traceActive, onToast }), {
      initialProps: { traceActive: true },
    })
    await act(async () => { await request.requested.promise })
    const completion = hook.result.current.history.hydrateConversation(THREAD_ID, { refresh: true })
    await act(async () => { request.response.reject(new Error('offline')); await completion })
    expect(onToast).toHaveBeenCalledWith('error', '会话加载失败，请重试')
    expect(hook.result.current.workspace.conversations[0]?.trace?.observedAt).toBe(initial.observedAt)
    expect(hook.result.current.history.historyRefresh?.phase).toBe('failed')
    expect(hook.result.current.graph.state).toEqual({ phase: 'ready', page })
    hook.rerender({ traceActive: false })
    const next = queueHistoryResponse()
    hook.rerender({ traceActive: true })
    await act(async () => { await next.requested.promise })
    const nextCompletion = hook.result.current.history.hydrateConversation(THREAD_ID, { refresh: true })
    await act(async () => { next.response.resolve(observed(1)); await nextCompletion })
    expect(hook.result.current.history.hydrationState).toBeNull()
    expect(historyMocks.graph).toHaveBeenCalledTimes(2)
    expect(onToast).toHaveBeenCalledOnce()
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

  it.each(['delete', 'switch', 'unmount'] as const)('%s 释放会话所有权并拒绝迟到结果', async (operation) => {
    const request = queueHistoryResponse()
    const hook = renderHook(() => useHarness(initial, { traceActive: true }))
    let signal!: AbortSignal
    await act(async () => { signal = await request.requested.promise })
    const completion = hook.result.current.history.hydrateConversation(THREAD_ID, { refresh: true })
    if (operation === 'unmount') hook.unmount()
    else if (operation === 'delete') act(() => hook.result.current.setWorkspace({ conversations: [], currentThreadId: '' }))
    else act(() => hook.result.current.switchThread('another-thread'))
    expect(signal.aborted).toBe(true)
    await act(async () => { request.response.resolve(observed(1)); await completion })
    expect(hook.result.current.workspace.conversations.find(item => item.threadId === THREAD_ID)?.trace?.observedAt)
      .not.toBe(observed(1).observedAt)
    expect(historyMocks.graph.mock.calls.filter(([threadId]) => threadId === THREAD_ID)).toHaveLength(1)
  })

  it.each(['local', 'remote'] as const)('%s 新运行进入实时跟随，不等待旧历史且不重复查询非实时图', async (source) => {
    const request = queueHistoryResponse()
    const followed = deferred<AbortSignal>()
    const closed = deferred<void>()
    historyMocks.followGraph.mockImplementation(async function* (
      _threadId: string, _filter: unknown, { signal }: { signal: AbortSignal },
    ) {
      const aborted = deferred<void>()
      signal.addEventListener('abort', () => aborted.resolve(), { once: true })
      followed.resolve(signal)
      try {
        yield { type: 'snapshot', snapshot: page }
        await aborted.promise
      } finally { closed.resolve() }
    })
    const hook = renderHook(() => useHarness(initial, { traceActive: true }))
    await act(async () => { await request.requested.promise })
    const completion = hook.result.current.history.hydrateConversation(THREAD_ID, { refresh: true })
    if (source === 'local') {
      act(() => hook.result.current.startOwnedRun())
      await act(async () => { await followed.promise })
      await act(async () => { request.response.resolve(observed(1)); await completion })
      expect(hook.result.current.workspace.conversations[0]?.activeRunId).toBe('run-owned-new')
    } else {
      await act(async () => {
        request.response.resolve({ ...observed(1), status: { execution: 'running', headRunId: RUN_ID } })
        await completion
      })
      await act(async () => { await followed.promise })
      expect(hook.result.current.workspace.conversations[0]?.runStatus).toBe('detached')
    }
    expect(historyMocks.graph).toHaveBeenCalledOnce()
    expect(historyMocks.followGraph).toHaveBeenCalledOnce()
    hook.unmount()
    await act(async () => { await closed.promise })
  })
})
