import { act, renderHook, waitFor } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { StreamedAgUiEvent } from '../../../api/conversation/client'
import { ApiError } from '../../../api/shared/http'
import { readActiveRunSession, writeActiveRunSession } from './activeRunSession'
import type {
  ConversationHistoryDetail,
  ConversationTraceEvent,
} from '../../../api/conversation/history'
import type { ChatRequestPayload } from '../../../api/conversation/types'
import { emptyTraceGraph, emptyTraceGraphDelta, traceGraphNode, traceGraphWithNodes } from '../../../test/traceFixtures'
import { restoreConversationFromTrace } from '../trace/runtime'
import { toolReviewInterrupts } from '../../../test/aguiFixtures'
import { prepareResumeSubmission } from '../agui'
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
  followConversationTrace: traceMocks.follow,
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
      notice: { kind: 'error', content: '连接已中断，尚无法确认任务状态，请恢复连接' },
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

  it('hydrates and follows a detached run only through Trace events', async () => {
    const snapshot = traceDetail({
      status: { execution: 'running', headRunId: RUN_ID },
      messages: [],
      asOfSeq: 2,
    })
    traceMocks.detail.mockResolvedValue(traceDetail({
      asOfSeq: 3,
      messages: [{ ...traceDetail().messages[0]!, content: 'detached update' }],
    }))
    traceMocks.follow.mockImplementation(() => traceItems([
      { type: 'snapshot', snapshot },
      {
        type: 'update',
        taskTrace: null,
        update: {
          asOfSeq: 3,
          generation: 'generation-test',
          observedAt: '2026-09-05T00:00:00.000001Z',
          events: [],
          facts: [],
          messages: {
            upserts: [{
              ...traceDetail().messages[0]!,
              content: 'detached update',
            }],
            removes: [],
          },
          reasoning: { upserts: [], removes: [] },
          graph: emptyTraceGraphDelta(3),
          interactions: { upserts: [], removes: [] },
          state: { root: {}, subgraphs: {} },
          status: { execution: 'succeeded', headRunId: RUN_ID },
          completeness: { missingPrefix: false, missingTail: false, payloadOmitted: false },
          messageCount: 1,
          toolCallCount: 0,
          projections: {}, runFailures: [],
        },
      },
    ]))
    const { result } = renderHook(() => useControllerHarness(
      conversation({ runStatus: 'detached', trace: snapshot }),
    ))

    await act(async () => {
      await result.current.controller.followDetachedConversation(THREAD_ID)
    })

    await waitFor(() => {
      const current = result.current.workspace.conversations[0]
      expect(current?.messages[0]?.content).toBe('detached update')
      expect(current?.runStatus).toBe('idle')
      expect(current?.trace?.asOfSeq).toBe(3)
    })
    expect(clientMocks.start).not.toHaveBeenCalled()
  })

  it('refreshes authority and reconnects after a non-terminal Trace EOF', async () => {
    const initial = traceDetail({
      status: { execution: 'running', headRunId: RUN_ID },
      messages: [],
      asOfSeq: 2,
    })
    const refreshed = traceDetail({
      status: { execution: 'running', headRunId: RUN_ID },
      messages: [],
      asOfSeq: 3,
    })
    const terminal = traceDetail({
      asOfSeq: 4,
      messages: [{ ...traceDetail().messages[0]!, content: 'EOF 后终态' }],
    })
    let followCalls = 0
    traceMocks.follow.mockImplementation(() => {
      followCalls += 1
      return followCalls === 1
        ? traceItems([{ type: 'snapshot', snapshot: initial }])
        : traceItems([{
            type: 'update',
            taskTrace: null,
            update: {
              asOfSeq: 4,
              generation: 'generation-test',
              observedAt: '2026-09-05T00:00:00.000001Z',
              events: [],
              facts: [],
              messages: { upserts: terminal.messages, removes: [] },
              reasoning: { upserts: [], removes: [] },
              graph: emptyTraceGraphDelta(4),
              interactions: { upserts: [], removes: [] },
              state: terminal.state,
              status: terminal.status,
              completeness: terminal.completeness,
              messageCount: terminal.messageCount,
              toolCallCount: terminal.toolCallCount,
              projections: {}, runFailures: [],
            },
          }])
    })
    traceMocks.detail
      .mockResolvedValueOnce(refreshed)
      .mockResolvedValueOnce(terminal)
    const { result } = renderHook(() => useControllerHarness(
      conversation({ runStatus: 'detached', trace: initial }),
    ))

    await act(async () => {
      await result.current.controller.followDetachedConversation(THREAD_ID)
    })

    expect(traceMocks.follow).toHaveBeenCalledTimes(2)
    expect(traceMocks.detail).toHaveBeenCalledTimes(2)
    expect(result.current.workspace.conversations[0]?.trace?.asOfSeq).toBe(4)
    expect(result.current.workspace.conversations[0]?.messages[0]?.content).toBe('EOF 后终态')
    expect(result.current.workspace.conversations[0]?.runStatus).toBe('idle')
  })

  it('aborts an existing Trace follower before starting an owned AG-UI run', async () => {
    let traceAborted = false
    traceMocks.follow.mockImplementation(async function* (
      _threadId: string,
      options: { signal: AbortSignal },
    ) {
      await new Promise<void>((resolve) => {
        options.signal.addEventListener('abort', () => {
          traceAborted = true
          resolve()
        }, { once: true })
      })
      if (options.signal.aborted) yield { type: 'error', code: 'trace_unavailable' }
    })
    clientMocks.start.mockImplementation(() => streamItems([
      { seq: 1, event: { type: 'RUN_STARTED', threadId: THREAD_ID, runId: RUN_ID } },
      {
        seq: 2,
        event: {
          type: 'RUN_FINISHED',
          threadId: THREAD_ID,
          runId: RUN_ID,
          outcome: { type: 'success' },
        },
      },
    ]))
    const { result } = renderHook(() => useControllerHarness(
      conversation({ runStatus: 'detached', trace: traceDetail() }),
    ))

    let follower: Promise<void> = Promise.resolve()
    await act(async () => {
      follower = result.current.controller.followDetachedConversation(THREAD_ID)
      await Promise.resolve()
      await result.current.controller.streamRun(THREAD_ID, payload, 'start')
    })
    await follower

    expect(traceAborted).toBe(true)
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


function traceFeed() {
  const pending: Array<{ event: ConversationTraceEvent; consumed: () => void }> = []
  let wake: (() => void) | undefined
  let markOpened!: () => void
  const opened = new Promise<void>((resolve) => { markOpened = resolve })
  let signal: AbortSignal | undefined
  return {
    opened,
    get signal() { return signal },
    push(event: ConversationTraceEvent) {
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

const traceUpdate = (snapshot: ConversationHistoryDetail): ConversationTraceEvent => ({
  type: 'update', taskTrace: snapshot.taskTrace,
  update: {
    asOfSeq: snapshot.asOfSeq, generation: snapshot.generation, observedAt: snapshot.observedAt,
    events: [], facts: [],
    messages: { upserts: snapshot.messages, removes: [] },
    reasoning: { upserts: snapshot.reasoning, removes: [] },
    interactions: { upserts: snapshot.interactions, removes: [] },
    graph: {
      ...emptyTraceGraphDelta(snapshot.asOfSeq),
      turnUpserts: snapshot.graph.turns, nodeUpserts: snapshot.graph.nodes,
      orderedNodeIds: snapshot.graph.orderedNodeIds, matchedNodeIds: snapshot.graph.matchedNodeIds,
    },
    state: snapshot.state, status: snapshot.status, completeness: snapshot.completeness,
    messageCount: snapshot.messageCount, toolCallCount: snapshot.toolCallCount,
    projections: {}, runFailures: snapshot.runFailures,
  },
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

  it('以同一运行的快照和实体更新恢复正文、工具参数和状态，不重放旧投递游标', async () => {
    const snapshot = runningTrace()
    cacheRun()
    const feed = traceFeed()
    traceMocks.follow.mockImplementation((_thread: string, options: { signal: AbortSignal }) => feed.read(options.signal))
    const { result, unmount } = renderHook(() => useControllerHarness({ ...restored(snapshot), lastSeq: 4 }))
    let recovering!: Promise<void>
    try {
      await act(async () => {
        recovering = result.current.controller.recoverConversation(THREAD_ID)
        await feed.opened
      })
      expect(readActiveRunSession(THREAD_ID)).toBeNull()
      expect(result.current.workspace.conversations[0]?.lastSeq).toBeUndefined()
      await act(async () => { await feed.push({ type: 'snapshot', snapshot }) })
      const next = runningTrace(11)
      next.messages[0]!.content = '本月门店营收增长 10%'
      next.graph.nodes[0]!.request = { path: '/月报.md', content: '营收增长 10%' }
      next.graph.nodes[0]!.updatedSeq = 11
      next.state.root = { section: 'summary', totals: [10, 20] }
      await act(async () => { await feed.push(traceUpdate(next)) })
      const current = result.current.workspace.conversations[0]!
      expect(current.messages.filter((message) => message.role === 'assistant').map((message) => message.content)).toEqual(['本月门店营收增长 10%'])
      const tools = current.messages.filter((message) => message.role === 'tool')
      expect(tools).toHaveLength(1)
      expect(JSON.parse(tools[0]!.meta!.params!)).toEqual(next.graph.nodes[0]!.request)
      expect(current.serverState).toEqual({ section: 'summary', totals: [10, 20] })
      expect(current).toMatchObject({ runStatus: 'streaming', activeRunId: RUN_ID })
      expect(current.lastSeq).toBeUndefined()
      expect(clientMocks.start).not.toHaveBeenCalled()
      expect(clientMocks.resume).not.toHaveBeenCalled()
    } finally { unmount(); await recovering }
    expect(feed.signal?.aborted).toBe(true)
    expect(readActiveRunSession(THREAD_ID)).toBeNull()
  })

  it.each(['succeeded', 'cancelled', 'failed', 'unknown', 'waiting'] as const)(
    '停止指向历史已确认的原运行，%s 状态释放停止与恢复记录', async (execution) => {
      const snapshot = runningTrace()
      cacheRun()
      const feed = traceFeed()
      traceMocks.follow.mockImplementation((_thread: string, options: { signal: AbortSignal }) => feed.read(options.signal))
      const terminal = runningTrace(11)
      terminal.status = { execution, headRunId: RUN_ID }
      traceMocks.detail.mockResolvedValue(terminal)
      const { result, unmount } = renderHook(() => useControllerHarness(restored(snapshot)))
      let recovering!: Promise<void>
      try {
        await act(async () => {
          recovering = result.current.controller.recoverConversation(THREAD_ID)
          await feed.opened
        })
        await act(async () => { await feed.push({ type: 'snapshot', snapshot }) })
        await act(async () => {
          expect(await result.current.controller.cancelRun(THREAD_ID)).toBe(true)
          expect(await result.current.controller.cancelRun(THREAD_ID)).toBe(true)
        })
        expect(clientMocks.cancel).toHaveBeenCalledExactlyOnceWith(THREAD_ID, RUN_ID)
        expect(result.current.controller.cancelPendingRunId).toBe(RUN_ID)
        await act(async () => { await feed.push(traceUpdate(terminal)); await recovering })
        expect(result.current.controller.cancelPendingRunId).toBeNull()
        expect(result.current.workspace.conversations[0]?.activeRunId).toBeUndefined()
        expect(readActiveRunSession(THREAD_ID)).toBeNull()
        expect(clientMocks.start).not.toHaveBeenCalled()
        expect(clientMocks.resume).not.toHaveBeenCalled()
        expect(await result.current.controller.cancelRun(THREAD_ID)).toBe(false)
      } finally { unmount(); await recovering }
    },
  )

  it.each(['eof', 'transport', 'protocol'] as const)('Trace %s 保留权威内容并允许重新跟随，不重新提交任务', async (failure) => {
    const snapshot = runningTrace()
    cacheRun()
    traceMocks.detail.mockResolvedValue(snapshot)
    traceMocks.follow.mockImplementation(async function* () {
      yield { type: 'snapshot', snapshot }
      if (failure === 'transport') throw new TypeError('offline')
      if (failure === 'protocol') yield { type: 'error', code: 'trace_unavailable' }
    })
    const { result, unmount } = renderHook(() => useControllerHarness(restored(snapshot)))
    try {
      await act(async () => { await result.current.controller.recoverConversation(THREAD_ID) })
      expect(result.current.workspace.conversations[0]).toMatchObject({ runStatus: 'detached', activeRunId: RUN_ID, notice: { kind: 'error' } })
      expect(result.current.workspace.conversations[0]?.messages[0]?.content).toBe(snapshot.messages[0]!.content)
      expect(traceMocks.follow).toHaveBeenCalledTimes(failure === 'eof' ? 4 : 1)
      expect(readActiveRunSession(THREAD_ID)).toBeNull()
      const terminal = runningTrace(11)
      terminal.status = { execution: 'succeeded', headRunId: RUN_ID }
      traceMocks.follow.mockImplementation(() => traceItems([{ type: 'snapshot', snapshot: terminal }]))
      traceMocks.detail.mockResolvedValue(terminal)
      await act(async () => { await result.current.controller.recoverConversation(THREAD_ID) })
      expect(result.current.workspace.conversations[0]?.runStatus).toBe('idle')
      expect(clientMocks.start).not.toHaveBeenCalled()
      expect(clientMocks.resume).not.toHaveBeenCalled()
    } finally { unmount() }
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

  it.each(['start', 'resume'] as const)('新的 %s 运行终止旧 Trace 跟随并重新使用实时事件', async (mode) => {
    const snapshot = runningTrace()
    cacheRun()
    const feed = traceFeed()
    traceMocks.follow.mockImplementation((_thread: string, options: { signal: AbortSignal }) => feed.read(options.signal))
    const events = eventFeed()
    const nextPayload = { ...payload, runId: 'next-run' }
    clientMocks[mode].mockImplementation((_request: ChatRequestPayload, signal: AbortSignal) => events.read(signal))
    const { result, unmount } = renderHook(() => useControllerHarness(restored(snapshot)))
    let recovering!: Promise<void>
    let streaming: Promise<void> = Promise.resolve()
    try {
      await act(async () => {
        recovering = result.current.controller.recoverConversation(THREAD_ID)
        await feed.opened
      })
      await act(async () => { await feed.push({ type: 'snapshot', snapshot }) })
      await act(async () => {
        streaming = result.current.controller.streamRun(THREAD_ID, nextPayload, mode)
        await recovering
      })
      expect(feed.signal?.aborted).toBe(true)
      await act(async () => {
        events.push({ type: 'RUN_STARTED', threadId: THREAD_ID, runId: nextPayload.runId })
        events.push({ type: 'TEXT_MESSAGE_START', messageId: 'next-answer', role: 'assistant' })
        events.push({ type: 'TEXT_MESSAGE_CONTENT', messageId: 'next-answer', delta: '接下来分析成本' })
        events.push({ type: 'TEXT_MESSAGE_END', messageId: 'next-answer' })
      })
      expect(result.current.workspace.conversations[0]).toMatchObject({ runStatus: 'streaming', activeRunId: nextPayload.runId, lastSeq: 4 })
      expect(result.current.workspace.conversations[0]?.messages.find((message) => message.id === 'next-answer')?.content).toBe('接下来分析成本')
      expect(clientMocks[mode]).toHaveBeenCalledExactlyOnceWith(nextPayload, expect.any(AbortSignal), undefined)
      expect(readActiveRunSession(THREAD_ID)?.payload.runId).toBe(nextPayload.runId)
      expect(traceMocks.follow).toHaveBeenCalledOnce()
    } finally { unmount(); await recovering; await streaming }
  })

  it('切换会话后 A 的历史更新不覆盖 B，停止只影响所选运行', async () => {
    const snapshotA = runningTrace()
    const snapshotB = traceDetail({ threadId: 'thread-b', headRunId: 'run-b', status: { execution: 'running', headRunId: 'run-b' } })
    cacheRun()
    const feed = traceFeed()
    traceMocks.follow.mockImplementation((_thread: string, options: { signal: AbortSignal }) => feed.read(options.signal))
    const { result, unmount } = renderHook(() => useControllerHarness(restored(snapshotA)))
    let recovering!: Promise<void>
    try {
      await act(async () => {
        recovering = result.current.controller.recoverConversation(THREAD_ID)
        await feed.opened
      })
      await act(async () => { await feed.push({ type: 'snapshot', snapshot: snapshotA }) })
      act(() => result.current.setWorkspace((state) => ({ ...state, currentThreadId: 'thread-b', conversations: [...state.conversations, restored(snapshotB)] })))
      const next = runningTrace(11)
      next.messages[0]!.content = 'A 的营收分析已更新'
      await act(async () => { await feed.push(traceUpdate(next)) })
      expect(result.current.workspace.currentThreadId).toBe('thread-b')
      expect(result.current.workspace.conversations.find((item) => item.threadId === 'thread-b')?.messages[0]?.content).toBe('Trace 最终内容')
      expect(result.current.workspace.conversations.find((item) => item.threadId === THREAD_ID)?.messages[0]?.content).toBe('A 的营收分析已更新')
      await act(async () => { await result.current.controller.cancelRun('thread-b') })
      expect(clientMocks.cancel).toHaveBeenCalledExactlyOnceWith('thread-b', 'run-b')
      expect(feed.signal?.aborted).toBe(false)
      expect(result.current.controller.cancelPendingRunId).toBe('run-b')
      act(() => result.current.setWorkspace((state) => ({ ...state, currentThreadId: THREAD_ID })))
      expect(result.current.controller.cancelPendingRunId).toBeNull()
    } finally { unmount(); await recovering }
  })
  it.each(['snapshot', 'update'] as const)('Trace %s 关联错误保留已展示内容并报告恢复失败', async (eventType) => {
    const snapshot = runningTrace()
    cacheRun()
    const conflicting = { ...runningTrace(11), generation: 'unrelated-generation' }
    traceMocks.follow.mockImplementation(() => traceItems([eventType === 'snapshot' ? { type: 'snapshot', snapshot: conflicting } : traceUpdate(conflicting)]))
    const { result, unmount } = renderHook(() => useControllerHarness(restored(snapshot)))
    try {
      await act(async () => { await result.current.controller.recoverConversation(THREAD_ID) })
      expect(result.current.workspace.conversations[0]).toMatchObject({ runStatus: 'detached', notice: { kind: 'error' } })
      expect(result.current.workspace.conversations[0]?.messages[0]?.content).toBe(snapshot.messages[0]!.content)
      expect(clientMocks.start).not.toHaveBeenCalled()
    } finally { unmount() }
  })

  it('历史审批结束跟随后，以新的运行和原审批内容继续实时执行', async () => {
    const snapshot = runningTrace()
    const waiting = runningTrace(11)
    waiting.status = { execution: 'waiting', headRunId: RUN_ID }
    waiting.interactions = [{
      id: 'review', sourceId: 'native-review', runId: RUN_ID, traceSeq: 11,
      kind: 'tool_review', graphNamespace: [], toolCallIds: ['native-write'],
      status: 'pending', payloadOmitted: false, openedAt: BASE_TIME,
      agui: toolReviewInterrupts('native-review', [{ toolCallId: 'write-report', args: { path: '/月报.md' } }]),
    }]
    cacheRun()
    traceMocks.follow.mockImplementation(() => traceItems([{ type: 'snapshot', snapshot: waiting }]))
    traceMocks.detail.mockResolvedValue(waiting)
    const events = eventFeed()
    clientMocks.resume.mockImplementation((_request: ChatRequestPayload, signal: AbortSignal) => events.read(signal))
    const { result, unmount } = renderHook(() => useControllerHarness(restored(snapshot)))
    let streaming: Promise<void> = Promise.resolve()
    try {
      await act(async () => { await result.current.controller.recoverConversation(THREAD_ID) })
      expect(result.current.workspace.conversations[0]).toMatchObject({
        runStatus: 'waiting_approval', pendingInteractionKind: 'tool_approval',
        approval: { items: [{ interruptId: 'native-review', toolCallId: 'write-report' }] },
      })
      const nextPayload: ChatRequestPayload = {
        ...payload, runId: 'approved-run', parentRunId: RUN_ID,
        resume: [{ interruptId: 'native-review', status: 'resolved', payload: { type: 'approve' } }],
      }
      act(() => result.current.setWorkspace((state) => ({
        ...state, conversations: state.conversations.map((item) => prepareResumeSubmission(item)),
      })))
      await act(async () => { streaming = result.current.controller.streamRun(THREAD_ID, nextPayload, 'resume') })
      await act(async () => {
        events.push({ type: 'RUN_STARTED', threadId: THREAD_ID, runId: nextPayload.runId })
        events.push({ type: 'TOOL_CALL_RESULT', toolCallId: 'write-report', messageId: 'written-result', content: '月报已保存', role: 'tool' })
        events.push({ type: 'STATE_SNAPSHOT', snapshot: { report: '/月报.md', delivered: true } })
      })
      const current = result.current.workspace.conversations[0]!
      expect(current).toMatchObject({ runStatus: 'streaming', activeRunId: 'approved-run', serverState: { report: '/月报.md', delivered: true } })
      const tools = current.messages.filter((message) => message.meta?.toolCallId === 'write-report')
      expect(tools).toHaveLength(1)
      expect(tools[0]?.meta).toMatchObject({ result: '月报已保存', status: 'completed' })
      expect(clientMocks.resume).toHaveBeenCalledExactlyOnceWith(nextPayload, expect.any(AbortSignal), undefined)
      expect(clientMocks.start).not.toHaveBeenCalled()
      expect(traceMocks.follow).toHaveBeenCalledOnce()
    } finally { unmount(); await streaming }
  })

  it('旧跟随的历史读取在新运行接管时失效，晚到结果不覆盖当前内容', async () => {
    const snapshot = runningTrace()
    const terminal = runningTrace(11)
    terminal.status = { execution: 'succeeded', headRunId: RUN_ID }
    traceMocks.follow.mockImplementation(() => traceItems([{ type: 'snapshot', snapshot: terminal }]))
    let releaseHistory!: (detail: ConversationHistoryDetail) => void
    const lateHistory = new Promise<ConversationHistoryDetail>((resolve) => { releaseHistory = resolve })
    let markRequested!: () => void
    const requested = new Promise<void>((resolve) => { markRequested = resolve })
    let markAborted!: () => void
    const aborted = new Promise<void>((resolve) => { markAborted = resolve })
    traceMocks.detail.mockImplementation((_thread: string, options: { signal: AbortSignal }) => {
      options.signal.addEventListener('abort', markAborted, { once: true })
      markRequested()
      return lateHistory
    })
    const events = eventFeed()
    clientMocks.start.mockImplementation((_request: ChatRequestPayload, signal: AbortSignal) => events.read(signal))
    const nextPayload = { ...payload, runId: 'next-run' }
    const { result, unmount } = renderHook(() => useControllerHarness(restored(snapshot)))
    let recovering: Promise<void> = Promise.resolve()
    let streaming: Promise<void> = Promise.resolve()
    try {
      await act(async () => {
        recovering = result.current.controller.recoverConversation(THREAD_ID)
        await requested
      })
      await act(async () => {
        streaming = result.current.controller.streamRun(THREAD_ID, nextPayload, 'start')
        await aborted
        const stale = runningTrace(12)
        stale.messages[0]!.content = '已失效的响应内容'
        releaseHistory(stale)
        await recovering
      })
      await act(async () => {
        events.push({ type: 'RUN_STARTED', threadId: THREAD_ID, runId: nextPayload.runId })
        events.push({ type: 'TEXT_MESSAGE_START', messageId: 'current-answer', role: 'assistant' })
        events.push({ type: 'TEXT_MESSAGE_CONTENT', messageId: 'current-answer', delta: '新的营收问题' })
        events.push({ type: 'TEXT_MESSAGE_END', messageId: 'current-answer' })
      })
      const current = result.current.workspace.conversations[0]!
      expect(current).toMatchObject({ activeRunId: nextPayload.runId, runStatus: 'streaming' })
      expect(current.messages.map((message) => message.content)).toContain('新的营收问题')
      expect(current.messages.map((message) => message.content)).not.toContain('已失效的响应内容')
      expect(current.notice).toBeUndefined()
      expect(clientMocks.start).toHaveBeenCalledExactlyOnceWith(nextPayload, expect.any(AbortSignal), undefined)
    } finally {
      releaseHistory(terminal)
      unmount()
      await recovering
      await streaming
    }
  })

  it('连续实体更新按完整顺序合并，保留其他消息和用户较新的标题', async () => {
    const snapshot = runningTrace()
    const first = runningTrace(11)
    first.messages.push({ ...first.messages[0]!, id: 'second-message', sourceId: 'second-native', agui: { kind: 'message', messageId: 'second-answer' }, content: '门店成本分析', traceSeq: 11 })
    first.messageCount = 2
    const second = runningTrace(12)
    second.messages[0]!.content = '门店营收分析已完成'
    second.messageCount = 2
    const feed = traceFeed()
    traceMocks.follow.mockImplementation((_thread: string, options: { signal: AbortSignal }) => feed.read(options.signal))
    const { result, unmount } = renderHook(() => useControllerHarness(restored(snapshot)))
    let recovering!: Promise<void>
    try {
      await act(async () => {
        recovering = result.current.controller.recoverConversation(THREAD_ID)
        await feed.opened
      })
      act(() => result.current.setWorkspace((state) => ({
        ...state,
        conversations: state.conversations.map((item) => ({ ...item, title: '九月门店经营复盘', titleSource: 'user', titleSeq: 99 })),
      })))
      await act(async () => {
        await Promise.all([
          feed.push({ type: 'snapshot', snapshot }),
          feed.push(traceUpdate(first)),
          feed.push(traceUpdate(second)),
        ])
      })
      const current = result.current.workspace.conversations[0]!
      expect(current.messages.filter((message) => message.role === 'assistant').map((message) => message.content)).toEqual(['门店营收分析已完成', '门店成本分析'])
      expect(current.trace?.messageCount).toBe(2)
      expect(current.trace?.asOfSeq).toBe(12)
      expect(current.title).toBe('九月门店经营复盘')
      expect(current.titleSeq).toBe(99)
      expect(current.notice).toBeUndefined()
    } finally { unmount(); await recovering }
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
