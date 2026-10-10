import { act, renderHook } from '@testing-library/react'
import { useLayoutEffect, useRef, useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { StreamedAgUiEvent } from '../../../api/conversation/client'
import type {
  ConversationHistoryDetail,
  followConversationRun,
} from '../../../api/conversation/history'
import type { ChatRequestPayload } from '../../../api/conversation/types'
import { ApiError } from '../../../api/shared/http'
import { clearAuthSession, saveAuthSession } from '../../../auth/session'
import { updateConversation } from '../../../lib/workspace'
import { toolReviewInterrupts } from '../../../test/aguiFixtures'
import { testAuthSession } from '../../../test/authSession'
import { mockResourceNotices } from '../../../test/resourceNotices'
import { emptyTraceGraph, traceGraphNode, traceGraphWithNodes } from '../../../test/traceFixtures'
import type { Conversation } from '../../../types'
import { useWorkspaceState } from '../../workspace/useWorkspaceState'
import { restoreConversationFromTrace } from '../trace/runtime'
import { useInteractionConfirmation } from '../useInteractionConfirmation'
import { readActiveRunSession, readActiveRunSessions } from './activeRunSession'
import { useConversationStreamController } from './useConversationStreamController'

const clientMocks = vi.hoisted(() => ({
  start: vi.fn(),
  compact: vi.fn(),
  resume: vi.fn(),
  cancel: vi.fn(),
}))
const traceMocks = vi.hoisted(() => ({
  detail: vi.fn(),
  follow: vi.fn(),
}))

vi.mock('../../../api/conversation/client', () => ({
  startConversationRun: clientMocks.start,
  compactConversationContext: clientMocks.compact,
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

beforeEach(() => saveAuthSession(testAuthSession))
afterEach(() => clearAuthSession())

const payload: ChatRequestPayload = {
  threadId: THREAD_ID,
  runId: RUN_ID,
  state: {},
  messages: [],
  tools: [],
  context: [],
  forwardedProps: {projectId: 'project-1',  skillIds: [], accessMode: 'write_approval', model: 'main', command: { plan: 'off' } },
}

const traceDetail = (
  overrides: Partial<ConversationHistoryDetail> = {},
): ConversationHistoryDetail => ({projectId: 'project-1', archived: false,  accessMode: 'write_approval',
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
  submissionResult: null, planResults: [], interactionAvailability: [], interactions: [],
  status: { execution: 'succeeded', headRunId: RUN_ID },
  completeness: { missingPrefix: false, missingTail: false, payloadOmitted: false },
  createdAt: BASE_TIME,
  updatedAt: BASE_TIME,
  ...overrides,
  graph: overrides.graph ?? emptyTraceGraph(overrides.asOfSeq ?? 5),
  taskTrace: overrides.taskTrace ?? { status: 'ready', todoGroups: [] },
})

const conversation = (overrides: Partial<Conversation> = {}): Conversation => ({projectId: 'project-1', archived: false,  accessMode: 'write_approval',
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
  historySynchronized: false,
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
  items: LiveEvent[],
): AsyncGenerator<LiveEvent> {
  for (const item of items) yield item
}

function useControllerHarness(initialConversation: Conversation, onNotice?: (notice: NonNullable<Conversation['notice']>) => void) {
  const { workspace, setWorkspace, retainConversationDetails, acknowledgeComposerPreferences, setComposerPreference } = useWorkspaceState()
  const initial = useRef(initialConversation).current
  useLayoutEffect(() => {
    setWorkspace({ conversations: [initial], currentThreadId: initial.threadId })
  }, [initial, setWorkspace])
  const [draftConversation, setDraftConversation] = useState<Conversation | null>(null)
  const controller = useConversationStreamController({projectId: 'project-1',
    workspace,
    setWorkspace,
    setDraftConversation,
    retainConversationDetails,
    acknowledgeComposerPreferences,
    onNotice,
  })
  return { controller, workspace, draftConversation, setWorkspace, setComposerPreference }
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

  it('等待旧订阅停止期间卸载，排队提交完成但不发出请求', async () => {
    const hook = renderHook(() => useControllerHarness(conversation()))
    let running!: Promise<void>
    act(() => { running = hook.result.current.controller.streamRun(THREAD_ID, payload, 'start') })
    hook.unmount()
    await act(async () => { await running })
    expect(clientMocks.start).not.toHaveBeenCalled()
    expect(readActiveRunSessions()).toEqual([])
  })

  it('preserves an unconfirmed submission and retries the same run only on explicit recovery', async () => {
    vi.useFakeTimers()
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
    expect(readActiveRunSession(THREAD_ID, 'project-1')?.payload).toEqual(payload)
    await act(async () => { await result.current.controller.followDetachedConversation(THREAD_ID) })
    await act(async () => { await vi.runOnlyPendingTimersAsync() })
    expect(traceMocks.follow).not.toHaveBeenCalled()
    expect(clientMocks.cancel).not.toHaveBeenCalled()
    expect(clientMocks.start).toHaveBeenCalledOnce()
    clientMocks.start.mockImplementation(() => streamItems([
      { event: { type: 'RUN_STARTED', threadId: THREAD_ID, runId: RUN_ID }, seq: 1 },
      { event: { type: 'RUN_FINISHED', threadId: THREAD_ID, runId: RUN_ID, outcome: { type: 'success' } }, seq: 2 },
    ]))
    await act(async () => { await result.current.controller.recoverConversation(THREAD_ID) })
    expect(clientMocks.start).toHaveBeenLastCalledWith(payload, expect.any(AbortSignal), 0)
    expect(readActiveRunSession(THREAD_ID, 'project-1')).toBeNull()
    expect(clientMocks.cancel).not.toHaveBeenCalled()
  })

  it('准备失败且历史暂不可用时保留审批输入，显式恢复只查询保存状态', async () => {
    const action = toolReviewInterrupts('review-write', [{ toolCallId: 'call-write', args: { file_path: '/notes.txt' } }])[0]
    const initial = conversation({
      approval: { activeIndex: 0, submitted: true, submissionRunId: RUN_ID, mode: 'reject', items: [{
        id: action.id, interruptId: action.id, toolCallId: 'call-write', toolName: 'write_file',
        params: '{}', input: '/notes.txt', description: '写入文件', originalArgs: { file_path: '/notes.txt' },
        allowedDecisions: ['approve', 'reject'], decision: 'rejected', rejectionReason: '保留笔记',
      }] },
    })
    clientMocks.resume.mockImplementation(() => streamItems([
      { seq: 1, event: { type: 'RUN_STARTED', threadId: THREAD_ID, runId: RUN_ID } },
      { seq: 2, event: { type: 'RUN_ERROR', code: 'runtime_initialization_error', message: '准备失败', rawEvent: { runId: RUN_ID } } },
    ]))
    traceMocks.detail.mockRejectedValue(new ApiError('unavailable', { status: 503 }))
    const { result } = renderHook(() => useControllerHarness(initial))
    await act(async () => { await result.current.controller.streamRun(THREAD_ID, {
      ...payload, resume: [{ interruptId: action.id, status: 'resolved', payload: { type: 'reject', message: '保留笔记' } }],
    }, 'resume') })
    expect(result.current.workspace.conversations[0]?.approval).toEqual(initial.approval)
    expect(result.current.workspace.conversations[0]?.notice?.recovery).toBe('history')
    const authority = traceDetail({
      status: { execution: 'waiting', headRunId: RUN_ID },
      interactions: [{ id: 'review', traceSeq: 3, sourceId: action.id, graphNamespace: [], runId: 'paused-run',
        kind: 'tool_approval', toolCallIds: ['call-write'], status: 'pending', payloadOmitted: false, openedAt: BASE_TIME, agui: [action] }],
      submissionResult: null, planResults: [], interactionAvailability: [{ interruptId: action.id, state: 'confirming', submissionRunId: RUN_ID }],
    })
    traceMocks.detail.mockResolvedValue(authority)
    await act(async () => { await result.current.controller.recoverConversation(THREAD_ID) })
    expect(result.current.workspace.conversations[0]?.approval).toEqual(initial.approval)
    traceMocks.detail.mockResolvedValue({ ...authority,
      interactionAvailability: [{ interruptId: action.id, state: 'available', submissionRunId: null }],
      submissionResult: { submissionRunId: RUN_ID, interruptIds: [action.id], state: 'not_saved' },
    })
    await act(async () => { await result.current.controller.recoverConversation(THREAD_ID) })
    expect(result.current.workspace.conversations[0]?.approval).toMatchObject({ submitted: false, items: initial.approval!.items })
    expect(clientMocks.resume).toHaveBeenCalledOnce()
    expect(clientMocks.cancel).not.toHaveBeenCalled()
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
    let markStarted!: () => void
    const started = new Promise<void>(resolve => { markStarted = resolve })
    clientMocks.start.mockImplementation(async function* () {
      yield { seq: 1, event: { type: 'RUN_STARTED', threadId: THREAD_ID, runId: RUN_ID } }
      markStarted()
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
      await started
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
})


function eventFeed() {
  const pending: Array<{ item: StreamedAgUiEvent; consumed: () => void }> = []
  let wake: (() => void) | undefined
  let seq = 0
  let markOpened!: () => void
  const opened = new Promise<void>(resolve => { markOpened = resolve })
  return {
    opened,
    push(event: StreamedAgUiEvent['event']) {
      const consumed = new Promise<void>(resolve => {
        pending.push({ item: { event, seq: ++seq }, consumed: resolve })
      })
      wake?.()
      return consumed
    },
    async *read(signal: AbortSignal) {
      const onAbort = () => wake?.()
      signal.addEventListener('abort', onAbort)
      markOpened()
      try {
        while (!signal.aborted) {
          const next = pending.shift()
          if (next) {
            try { yield next.item } finally { next.consumed() }
            if (next.item.event.type === 'RUN_FINISHED') return
          } else {
            await new Promise<void>((resolve) => { wake = resolve })
          }
        }
      } finally {
        signal.removeEventListener('abort', onAbort)
        pending.splice(0).forEach(item => item.consumed())
      }
    },
  }
}

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


describe('已受理运行的历史恢复', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    window.sessionStorage.clear()
    clientMocks.cancel.mockResolvedValue({ cancelled: true })
  })

  afterEach(() => { vi.useRealTimers() })

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

it.each(['resume', 'follow', 'recover'] as const)('独立确认解锁后编辑的审批输入不被迟到历史覆盖：%s', async mode => {
  vi.clearAllMocks()
  vi.useFakeTimers()
  window.sessionStorage.clear()
  mockResourceNotices()
  const action = toolReviewInterrupts('review-write', [{ toolCallId: 'call-write', args: { file_path: '/notes.txt' } }])[0]
  const initial = conversation({ runStatus: mode === 'follow' ? 'detached' : 'streaming',
    notice: mode === 'recover' ? { kind: 'error', content: '历史同步失败', recovery: 'history' } : undefined,
    approval: { activeIndex: 0, submitted: true, submissionRunId: RUN_ID, mode: 'reject', items: [{
      id: action.id, interruptId: action.id, toolCallId: 'call-write', toolName: 'write_file', params: '{}', input: '/notes.txt',
      description: '写入文件', originalArgs: { file_path: '/notes.txt' }, allowedDecisions: ['reject'], decision: 'rejected', rejectionReason: '提交 A 的旧原因',
    }] },
  })
  const authority = traceDetail({
    status: { execution: 'waiting', headRunId: RUN_ID },
    interactions: [{ id: 'review', traceSeq: 3, sourceId: action.id, graphNamespace: [], runId: 'origin', kind: 'tool_approval',
      toolCallIds: ['call-write'], status: 'pending', payloadOmitted: false, openedAt: BASE_TIME, agui: [action] }],
    interactionAvailability: [{ interruptId: action.id, state: 'available', submissionRunId: null }],
    submissionResult: { submissionRunId: RUN_ID, interruptIds: [action.id], state: 'not_saved' },
  })
  let releaseTerminal!: (value: ConversationHistoryDetail) => void
  let releaseConfirmation!: (value: ConversationHistoryDetail) => void
  let terminalRequested!: () => void
  const terminalRequest = new Promise<void>(resolve => { terminalRequested = resolve })
  const terminal = new Promise<ConversationHistoryDetail>(resolve => { releaseTerminal = resolve })
  const confirmation = new Promise<ConversationHistoryDetail>(resolve => { releaseConfirmation = resolve })
  traceMocks.detail.mockImplementation((_threadId: string, options: { includeTaskTrace: boolean }) => {
    if (options.includeTaskTrace) { terminalRequested(); return terminal }
    return confirmation
  })
  const events: StreamedAgUiEvent[] = [
    { seq: 1, event: { type: 'RUN_STARTED', threadId: THREAD_ID, runId: RUN_ID } },
    { seq: 2, event: { type: 'RUN_ERROR', code: 'runtime_initialization_error', message: '准备失败', rawEvent: { runId: RUN_ID } } },
  ]
  clientMocks.resume.mockImplementation(() => streamItems(events))
  traceMocks.follow.mockImplementation(async function* () {
    yield { type: 'snapshot', replay: true, snapshot: { ...authority, asOfSeq: 4, graph: emptyTraceGraph(4), submissionResult: null,
      interactionAvailability: [{ interruptId: action.id, state: 'confirming', submissionRunId: RUN_ID }],
      status: { execution: 'running', headRunId: RUN_ID },
    } }
    for (const item of events) yield { type: 'event', replayed: false, ...item }
  })
  const hook = renderHook(() => {
    const harness = useControllerHarness(initial)
    useInteractionConfirmation(harness.workspace.conversations[0] ?? initial, (threadId, updater) => {
      harness.setWorkspace(current => updateConversation(current, threadId, updater))
    })
    return harness
  })
  let running!: Promise<void>
  try {
    await act(async () => {
      if (mode !== 'resume') await hook.result.current.controller.handoffTaskTraceFollow(THREAD_ID)
      running = mode === 'resume'
        ? hook.result.current.controller.streamRun(THREAD_ID, payload, 'resume', { target: 'workspace' })
        : mode === 'follow' ? hook.result.current.controller.followDetachedConversation(THREAD_ID)
          : hook.result.current.controller.recoverConversation(THREAD_ID)
      await terminalRequest
    })
    await act(async () => releaseConfirmation({ ...authority, taskTrace: null }))
    expect(hook.result.current.workspace.conversations[0]?.approval?.submitted).toBe(false)
    if (mode !== 'recover') expect(hook.result.current.workspace.conversations[0]?.notice?.recovery).not.toBe('history')
    await act(async () => {
      hook.result.current.setWorkspace(current => updateConversation(current, THREAD_ID, item => ({ ...item,
        approval: item.approval ? { ...item.approval, error: undefined, items: item.approval.items.map(entry => ({ ...entry, rejectionReason: '用户解锁后刚编辑的新原因' })) } : undefined,
      })))
      releaseTerminal(authority)
      await running
    })
    expect(hook.result.current.workspace.conversations[0]?.approval?.items[0].rejectionReason).toBe('用户解锁后刚编辑的新原因')
    expect(hook.result.current.workspace.conversations[0]?.approval?.submitted).toBe(false)
    expect(hook.result.current.workspace.conversations[0]?.notice?.recovery).not.toBe('history')
    expect(hook.result.current.workspace.conversations[0]?.lastSeq).toBe(mode === 'recover' ? 0 : 2)
  } finally {
    releaseConfirmation({ ...authority, taskTrace: null })
    releaseTerminal(authority)
    hook.unmount()
    if (running) await running
    vi.restoreAllMocks()
    vi.useRealTimers()
  }
})
