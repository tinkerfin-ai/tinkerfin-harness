import { act, renderHook, waitFor } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { StreamedAgUiEvent } from '../../../api/conversation/client'
import { ApiError } from '../../../api/shared/http'
import { readActiveRunSession, writeActiveRunSession } from './activeRunSession'
import type {
  ConversationHistoryDetail,
  ConversationTraceEvent,
  followConversationRun,
} from '../../../api/conversation/history'
import type { ChatRequestPayload } from '../../../api/conversation/types'
import { emptyTraceGraph, traceGraphNode, traceGraphWithNodes } from '../../../test/traceFixtures'
import { restoreConversationFromTrace } from '../trace/runtime'
import { attachmentInput, messageAttachments, messageText } from '../attachments/content'
import { parseTaskTraceSnapshot } from '../../../api/conversation/taskTrace'
import todoMessagesFixture from '../../../../../server/tests/fixtures/todo-multimodal.json'
import todoSnapshotFixture from '../../../../../server/tests/fixtures/todo-multimodal-expected.json'
import type { Conversation, WorkspaceState } from '../../../types'
import { useConversationStreamController } from './useConversationStreamController'

const clientMocks = vi.hoisted(() => ({
  start: vi.fn(),
  resume: vi.fn(),
  cancel: vi.fn(),
}))
const traceMocks = vi.hoisted(() => ({
  detail: vi.fn(),
  follow: vi.fn(),
}))

vi.mock('../../../api/conversation/client', () => ({
  startConversationRun: clientMocks.start,
  resumeConversationRun: clientMocks.resume,
  cancelConversationRun: clientMocks.cancel,
}))

vi.mock(import('../../../api/conversation/history'), async (importOriginal) => ({
  ...await importOriginal(),
  fetchConversationHistoryDetail: traceMocks.detail,
  followConversationRun: traceMocks.follow,
}))

const THREAD_ID = 'thread-controller'
const RUN_ID = 'run-controller'
const BASE_TIME = '2026-08-09T00:00:00.000Z'

const payload: ChatRequestPayload = {
  threadId: THREAD_ID,
  runId: RUN_ID,
  state: {},
  messages: [],
  tools: [],
  context: [],
  forwardedProps: { accessMode: 'write_approval', model: 'main', command: { plan: 'off' } },
}

const traceDetail = (
  overrides: Partial<ConversationHistoryDetail> = {},
): ConversationHistoryDetail => ({ accessMode: 'write_approval',
  titleSource: 'default',
  titleGenerationStatus: 'idle',
  titleSeq: 0,
  id: 1,
  threadId: THREAD_ID,
  title: 'Trace authority',
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
    id: 'message-authoritative',
    traceSeq: 1,
    sourceId: 'assistant-authoritative',
    graphNamespace: [],
    runId: RUN_ID,
    role: 'assistant',
    content: 'Trace 最终内容',
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

const conversation = (overrides: Partial<Conversation> = {}): Conversation => ({ accessMode: 'write_approval',
  threadId: THREAD_ID,
  title: 'Controller test',
  pinned: false,
  updatedAt: BASE_TIME,
  model: 'main',
  mode: 'default',
  messages: [],
  todos: [],
  runStatus: 'streaming',
  activeRunId: RUN_ID,
  serverState: {},
  lastSeq: 0,
  isHydrated: true,
  ...overrides,
  taskTrace: overrides.taskTrace ?? {
    phase: 'ready',
    snapshot: { status: 'ready', todoGroups: [] },
  },
})

async function* streamItems(
  items: StreamedAgUiEvent[],
): AsyncGenerator<StreamedAgUiEvent> {
  for (const item of items) yield item
}

async function* traceItems(
  items: ConversationTraceEvent[],
): AsyncGenerator<ConversationTraceEvent> {
  for (const item of items) yield item
}

function useControllerHarness(initialConversation: Conversation) {
  const [workspace, setWorkspace] = useState<WorkspaceState>({
    conversations: [initialConversation],
    currentThreadId: initialConversation.threadId,
  })
  const [draftConversation, setDraftConversation] = useState<Conversation | null>(null)
  const controller = useConversationStreamController({
    workspace,
    setWorkspace,
    setDraftConversation,
  })
  return { controller, workspace, draftConversation, setWorkspace }
}

describe('useConversationStreamController', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    window.localStorage.clear()
    window.sessionStorage.clear()
    traceMocks.detail.mockResolvedValue(traceDetail())
    traceMocks.follow.mockImplementation(() => traceItems([]))
    clientMocks.cancel.mockResolvedValue({ cancelled: true })
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('preserves an unconfirmed submission and retries the same run only on explicit recovery', async () => {
    clientMocks.start.mockImplementation(async function* () {
      throw new ApiError('network unavailable', { status: 0 })
      yield* streamItems([])
    })
    const { result } = renderHook(() => useControllerHarness(conversation()))
    await act(async () => { await result.current.controller.streamRun(THREAD_ID, payload, 'start') })
    expect(result.current.workspace.conversations[0]).toMatchObject({
      runStatus: 'detached', activeRunId: RUN_ID,
      notice: { kind: 'warning', content: '连接已中断，尚无法确认任务状态，请恢复连接' },
    })
    expect(result.current.controller.hasActiveStream()).toBe(false)
    expect(readActiveRunSession(THREAD_ID)?.payload).toEqual(payload)
    await act(async () => { await result.current.controller.followDetachedConversation(THREAD_ID) })
    expect(traceMocks.follow).not.toHaveBeenCalled()
    expect(clientMocks.cancel).not.toHaveBeenCalled()
    expect(clientMocks.start).toHaveBeenCalledOnce()
    clientMocks.start.mockImplementation(() => streamItems([
      { event: { type: 'RUN_STARTED', threadId: THREAD_ID, runId: RUN_ID }, seq: 1 },
      { event: { type: 'RUN_FINISHED', threadId: THREAD_ID, runId: RUN_ID, outcome: { type: 'success' } }, seq: 2 },
    ]))
    await act(async () => { await result.current.controller.recoverConversation(THREAD_ID) })
    expect(clientMocks.start).toHaveBeenLastCalledWith(payload, expect.any(AbortSignal), 0)
    expect(readActiveRunSession(THREAD_ID)).toBeNull()
    expect(clientMocks.cancel).not.toHaveBeenCalled()
  })

  it('limits repeated disconnections and retains the last applied cursor without cancelling the task', async () => {
    vi.useFakeTimers()
    let attempts = 0
    clientMocks.start.mockImplementation(async function* () {
      attempts += 1
      if (attempts === 1) yield {
        event: { type: 'RUN_STARTED', threadId: THREAD_ID, runId: RUN_ID }, seq: 1,
      }
      throw new TypeError('stream interrupted')
    })
    traceMocks.follow.mockImplementation(async function* () { throw new TypeError('offline'); yield* traceItems([]) })
    const { result } = renderHook(() => useControllerHarness(conversation()))
    let running: Promise<void>
    await act(async () => {
      running = result.current.controller.streamRun(THREAD_ID, payload, 'start')
      await vi.advanceTimersByTimeAsync(20_000)
      await running
    })
    expect(attempts).toBe(4)
    expect(result.current.workspace.conversations[0]).toMatchObject({ runStatus: 'detached', activeRunId: RUN_ID, lastSeq: 1 })
    expect(readActiveRunSession(THREAD_ID)?.lastSeq).toBe(1)
    expect(clientMocks.cancel).not.toHaveBeenCalled()
    expect(result.current.controller.hasActiveStream()).toBe(false)
  })

  it('releases a reconnect wait on unmount and never requests backend cancellation', async () => {
    vi.useFakeTimers()
    clientMocks.start.mockImplementation(async function* () {
      yield { event: { type: 'RUN_STARTED', threadId: THREAD_ID, runId: RUN_ID }, seq: 1 }
      throw new TypeError('stream interrupted')
    })
    const { result, unmount } = renderHook(() => useControllerHarness(conversation()))
    let running: Promise<void>
    await act(async () => {
      running = result.current.controller.streamRun(THREAD_ID, payload, 'start')
      await vi.advanceTimersByTimeAsync(1)
    })
    unmount()
    await running!
    await vi.advanceTimersByTimeAsync(10_000)
    expect(clientMocks.start).toHaveBeenCalledOnce()
    expect(clientMocks.cancel).not.toHaveBeenCalled()
    expect(vi.getTimerCount()).toBe(0)
  })

  it.each(['success', 'failure'] as const)('keeps a confirmed %s terminal when history fails and retries only history', async (outcome) => {
    const terminal: StreamedAgUiEvent = outcome === 'success'
      ? { event: { type: 'RUN_FINISHED', threadId: THREAD_ID, runId: RUN_ID, outcome: { type: 'success' } }, seq: 2 }
      : { event: { type: 'RUN_ERROR', code: 'runtime_initialization_error', message: 'Agent run failed', rawEvent: { runId: RUN_ID } }, seq: 2 }
    clientMocks.start.mockImplementation(() => streamItems([
      { event: { type: 'RUN_STARTED', threadId: THREAD_ID, runId: RUN_ID }, seq: 1 }, terminal,
    ]))
    traceMocks.detail.mockRejectedValue(new ApiError('unavailable', { status: 503 }))
    const { result } = renderHook(() => useControllerHarness(conversation()))
    await act(async () => { await result.current.controller.streamRun(THREAD_ID, payload, 'start') })
    expect(result.current.workspace.conversations[0]).toMatchObject({
      runStatus: outcome === 'success' ? 'idle' : 'error', activeRunId: undefined,
      notice: { recovery: 'history' },
    })
    expect(readActiveRunSession(THREAD_ID)).toBeNull()
    traceMocks.detail.mockResolvedValue(traceDetail({
      status: { execution: outcome === 'success' ? 'succeeded' : 'failed', headRunId: RUN_ID },
    }))
    await act(async () => { await result.current.controller.recoverConversation(THREAD_ID) })
    expect(clientMocks.start).toHaveBeenCalledOnce()
    expect(clientMocks.cancel).not.toHaveBeenCalled()
    expect(result.current.workspace.conversations[0]?.notice).toBeUndefined()
    expect(result.current.workspace.conversations[0]?.runStatus).toBe(outcome === 'success' ? 'idle' : 'error')
    expect(readActiveRunSession(THREAD_ID)).toBeNull()
  })

  it('replaces the temporary AG-UI view with the terminal Trace snapshot', async () => {
    clientMocks.start.mockImplementation(() => streamItems([
      { seq: 1, event: { type: 'RUN_STARTED', threadId: THREAD_ID, runId: RUN_ID } },
      {
        seq: 2,
        event: {
          type: 'TEXT_MESSAGE_START',
          messageId: 'temporary',
          role: 'assistant',
        },
      },
      {
        seq: 3,
        event: {
          type: 'TEXT_MESSAGE_CONTENT',
          messageId: 'temporary',
          delta: '临时内容',
        },
      },
      {
        seq: 4,
        event: {
          type: 'RUN_FINISHED',
          threadId: THREAD_ID,
          runId: RUN_ID,
          outcome: { type: 'success' },
        },
      },
    ]))
    const { result } = renderHook(() => useControllerHarness(conversation()))

    await act(async () => {
      await result.current.controller.streamRun(THREAD_ID, payload, 'start')
    })

    await waitFor(() => {
      const current = result.current.workspace.conversations[0]
      expect(current?.messages.map((message) => message.content)).toEqual(['Trace 最终内容'])
      expect(current?.runStatus).toBe('idle')
      expect(current?.lastSeq).toBe(4)
      expect(current?.trace?.asOfSeq).toBe(5)
    })
    expect(traceMocks.detail).toHaveBeenCalledWith(THREAD_ID, {
      includeTaskTrace: true,
      signal: expect.any(AbortSignal),
      suppressGlobalError: true,
    })
  })

  it('审批恢复结束后同步权威历史，用户消息仍归属首次运行', async () => {
    const original = traceDetail({ headRunId: 'original-run', asOfSeq: 2, messages: [{
      ...traceDetail().messages[0]!, id: 'original-user', role: 'user', runId: 'original-run', content: '写入文件',
    }] })
    const initial = restoreConversationFromTrace(original, { model: 'main', includeTaskTrace: true })
    clientMocks.resume.mockImplementation(() => streamItems([
      { seq: 3, event: { type: 'RUN_STARTED', threadId: THREAD_ID, runId: RUN_ID } },
      { seq: 4, event: { type: 'RUN_FINISHED', threadId: THREAD_ID, runId: RUN_ID, outcome: { type: 'success' } } },
    ]))
    const { result } = renderHook(() => useControllerHarness(initial))
    await act(async () => { await result.current.controller.streamRun(THREAD_ID, payload, 'resume') })
    expect(result.current.workspace.conversations[0]?.trace?.headRunId).toBe(RUN_ID)
    expect(result.current.workspace.conversations[0]?.messages.map(message => message.content)).toEqual(['Trace 最终内容'])
  })

  it('accepts the first thread-wide sequence as the baseline when history has no cursor', async () => {
    clientMocks.resume.mockImplementation(() => streamItems([
      { seq: 185, event: { type: 'RUN_STARTED', threadId: THREAD_ID, runId: RUN_ID } },
      {
        seq: 186,
        event: {
          type: 'RUN_FINISHED',
          threadId: THREAD_ID,
          runId: RUN_ID,
          outcome: { type: 'success' },
        },
      },
    ]))
    const { result } = renderHook(() => useControllerHarness(
      conversation({ lastSeq: undefined }),
    ))

    await act(async () => {
      await result.current.controller.streamRun(THREAD_ID, payload, 'resume')
    })

    expect(clientMocks.resume).toHaveBeenCalledOnce()
    expect(clientMocks.resume).toHaveBeenCalledWith(
      payload,
      expect.any(AbortSignal),
      undefined,
    )
    expect(result.current.workspace.conversations[0]?.lastSeq).toBe(186)
  })

  it('repairs a sequence gap by reconnecting Messaging from the last applied seq', async () => {
    vi.useFakeTimers()
    const calls: Array<number | undefined> = []
    clientMocks.start.mockImplementation((
      _payload: ChatRequestPayload,
      _signal: AbortSignal,
      after?: number,
    ) => {
      calls.push(after)
      return calls.length === 1
        ? streamItems([{
            seq: 3,
            event: { type: 'STATE_SNAPSHOT', snapshot: { skipped: true } },
          }])
        : streamItems([
            { seq: 2, event: { type: 'STATE_SNAPSHOT', snapshot: { replayed: true } } },
            { seq: 3, event: { type: 'STATE_SNAPSHOT', snapshot: { replayed: true, next: true } } },
            {
              seq: 4,
              event: {
                type: 'RUN_FINISHED',
                threadId: THREAD_ID,
                runId: RUN_ID,
                outcome: { type: 'success' },
              },
            },
          ])
    })
    const { result } = renderHook(() => useControllerHarness(
      conversation({ lastSeq: 1 }),
    ))

    await act(async () => {
      const streaming = result.current.controller.streamRun(THREAD_ID, payload, 'start')
      await vi.advanceTimersByTimeAsync(250)
      await streaming
    })

    expect(calls).toEqual([undefined, 1])
    expect(traceMocks.detail).toHaveBeenCalledOnce()
  })

  it('deduplicates backend cancellation while the owned stream is active', async () => {
    let release: (() => void) | undefined
    clientMocks.start.mockImplementation(async function* () {
      yield { seq: 1, event: { type: 'RUN_STARTED', threadId: THREAD_ID, runId: RUN_ID } }
      await new Promise<void>((resolve) => {
        release = resolve
      })
      yield {
        seq: 2,
        event: {
          type: 'RUN_FINISHED',
          threadId: THREAD_ID,
          runId: RUN_ID,
          outcome: { type: 'success' },
        },
      }
    })
    const { result } = renderHook(() => useControllerHarness(conversation()))
    let streaming: Promise<void> = Promise.resolve()
    await act(async () => {
      streaming = result.current.controller.streamRun(THREAD_ID, payload, 'start')
      await Promise.resolve()
    })

    let first: Promise<boolean> = Promise.resolve(false)
    let second: Promise<boolean> = Promise.resolve(false)
    await act(async () => {
      first = result.current.controller.cancelRun(THREAD_ID)
      second = result.current.controller.cancelRun(THREAD_ID)
      await first
    })
    expect(first).toBe(second)
    expect(clientMocks.cancel).toHaveBeenCalledOnce()

    await act(async () => {
      release?.()
      await streaming
    })
  })

  it('从权威历史恢复独立运行错误，不合成消息或 Toast 通知', async () => {
    traceMocks.detail.mockResolvedValue(traceDetail({
      status: { execution: 'failed', headRunId: RUN_ID },
      messages: [],
      runFailures: [{ runId: RUN_ID, errorCode: 'failed', failedAt: '2026-09-08T00:00:00Z', retryable: false }],
    }))
    clientMocks.start.mockImplementation(() => streamItems([
      { seq: 1, event: { type: 'RUN_STARTED', threadId: THREAD_ID, runId: RUN_ID } },
      {
        seq: 2,
        event: {
          type: 'RUN_ERROR',
          rawEvent: { runId: RUN_ID },
          code: 'failed',
          message: 'temporary failure',
        },
      },
    ]))
    const { result } = renderHook(() => useControllerHarness(conversation()))

    await act(async () => {
      await result.current.controller.streamRun(THREAD_ID, payload, 'start')
    })

    await waitFor(() => {
      const current = result.current.workspace.conversations[0]
      expect(current?.messages.filter((message) => message.role === 'error')).toHaveLength(0)
      expect(current?.runFailures).toMatchObject([{ runId: RUN_ID, retryable: false }])
      expect(current?.notice).toBeUndefined()
    })
  })
})


function eventFeed() {
  const pending: StreamedAgUiEvent[] = []
  let wake: (() => void) | undefined
  let seq = 0
  return {
    push(event: StreamedAgUiEvent['event']) {
      pending.push({ event, seq: ++seq })
      wake?.()
    },
    async *read(signal: AbortSignal) {
      const onAbort = () => wake?.()
      signal.addEventListener('abort', onAbort)
      try {
        while (!signal.aborted) {
          const next = pending.shift()
          if (next) {
            yield next
            if (next.event.type === 'RUN_FINISHED') return
          } else {
            await new Promise<void>((resolve) => { wake = resolve })
          }
        }
      } finally {
        signal.removeEventListener('abort', onAbort)
      }
    },
  }
}

it('切换与启动 B 保留 A 的原连接，停止 B 不影响 A', async () => {
  const a = eventFeed()
  const b = eventFeed()
  const signals = new Map<string, AbortSignal>()
  clientMocks.start.mockImplementation((request: ChatRequestPayload, signal: AbortSignal) => {
    signals.set(request.threadId, signal)
    return (request.threadId === THREAD_ID ? a : b).read(signal)
  })
  clientMocks.cancel.mockResolvedValue({ cancelled: true })
  traceMocks.detail.mockImplementation((threadId: string) => Promise.resolve(traceDetail({
    threadId, headRunId: threadId === THREAD_ID ? RUN_ID : 'run-b',
  })))
  const { result } = renderHook(() => useControllerHarness(conversation()))
  let runA: Promise<void> = Promise.resolve()
  let runB: Promise<void> = Promise.resolve()
  await act(async () => { runA = result.current.controller.streamRun(THREAD_ID, payload, 'start') })
  await act(async () => {
    a.push({ type: 'RUN_STARTED', threadId: THREAD_ID, runId: RUN_ID })
    result.current.setWorkspace((state) => ({ ...state, currentThreadId: 'thread-b', conversations: [...state.conversations, conversation({ threadId: 'thread-b', activeRunId: 'run-b' })] }))
    runB = result.current.controller.streamRun('thread-b', { ...payload, threadId: 'thread-b', runId: 'run-b' }, 'start')
  })
  await waitFor(() => expect(signals.size).toBe(2))
  await act(async () => {
    b.push({ type: 'RUN_STARTED', threadId: 'thread-b', runId: 'run-b' })
    a.push({ type: 'TEXT_MESSAGE_START', messageId: 'a-text', role: 'assistant' })
    a.push({ type: 'TEXT_MESSAGE_CONTENT', messageId: 'a-text', delta: 'A仍在输出' })
    a.push({ type: 'TEXT_MESSAGE_END', messageId: 'a-text' })
    b.push({ type: 'TEXT_MESSAGE_START', messageId: 'b-text', role: 'assistant' })
    b.push({ type: 'TEXT_MESSAGE_CONTENT', messageId: 'b-text', delta: 'B独立输出' })
    b.push({ type: 'TEXT_MESSAGE_END', messageId: 'b-text' })
  })
  await waitFor(() => expect(result.current.workspace.conversations.find((item) => item.threadId === THREAD_ID)?.messages.some((item) => item.content === 'A仍在输出')).toBe(true))
  expect(result.current.workspace.currentThreadId).toBe('thread-b')
  expect(signals.get(THREAD_ID)?.aborted).toBe(false)
  expect(result.current.controller.isActiveThread(THREAD_ID)).toBe(true)
  await act(async () => { await result.current.controller.cancelRun('thread-b') })
  expect(clientMocks.cancel).toHaveBeenCalledWith('thread-b', 'run-b')
  expect(signals.get(THREAD_ID)?.aborted).toBe(false)
  await act(async () => {
    b.push({ type: 'RUN_FINISHED', threadId: 'thread-b', runId: 'run-b' })
    await runB
  })
  expect(result.current.controller.isActiveThread(THREAD_ID)).toBe(true)
  await act(async () => {
    a.push({ type: 'RUN_FINISHED', threadId: THREAD_ID, runId: RUN_ID })
    await runA
  })
  expect(result.current.controller.hasActiveStream()).toBe(false)
})

it('新草稿首帧晚到只登记原会话，不抢回当前页面', async () => {
  const feed = eventFeed()
  clientMocks.start.mockImplementation((_request: ChatRequestPayload, signal: AbortSignal) => feed.read(signal))
  traceMocks.detail.mockResolvedValue(traceDetail({ threadId: 'server-draft' }))
  const { result } = renderHook(() => useControllerHarness(conversation()))
  let running: Promise<void> = Promise.resolve()
  await act(async () => {
    running = result.current.controller.streamRun('', { ...payload, threadId: '' }, 'start', {
      target: 'draft', initialConversation: conversation({ threadId: '' }),
    })
  })
  act(() => result.current.controller.releaseDraft())
  await act(async () => { feed.push({ type: 'RUN_STARTED', threadId: 'server-draft', runId: RUN_ID }) })
  await waitFor(() => expect(result.current.workspace.conversations.some((item) => item.threadId === 'server-draft')).toBe(true))
  expect(result.current.workspace.currentThreadId).toBe(THREAD_ID)
  await act(async () => {
    feed.push({ type: 'RUN_FINISHED', threadId: 'server-draft', runId: RUN_ID })
    await running
  })
})

it.each(['success', 'failure'])('旧Run历史收尾不影响新Run：%s', async (outcome) => {
  const first = eventFeed()
  const second = eventFeed()
  let rejectHistory: (error: Error) => void = () => undefined
  let resolveHistory!: (detail: ConversationHistoryDetail) => void
  const history = new Promise<ConversationHistoryDetail>((resolve, reject) => { resolveHistory = resolve; rejectHistory = reject })
  traceMocks.detail.mockReset()
  traceMocks.detail.mockImplementationOnce(() => history).mockResolvedValue(traceDetail())
  clientMocks.start.mockImplementation((request: ChatRequestPayload, signal: AbortSignal) => {
    if (request.runId === RUN_ID) return first.read(signal)
    return (async function* () {
      for await (const item of second.read(signal)) yield { ...item, seq: (item.seq ?? 0) + 2 }
    })()
  })
  const { result, unmount } = renderHook(() => useControllerHarness(conversation()))
  let oldRun!: Promise<void>
  let newRun!: Promise<void>
  try {
    await act(async () => { oldRun = result.current.controller.streamRun(THREAD_ID, payload, 'start') })
    await act(async () => { first.push({ type: 'RUN_STARTED', threadId: THREAD_ID, runId: RUN_ID }) })
    await act(async () => { first.push({ type: 'RUN_FINISHED', threadId: THREAD_ID, runId: RUN_ID }) })
    await waitFor(() => expect(traceMocks.detail).toHaveBeenCalledOnce())
    await act(async () => { newRun = result.current.controller.streamRun(THREAD_ID, { ...payload, runId: 'new-run' }, 'start') })
    await act(async () => { second.push({ type: 'RUN_STARTED', threadId: THREAD_ID, runId: 'new-run' }) })
    expect(result.current.workspace.conversations[0]).toMatchObject({ activeRunId: 'new-run', runStatus: 'streaming' })
    await act(async () => { if (outcome === 'failure') rejectHistory(new Error('历史读取失败')); else resolveHistory(traceDetail()); await oldRun })
    expect(result.current.workspace.conversations[0]?.notice).toBeUndefined()
    expect(result.current.controller.isActiveThread(THREAD_ID)).toBe(true)
    expect(result.current.workspace.conversations[0]).toMatchObject({ activeRunId: 'new-run', runStatus: 'streaming' })
  } finally {
    resolveHistory(traceDetail())
    unmount()
    if (oldRun) await oldRun
    if (newRun) await newRun
  }
})

it('新恢复运行已经结束后，旧运行延迟返回的历史仍不可覆盖页面', async () => {
  const first = eventFeed()
  let resolveHistory!: (detail: ConversationHistoryDetail) => void
  let historyRequested!: () => void
  const requested = new Promise<void>(resolve => { historyRequested = resolve })
  const delayedHistory = new Promise<ConversationHistoryDetail>(resolve => { resolveHistory = resolve })
  traceMocks.detail.mockReset()
  traceMocks.detail.mockImplementationOnce(() => { historyRequested(); return delayedHistory })
    .mockResolvedValue(traceDetail({ headRunId: 'new-run', title: '新运行结果', titleSeq: 2 }))
  clientMocks.resume.mockImplementation((request: ChatRequestPayload, signal: AbortSignal) => request.runId === RUN_ID
    ? first.read(signal)
    : streamItems([
      { seq: 3, event: { type: 'RUN_STARTED', threadId: THREAD_ID, runId: 'new-run' } },
      { seq: 4, event: { type: 'RUN_FINISHED', threadId: THREAD_ID, runId: 'new-run' } },
    ]))
  const { result, unmount } = renderHook(() => useControllerHarness(conversation()))
  let oldRun!: Promise<void>
  try {
    await act(async () => {
      oldRun = result.current.controller.streamRun(THREAD_ID, payload, 'resume')
      first.push({ type: 'RUN_STARTED', threadId: THREAD_ID, runId: RUN_ID })
      first.push({ type: 'RUN_FINISHED', threadId: THREAD_ID, runId: RUN_ID })
      await requested
    })
    await act(async () => { await result.current.controller.streamRun(THREAD_ID, { ...payload, runId: 'new-run' }, 'resume') })
    const completed = result.current.workspace.conversations[0]
    expect(completed).toMatchObject({ runStatus: 'idle', title: '新运行结果' })
    await act(async () => { resolveHistory(traceDetail()); await oldRun })
    expect(result.current.workspace.conversations[0]).toBe(completed)
  } finally {
    resolveHistory(traceDetail())
    unmount()
    if (oldRun) await oldRun
  }
})

it('旧Run历史失败保留新Run首响应丢失的恢复状态', async () => {
  vi.useFakeTimers()
  const first = eventFeed()
  let rejectHistory!: (error: Error) => void
  const history = new Promise<ConversationHistoryDetail>((_resolve, reject) => { rejectHistory = reject })
  traceMocks.detail.mockReset()
  traceMocks.detail.mockImplementationOnce(() => history).mockResolvedValue(traceDetail())
  traceMocks.follow.mockReset()
  traceMocks.follow.mockImplementation(() => traceItems([]))
  clientMocks.start.mockImplementation((request: ChatRequestPayload, signal: AbortSignal) => {
    if (request.runId === RUN_ID) return first.read(signal)
    return (async function* () {
      yield* streamItems([])
      throw new ApiError('network unavailable', { status: 0 })
    })()
  })
  const { result, unmount } = renderHook(() => useControllerHarness(conversation()))
  let oldRun!: Promise<void>
  try {
    await act(async () => { oldRun = result.current.controller.streamRun(THREAD_ID, payload, 'start') })
    await act(async () => { first.push({ type: 'RUN_STARTED', threadId: THREAD_ID, runId: RUN_ID }) })
    await act(async () => { first.push({ type: 'RUN_FINISHED', threadId: THREAD_ID, runId: RUN_ID }) })
    expect(traceMocks.detail).toHaveBeenCalledOnce()
    act(() => result.current.setWorkspace((state) => ({ ...state, conversations: state.conversations.map((item) => ({ ...item, activeRunId: 'new-run', runStatus: 'streaming' })) })))
    await act(async () => { await result.current.controller.streamRun(THREAD_ID, { ...payload, runId: 'new-run' }, 'start') })
    expect(result.current.workspace.conversations[0]).toMatchObject({ activeRunId: 'new-run', runStatus: 'detached' })
    await act(async () => { rejectHistory(new Error('old history failed')); await oldRun })
    await act(async () => { await vi.advanceTimersByTimeAsync(200) })
    expect(result.current.workspace.conversations[0]).toMatchObject({ activeRunId: 'new-run', runStatus: 'detached' })
    expect(traceMocks.follow).not.toHaveBeenCalled()
  } finally {
    rejectHistory(new Error('cleanup'))
    unmount()
    if (oldRun) await oldRun
    vi.useRealTimers()
  }
})


type LiveEvent = ReturnType<typeof followConversationRun> extends AsyncGenerator<infer T> ? T : never

function traceFeed() {
  const pending: Array<{ event: LiveEvent; consumed: () => void }> = []
  let wake: (() => void) | undefined
  let markOpened!: () => void
  const opened = new Promise<void>((resolve) => { markOpened = resolve })
  let signal: AbortSignal | undefined
  return {
    opened,
    get signal() { return signal },
    push(event: LiveEvent) {
      const consumed = new Promise<void>((resolve) => { pending.push({ event, consumed: resolve }) })
      wake?.()
      return consumed
    },
    async *read(currentSignal: AbortSignal) {
      signal = currentSignal
      markOpened()
      const onAbort = () => wake?.()
      signal.addEventListener('abort', onAbort)
      try {
        while (!signal.aborted) {
          const next = pending.shift()
          if (!next) {
            await new Promise<void>((resolve) => { wake = resolve })
            continue
          }
          try { yield next.event } finally { next.consumed() }
          if (next.event.type === 'event' && next.event.event.type === 'RUN_FINISHED') return
        }
      } finally {
        signal.removeEventListener('abort', onAbort)
        pending.splice(0).forEach((entry) => entry.consumed())
      }
    },
  }
}

const runningTrace = (asOfSeq = 10): ConversationHistoryDetail => traceDetail({
  asOfSeq,
  status: { execution: 'running', headRunId: RUN_ID },
  messages: [{
    ...traceDetail().messages[0]!,
    agui: { kind: 'message', messageId: 'answer' },
    content: '本月门店营收', status: 'streaming', completedAt: null,
  }],
  toolCallCount: 1,
  graph: traceGraphWithNodes([traceGraphNode({
    id: 'report-tool', runId: RUN_ID, name: 'write_file', sourceId: 'native-write',
    agui: { kind: 'tool', toolCallId: 'write-report' },
    status: 'running', completedAt: null, request: { path: '/月报.md' },
  })], asOfSeq),
  state: { root: { section: 'revenue', totals: [10] }, subgraphs: {} },
})

const restored = (snapshot: ConversationHistoryDetail) => restoreConversationFromTrace(snapshot, {
  model: 'main', includeTaskTrace: true,
})

const cacheRun = (request = payload, lastSeq = 4) => writeActiveRunSession({
  threadId: request.threadId, payload: request, mode: 'start', lastSeq,
})

describe('已受理运行的历史恢复', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    window.sessionStorage.clear()
    clientMocks.cancel.mockResolvedValue({ cancelled: true })
  })

  afterEach(() => { vi.useRealTimers() })

  it('主动重载历史时在异步操作内拒绝相同观测的矛盾正文，保留原展示', async () => {
    const snapshot = traceDetail()
    const inconsistent = structuredClone(snapshot)
    inconsistent.messages[0]!.content = '同一观测出现了不同正文'
    traceMocks.detail.mockResolvedValue(inconsistent)
    const initial = {
      ...restored(snapshot),
      notice: { kind: 'error' as const, content: '历史同步失败', recovery: 'history' as const },
    }
    const { result } = renderHook(() => useControllerHarness(initial))
    await act(async () => { await result.current.controller.recoverConversation(THREAD_ID) })
    const current = result.current.workspace.conversations[0]!
    expect(current.messages).toEqual(initial.messages)
    expect(current.notice?.recovery).toBe('history')
    expect(clientMocks.start).not.toHaveBeenCalled()
  })

  it('其他运行的 Trace 不代表当前提交已受理，保留相同请求与游标重连', async () => {
    const old = runningTrace()
    const nextPayload = { ...payload, runId: 'next-run' }
    cacheRun(nextPayload, 73)
    clientMocks.start.mockImplementation(() => streamItems([
      { seq: 74, event: { type: 'RUN_STARTED', threadId: THREAD_ID, runId: nextPayload.runId } },
      { seq: 75, event: { type: 'RUN_FINISHED', threadId: THREAD_ID, runId: nextPayload.runId, outcome: { type: 'success' } } },
    ]))
    traceMocks.detail.mockResolvedValue(traceDetail({ headRunId: nextPayload.runId, status: { execution: 'succeeded', headRunId: nextPayload.runId }, asOfSeq: 11 }))
    const { result, unmount } = renderHook(() => useControllerHarness({ ...restored(old), activeRunId: nextPayload.runId, lastSeq: 73 }))
    try {
      await act(async () => { await result.current.controller.recoverConversation(THREAD_ID) })
      expect(clientMocks.start).toHaveBeenCalledExactlyOnceWith(nextPayload, expect.any(AbortSignal), 73)
      expect(traceMocks.follow).not.toHaveBeenCalled()
      expect(readActiveRunSession(THREAD_ID)).toBeNull()
    } finally { unmount() }
  })

  it.each([false, true])('刷新恢复不依赖本地缓存，暂停时首段可见且续流不重复（缓存=%s）', async (cached) => {
    if (cached) cacheRun(payload, 999)
    const base = traceDetail({ asOfSeq: 2, graph: emptyTraceGraph(2), messages: [], messageCount: 0, status: { execution: 'running', headRunId: RUN_ID } })
    const feed = traceFeed()
    traceMocks.follow.mockImplementation((_thread, _run, { signal }) => feed.read(signal))
    const final = traceDetail({ messages: [{ ...traceDetail().messages[0]!, agui: { kind: 'message', messageId: 'answer' }, content: '第一段第二段' }] })
    traceMocks.detail.mockResolvedValue(final)
    const { result, unmount } = renderHook(() => useControllerHarness(restored(runningTrace())))
    let recovering!: Promise<void>
    await act(async () => { recovering = result.current.controller.recoverConversation(THREAD_ID) })
    try {
      await feed.opened
      await act(async () => { await feed.push({ type: 'snapshot', snapshot: base, replay: true }) })
      await act(async () => {
        await feed.push({ type: 'event', replayed: true, seq: 279, event: { type: 'RUN_STARTED', threadId: THREAD_ID, runId: RUN_ID } })
        await feed.push({ type: 'event', replayed: true, seq: 280, event: { type: 'TEXT_MESSAGE_START', messageId: 'answer', role: 'assistant' } })
        await feed.push({ type: 'event', replayed: true, seq: 281, event: { type: 'TEXT_MESSAGE_CONTENT', messageId: 'answer', delta: '第一段' } })
      })
      expect(result.current.workspace.conversations[0]?.messages.find(item => item.id === 'answer')?.content).toBe('第一段')
      expect(result.current.workspace.conversations[0]?.messages.find(item => item.id === 'answer')?.liveText).toBeUndefined()
      expect(result.current.workspace.conversations[0]?.runStatus).toBe('streaming')
      expect(clientMocks.start).not.toHaveBeenCalled()
      expect(clientMocks.resume).not.toHaveBeenCalled()
      expect(traceMocks.follow.mock.calls[0]?.[2].afterSeq).toBeUndefined()
      await act(async () => {
        await feed.push({ type: 'event', replayed: true, seq: 281, event: { type: 'TEXT_MESSAGE_CONTENT', messageId: 'answer', delta: '第一段' } })
        await feed.push({ type: 'event', replayed: false, seq: 282, event: { type: 'TEXT_MESSAGE_CONTENT', messageId: 'answer', delta: '第二段' } })
      })
      expect(result.current.workspace.conversations[0]?.messages.find(item => item.id === 'answer')?.liveText?.initialContent).toBe('第一段')
      expect(result.current.workspace.conversations[0]?.messages.find(item => item.id === 'answer')?.content).toBe('第一段第二段')
      await act(async () => { await feed.push({ type: 'event', replayed: false, seq: 283, event: { type: 'RUN_FINISHED', threadId: THREAD_ID, runId: RUN_ID, outcome: { type: 'success' } } }); await recovering })
      expect(result.current.workspace.conversations[0]?.runStatus).toBe('idle')
      expect(readActiveRunSession(THREAD_ID)).toBeNull()
    } finally { unmount(); await recovering }
  })

  it('续播序号缺口从最后已应用游标重连，保留正文且不重复执行', async () => {
    vi.useFakeTimers()
    const base = traceDetail({ asOfSeq: 2, graph: emptyTraceGraph(2), messages: [], messageCount: 0, status: { execution: 'running', headRunId: RUN_ID } })
    traceMocks.follow.mockImplementation(async function* (_thread, _run, options) {
      if (options.afterSeq == null) {
        yield { type: 'snapshot', snapshot: base, replay: true }
        yield { type: 'event', replayed: true, seq: 279, event: { type: 'RUN_STARTED', threadId: THREAD_ID, runId: RUN_ID } }
        yield { type: 'event', replayed: true, seq: 280, event: { type: 'TEXT_MESSAGE_START', messageId: 'answer', role: 'assistant' } }
        yield { type: 'event', replayed: true, seq: 281, event: { type: 'TEXT_MESSAGE_CONTENT', messageId: 'answer', delta: '第一段' } }
        yield { type: 'event', replayed: false, seq: 283, event: { type: 'TEXT_MESSAGE_CONTENT', messageId: 'answer', delta: '不应应用' } }
      } else {
        expect(options.afterSeq).toBe(281)
        yield { type: 'event', replayed: false, seq: 282, event: { type: 'TEXT_MESSAGE_CONTENT', messageId: 'answer', delta: '第二段' } }
        yield { type: 'event', replayed: false, seq: 283, event: { type: 'RUN_FINISHED', threadId: THREAD_ID, runId: RUN_ID, outcome: { type: 'success' } } }
      }
    })
    traceMocks.detail.mockResolvedValue(traceDetail({ messages: [{ ...traceDetail().messages[0]!, agui: { kind: 'message', messageId: 'answer' }, content: '第一段第二段' }] }))
    const { result, unmount } = renderHook(() => useControllerHarness(restored(runningTrace())))
    let recovering!: Promise<void>
    try {
      await act(async () => { recovering = result.current.controller.recoverConversation(THREAD_ID) })
      expect(result.current.workspace.conversations[0]?.messages.find(item => item.id === 'answer')?.content).toBe('第一段')
      await act(async () => { await vi.advanceTimersByTimeAsync(250); await recovering })
      expect(result.current.workspace.conversations[0]?.messages.find(item => item.id === 'answer')?.content).toBe('第一段第二段')
      expect(traceMocks.follow).toHaveBeenCalledTimes(2)
      expect(clientMocks.start).not.toHaveBeenCalled()
      expect(clientMocks.resume).not.toHaveBeenCalled()
    } finally { unmount(); await recovering }
  })

  it('恢复连接卸载只关闭读取，不取消后台运行', async () => {
    const feed = traceFeed()
    traceMocks.follow.mockImplementation((_thread, _run, { signal }) => feed.read(signal))
    const { result, unmount } = renderHook(() => useControllerHarness(restored(runningTrace())))
    let recovering!: Promise<void>
    await act(async () => { recovering = result.current.controller.recoverConversation(THREAD_ID) })
    await feed.opened
    unmount()
    await recovering
    expect(feed.signal?.aborted).toBe(true)
    expect(clientMocks.cancel).not.toHaveBeenCalled()
  })

  it('其他线程的恢复快照被拒绝，保留当前显示内容', async () => {
    traceMocks.follow.mockImplementation(async function* () { yield { type: 'snapshot', snapshot: { ...runningTrace(), threadId: 'other' }, replay: true } })
    const initial = restored(runningTrace())
    const { result } = renderHook(() => useControllerHarness(initial))
    await act(async () => { await result.current.controller.recoverConversation(THREAD_ID) })
    expect(result.current.workspace.conversations[0]?.messages).toEqual(initial.messages)
    expect(result.current.workspace.conversations[0]?.notice?.kind).toBe('error')
    expect(clientMocks.start).not.toHaveBeenCalled()
  })
})

describe('多模态提问的任务组标题', () => {
  beforeEach(() => { vi.resetAllMocks(); window.sessionStorage.clear(); vi.useFakeTimers({ toFake: ['Date'] }) })
  afterEach(() => { vi.useRealTimers() })

  it.each(['plain', 'multimodal', 'attachments'] as const)('%s 从实时输出到历史加载保持同一任务组', async (form) => {
    const nativeUser = todoMessagesFixture.find(event => event.fact.kind === 'message')
    const content = nativeUser?.fact.content?.value
    if (!Array.isArray(content)) throw new Error('missing user message')
    const userContent = form === 'plain' ? '核对销售与库存，整理交付清单' : form === 'attachments' ? content.slice(1) : content
    const text = messageText(userContent)
    const attachments = messageAttachments(userContent)
    const expected = parseTaskTraceSnapshot(todoSnapshotFixture)
    if (expected.status !== 'ready') throw new Error('missing todo group')
    const group = expected.todoGroups[0]!
    const todos = todoMessagesFixture.filter(event => event.fact.kind === 'state.revision').at(-1)?.fact.changes?.value?.todos
    if (!todos) throw new Error('missing todo state')
    group.userMessagePreview = form === 'attachments' ? '销售表.xlsx, 库存.pdf, 说明.docx' : '核对销售与库存，整理交付清单'
    const threadId = 'thread:multimodal'
    const runId = 'run:multimodal'
    vi.setSystemTime(new Date(group.createdAt))
    const ready = traceDetail({
      threadId, headRunId: runId, availableHeads: [runId], asOfSeq: 179,
      status: { execution: 'succeeded', headRunId: runId }, taskTrace: expected,
    })
    const feed = eventFeed()
    clientMocks.start.mockImplementation((_request: ChatRequestPayload, signal: AbortSignal) => feed.read(signal))
    let finishHistory!: (detail: ConversationHistoryDetail) => void
    traceMocks.detail.mockReturnValue(new Promise<ConversationHistoryDetail>(resolve => { finishHistory = resolve }))
    const request: ChatRequestPayload = {
      ...payload, threadId, runId,
      messages: [{ id: 'message:user', role: 'user', content: attachments.length ? [{ type: 'text', text }, ...attachments.map(attachmentInput)] : text }],
    }
    const { result, unmount } = renderHook(() => useControllerHarness(conversation({
      threadId, activeRunId: runId,
      messages: [{ id: 'message:user', role: 'user', content: text, attachments, createdAt: BASE_TIME, meta: { runId } }],
    })))
    let streaming: Promise<void> = Promise.resolve()
    try {
      await act(async () => { streaming = result.current.controller.streamRun(threadId, request, 'start') })
      await act(async () => {
        feed.push({ type: 'RUN_STARTED', threadId, runId })
        feed.push({ type: 'TOOL_CALL_START', toolCallId: group.groupToolCallId, toolCallName: 'write_todos', parentMessageId: 'assistant:todos' })
        feed.push({ type: 'TOOL_CALL_ARGS', toolCallId: group.groupToolCallId, delta: JSON.stringify({ todos }) })
        feed.push({ type: 'TOOL_CALL_END', toolCallId: group.groupToolCallId })
        feed.push({ type: 'TOOL_CALL_RESULT', toolCallId: group.groupToolCallId, messageId: 'result:todos', content: '已更新任务清单', role: 'tool' })
        feed.push({ type: 'STATE_SNAPSHOT', snapshot: { todos } })
        feed.push({ type: 'RUN_FINISHED', threadId, runId, outcome: { type: 'success' } })
      })
      const live = result.current.workspace.conversations[0]?.taskTrace
      expect(live?.phase).toBe('ready')
      if (live?.phase !== 'ready') throw new Error('missing live todo group')
      expect(live.snapshot).toEqual({ ...expected, todoGroups: expected.todoGroups.map(item => ({ ...item, createdAt: new Date(item.createdAt).toISOString() })) })
      await act(async () => { finishHistory(ready); await streaming })
      const restored = result.current.workspace.conversations[0]?.taskTrace
      expect(restored).toEqual({ phase: 'ready', snapshot: expected })
      expect(clientMocks.start).toHaveBeenCalledExactlyOnceWith(request, expect.any(AbortSignal), undefined)
    } finally { finishHistory(ready); unmount(); await streaming }
  })
})

it.each(['resume', 'follow'] as const)('审批工具跨运行返回时保持 Todo 清单：%s', async (mode) => {
  const group = {
    id: 'todo-group:original', userMessageId: 'question', userMessagePreview: '生成报告',
    groupToolCallId: 'trace:todos', createdAt: BASE_TIME, status: 'running' as const,
    todos: [{ id: 'todo', content: '生成报告', status: 'running' as const }],
  }
  const headRunId = mode === 'resume' ? 'previous-resume' : RUN_ID
  const base = traceDetail({
    headRunId, availableHeads: [headRunId],
    status: { execution: mode === 'resume' ? 'waiting' : 'running', headRunId },
    messages: [{
      ...traceDetail().messages[0]!, id: 'question', sourceId: 'question',
      role: 'user', runId: 'original', content: '生成报告',
    }],
    taskTrace: { status: 'ready', todoGroups: [group] },
    graph: traceGraphWithNodes([
      traceGraphNode({ id: 'trace:todos', agui: { kind: 'tool', toolCallId: 'todos' }, name: 'write_todos', runId: 'previous-resume', startedSeq: 2 }),
      traceGraphNode({ id: 'trace:write', agui: { kind: 'tool', toolCallId: 'write' }, name: 'write_file', runId: 'previous-resume', status: 'waiting', startedSeq: 3 }),
    ], 5),
  })
  let processed!: () => void
  const received = new Promise<void>(resolve => { processed = resolve })
  let finish!: () => void
  const finishing = new Promise<void>(resolve => { finish = resolve })
  const events: StreamedAgUiEvent[] = [
    { seq: 1, event: { type: 'RUN_STARTED', threadId: THREAD_ID, runId: RUN_ID } },
    { seq: 2, event: { type: 'TOOL_CALL_RESULT', toolCallId: 'write', messageId: 'write-result', content: 'written', role: 'tool' } },
  ]
  const terminal: StreamedAgUiEvent = { seq: 3, event: { type: 'RUN_FINISHED', threadId: THREAD_ID, runId: RUN_ID } }
  clientMocks.resume.mockImplementation(async function* () {
    yield* streamItems(events)
    processed()
    await finishing
    yield terminal
  })
  traceMocks.follow.mockImplementation(async function* () {
    yield { type: 'snapshot', snapshot: base, replay: true }
    for (const item of events) yield { type: 'event', replayed: true, ...item }
    processed()
    await finishing
    yield { type: 'event', replayed: false, ...terminal }
  })
  const final = traceDetail({
    ...base, asOfSeq: 6, headRunId: RUN_ID, availableHeads: [RUN_ID],
    status: { execution: 'succeeded', headRunId: RUN_ID },
    graph: traceGraphWithNodes(base.graph.nodes.map(node => ({ ...node, status: 'succeeded' })), 6),
    taskTrace: { status: 'ready', todoGroups: [{ ...group, status: 'incomplete', todos: [{ ...group.todos[0]!, status: 'incomplete' }] }] },
  })
  traceMocks.detail.mockResolvedValue(final)
  const initial = restoreConversationFromTrace(base, { model: 'main', includeTaskTrace: true })
  const { result, unmount } = renderHook(() => useControllerHarness(initial))
  let streaming!: Promise<void>
  try {
    await act(async () => {
      await result.current.controller.handoffTaskTraceFollow(THREAD_ID)
      streaming = mode === 'resume'
        ? result.current.controller.streamRun(THREAD_ID, payload, 'resume')
        : result.current.controller.followDetachedConversation(THREAD_ID)
      await received
    })
    expect(result.current.workspace.conversations[0]?.taskTrace).toEqual({ phase: 'ready', snapshot: { status: 'ready', todoGroups: [group] } })
    await act(async () => { finish(); await streaming })
    expect(result.current.workspace.conversations[0]?.taskTrace).toEqual({ phase: 'ready', snapshot: final.taskTrace })
  } finally {
    finish()
    unmount()
    await streaming
  }
})
