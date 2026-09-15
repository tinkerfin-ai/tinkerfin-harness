import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type Dispatch,
  type SetStateAction,
} from 'react'

import {
  cancelConversationRun,
  resumeConversationRun,
  startConversationRun,
} from '../../../api/conversation/client'
import { ApiError } from '../../../api/shared/http'
import {
  ConversationError,
  conversationErrorMessage,
  hasConversationErrorCode,
} from '../../../api/conversation/errors'
import {
  fetchConversationHistoryDetail,
  followConversationRun,
} from '../../../api/conversation/history'
import type { ChatRequestPayload } from '../../../api/conversation/types'
import type { TaskTraceSnapshot } from '../../../api/conversation/taskTrace'
import { translateCurrent } from '../../../i18n'
import type {
  Conversation,
  WebTaskTraceViewState,
  WorkspaceState,
} from '../../../types'
import {
  applyConversationEvent,
  markConversationDetached,
} from '../agui'
import {
  restoreConversationFromTrace,
} from '../trace/runtime'
import { LiveTodoTraceProjector } from '../todoTrace/liveProjection'
import { TaskTraceFollowOwnership } from '../todoTrace/followOwnership'
import { InvalidStateDeltaError } from '../agui/jsonPatch'
import {
  mergeConversationTitle,
  selectCurrentConversation,
  updateConversation,
  upsertConversation,
} from '../../../lib/workspace'
import {
  clearActiveRunSession,
  readActiveRunSession,
  writeActiveRunSession,
  type ActiveRunSession,
} from './activeRunSession'

const RECONNECT_MAX_DELAY_MS = 5000
const RECONNECT_LIMIT = 3
const ACTIVE_RUN_PERSIST_INTERVAL_MS = 250
const TEXT_RENDER_INTERVAL_MS = 50
const DETACHED_TRACE_RECONNECT_LIMIT = 3

const isTransportFailure = (error: unknown) => error instanceof TypeError
  || (error instanceof ApiError && (error.status === 0 || error.status >= 500))

const historySyncNotice = (id: string): NonNullable<Conversation['notice']> => ({
  id,
  kind: 'error',
  content: translateCurrent('历史同步失败，请重新加载'),
  recovery: 'history',
})

const taskTraceView = (snapshot: TaskTraceSnapshot): WebTaskTraceViewState => (
  snapshot.status === 'ready'
    ? { phase: 'ready', snapshot }
    : { phase: 'unavailable', snapshot }
)

const latestUserTurn = (conversation: Conversation | undefined) => {
  let message: Conversation['messages'][number] | undefined
  for (let index = (conversation?.messages.length ?? 0) - 1; index >= 0; index -= 1) {
    const candidate = conversation?.messages[index]
    if (candidate?.role !== 'user') continue
    message = candidate
    break
  }
  const runId = message?.meta?.runId
  return message && runId
    ? {
        runId,
        userMessageId: message.id,
        userMessagePreview: message.content.trim()
          ? message.content
          : (message.attachments ?? []).map(attachment => attachment.name).join(', '),
      }
    : undefined
}

const waitForReconnect = (delay: number, signal: AbortSignal): Promise<void> => (
  new Promise((resolve) => {
    if (signal.aborted) {
      resolve()
      return
    }
    const timer = window.setTimeout(() => {
      signal.removeEventListener('abort', onAbort)
      resolve()
    }, delay)
    const onAbort = () => {
      window.clearTimeout(timer)
      resolve()
    }
    signal.addEventListener('abort', onAbort, { once: true })
  })
)

export interface StreamRunOptions {
  onAccepted?: () => void
  /** 请求未被接受且服务端明确拒绝时，恢复仍属于该次提交的输入 */
  onRequestRejected?: () => void
  target: 'draft' | 'workspace'
  initialConversation?: Conversation
  initialAfterSeq?: number
}

interface ConversationStreamControllerOptions {
  workspace: WorkspaceState
  setWorkspace: Dispatch<SetStateAction<WorkspaceState>>
  setDraftConversation: Dispatch<SetStateAction<Conversation | null>>
  onNotice?: (notice: NonNullable<Conversation['notice']>) => void
}

export interface ConversationStreamController {
  streamRun: (
    threadId: string,
    payload: ChatRequestPayload,
    mode: 'start' | 'resume',
    options?: StreamRunOptions,
  ) => Promise<void>
  followDetachedConversation: (threadId: string) => Promise<void>
  recoverConversation: (threadId: string) => Promise<void>
  handoffTaskTraceFollow: (threadId: string) => Promise<void>
  releaseDraft: () => void
  cancelRun: (threadId: string) => Promise<boolean>
  cancelPendingRunId: string | null
  hasActiveStream: () => boolean
  isActiveThread: (threadId: string) => boolean
}

/**
 * 管理会话实时连接和持久化事件序号状态
 *
 * 页面提供 React 状态边界；控制器负责流取消、epoch、权威线程切换、
 * 序号去重、缺口恢复、断连回放和动画帧合并
 */
export function useConversationStreamController({
  workspace,
  setWorkspace,
  setDraftConversation,
  onNotice,
}: ConversationStreamControllerOptions): ConversationStreamController {
  const latestNoticeHandler = useRef(onNotice)
  latestNoticeHandler.current = onNotice
  const streams = useRef(new Map<number, {
    threadId: string
    runId: string
    controller: AbortController
    receiving: boolean
    superseded: boolean
    todoProjector: LiveTodoTraceProjector | null
  }>())
  const isActiveThread = useCallback((threadId: string) => (
    [...streams.current.values()].some((stream) => stream.threadId === threadId && stream.receiving)
  ), [])
  const recoveryPending = useRef(new Set<string>())
  const recoveryRequests = useRef(new Map<string, {
    threadId: string
    payload: ChatRequestPayload
    mode: 'start' | 'resume'
    options: StreamRunOptions
    receivedEvent: boolean
  }>())
  const cancelRequests = useRef(new Map<string, Promise<boolean>>())
  const [cancelPendingRunIds, setCancelPendingRunIds] = useState<ReadonlySet<string>>(new Set())
  const cancelPending = useRef(new Set<string>())
  const activeStreamEpoch = useRef(0)
  const draftStreamEpoch = useRef<number | null>(null)
  const liveEpochs = useRef(new Set<number>())
  const workspaceUpdateTimer = useRef<number | null>(null)
  const workspaceFrameUpdates = useRef<Array<{
    epoch: number
    update: (state: WorkspaceState) => WorkspaceState
  }>>([])
  const draftUpdateTimer = useRef<number | null>(null)
  const pendingDraftFrameValue = useRef<{
    epoch: number
    value: Conversation | null
  } | undefined>(undefined)
  const latestWorkspace = useRef(workspace)
  const taskTraceFollowOwnership = useRef(new TaskTraceFollowOwnership())
  const delayedTraceFollowTimers = useRef(new Set<number>())
  const activeRunPersistence = useRef(new Map<string, { session: ActiveRunSession; timer: number | null }>())
  const isMounted = useRef(true)
  latestWorkspace.current = workspace

  const clearCancelPending = useCallback((runId: string) => {
    cancelPending.current.delete(runId)
    if (isMounted.current) setCancelPendingRunIds(new Set(cancelPending.current))
  }, [])

  const flushActiveRunPersistence = useCallback((runId?: string) => {
    for (const [key, owner] of activeRunPersistence.current) {
      if (runId && key !== runId) continue
      if (owner.timer != null) window.clearTimeout(owner.timer)
      owner.timer = null
      writeActiveRunSession(owner.session)
    }
  }, [])

  const scheduleActiveRunPersistence = useCallback((session: ActiveRunSession, immediate = false) => {
    const runId = session.payload.runId
    let owner = activeRunPersistence.current.get(runId)
    if (!owner) {
      owner = { session, timer: null }
      activeRunPersistence.current.set(runId, owner)
    }
    owner.session = session
    if (immediate) { flushActiveRunPersistence(runId); return }
    if (owner.timer != null) return
    owner.timer = window.setTimeout(() => flushActiveRunPersistence(runId), ACTIVE_RUN_PERSIST_INTERVAL_MS)
  }, [flushActiveRunPersistence])

  const clearActiveRunPersistence = useCallback((runId: string) => {
    const owner = activeRunPersistence.current.get(runId)
    if (owner?.timer != null) window.clearTimeout(owner.timer)
    activeRunPersistence.current.delete(runId)
    clearActiveRunSession(runId)
  }, [])

  const flushWorkspaceUpdates = useCallback(() => {
    if (workspaceUpdateTimer.current != null) {
      window.clearTimeout(workspaceUpdateTimer.current)
      workspaceUpdateTimer.current = null
    }
    const updates = workspaceFrameUpdates.current
      .splice(0)
      .filter((entry) => liveEpochs.current.has(entry.epoch))
    if (updates.length === 0) return
    setWorkspace((state) => updates.reduce((next, entry) => entry.update(next), state))
  }, [setWorkspace])

  const enqueueWorkspaceUpdate = useCallback((
    epoch: number,
    updater: (state: WorkspaceState) => WorkspaceState,
    deferTextRender = false,
  ) => {
    workspaceFrameUpdates.current.push({ epoch, update: updater })
    if (!deferTextRender) {
      flushWorkspaceUpdates()
      return
    }
    if (workspaceUpdateTimer.current != null) return
    workspaceUpdateTimer.current = window.setTimeout(
      flushWorkspaceUpdates,
      TEXT_RENDER_INTERVAL_MS,
    )
  }, [flushWorkspaceUpdates])

  const flushDraftUpdate = useCallback(() => {
    if (draftUpdateTimer.current != null) {
      window.clearTimeout(draftUpdateTimer.current)
      draftUpdateTimer.current = null
    }
    const pending = pendingDraftFrameValue.current
    pendingDraftFrameValue.current = undefined
    if (pending && pending.epoch === draftStreamEpoch.current) {
      setDraftConversation(pending.value)
    }
  }, [setDraftConversation])

  const enqueueDraftUpdate = useCallback((
    epoch: number,
    value: Conversation | null,
    deferTextRender = false,
  ) => {
    pendingDraftFrameValue.current = { epoch, value }
    if (!deferTextRender) {
      flushDraftUpdate()
      return
    }
    if (draftUpdateTimer.current != null) return
    draftUpdateTimer.current = window.setTimeout(
      flushDraftUpdate,
      TEXT_RENDER_INTERVAL_MS,
    )
  }, [flushDraftUpdate])

  const releaseDraft = useCallback(() => {
    draftStreamEpoch.current = null
    pendingDraftFrameValue.current = undefined
  }, [])

  const followDetachedConversation = useCallback(async (threadId: string) => {
    if (!isMounted.current || isActiveThread(threadId)) return
    // 首响应丢失时，旧 head 的 Trace 不能证明这次提交是否被受理
    if (recoveryRequests.current.get(threadId)?.receivedEvent === false) return
    const target = latestWorkspace.current.conversations.find((item) => item.threadId === threadId)
    if (!target?.isHydrated || (target.runStatus !== 'detached' && target.runStatus !== 'streaming')) return
    const runId = target.activeRunId ?? target.trace?.headRunId
    if (!runId) return
    await taskTraceFollowOwnership.current.follow(threadId, async ({ includeTaskTrace, signal }) => {
      let projected = target
      let lastSeq: number | undefined
      let completed = false
      let projector: LiveTodoTraceProjector | null = null
      const publish = () => setWorkspace((state) => updateConversation(state, threadId, (item) => (
        signal.aborted || !isMounted.current || isActiveThread(threadId)
          ? item : { ...projected, ...mergeConversationTitle(item, projected) }
      )))
      try {
        let attempts = 0
        while (!signal.aborted) {
          try {
            for await (const item of followConversationRun(threadId, runId, { includeTaskTrace, signal, afterSeq: lastSeq })) {
              if (signal.aborted || !isMounted.current || isActiveThread(threadId)) return
              if (item.type === 'snapshot') {
                if (item.snapshot.threadId !== threadId || item.snapshot.headRunId !== runId) throw new ConversationError('stream_event_invalid')
                // 该基线与运行重放起点对应，不能用刷新前的投影或游标拼接
                projected = restoreConversationFromTrace(item.snapshot, { model: target.model, includeTaskTrace })
                completed = !item.replay
                if (!completed) projected = { ...projected, runStatus: 'streaming' }
                projector?.close()
                if (projected.taskTrace.phase === 'ready') {
                  projector = new LiveTodoTraceProjector()
                  projector.hydrate(projected.taskTrace.snapshot, {
                    headRunId: runId, latestTurn: latestUserTurn(projected), isRunning: !completed,
                  })
                }
                publish()
                continue
              }
              if (lastSeq == null && (item.event.type !== 'RUN_STARTED' || item.event.runId !== runId || item.event.threadId !== threadId)) throw new ConversationError('stream_sequence_invalid')
              if (lastSeq != null && item.seq <= lastSeq) continue
              if (lastSeq != null && item.seq !== lastSeq + 1) throw new ConversationError('stream_sequence_invalid')
              projected = applyConversationEvent(projected, item.event)
              if (item.replayed) {
                // 已提交正文直接恢复；后续新增量以完整正文作为逐字显示起点
                projected = { ...projected, messages: projected.messages.map(message => (
                  message.liveText ? { ...message, liveText: undefined } : message
                )) }
              }
              if (projector) {
                const snapshot = projector.consume(item.event, { receivedAt: new Date().toISOString(), rootState: projected.serverState })
                projected = { ...projected, taskTrace: taskTraceView(snapshot) }
              }
              lastSeq = item.seq
              projected = { ...projected, lastSeq }
              completed = (item.event.type === 'RUN_FINISHED' && item.event.runId === runId)
                || (item.event.type === 'RUN_ERROR' && item.event.rawEvent?.source?.agentType !== 'subagent')
              publish()
            }
            if (completed) break
            throw new ConversationError('stream_disconnected')
          } catch (error) {
            if (signal.aborted) return
            if (!(isTransportFailure(error) || hasConversationErrorCode(error, 'stream_disconnected') || hasConversationErrorCode(error, 'stream_sequence_invalid'))
              || attempts >= DETACHED_TRACE_RECONNECT_LIMIT) throw error
            attempts += 1
            await waitForReconnect(Math.min(250 * (2 ** (attempts - 1)), RECONNECT_MAX_DELAY_MS), signal)
          }
        }
        if (signal.aborted) return
        clearActiveRunPersistence(runId)
        clearCancelPending(runId)
        const detail = await fetchConversationHistoryDetail(threadId, { includeTaskTrace, signal, suppressGlobalError: true })
        projected = restoreConversationFromTrace(detail, { previous: projected, model: projected.model, includeTaskTrace })
        publish()
      } catch (error) {
        if (signal.aborted || !isMounted.current || isActiveThread(threadId)) return
        const notice: NonNullable<Conversation['notice']> = completed ? historySyncNotice(`${runId}:history`) : {
          kind: 'error', content: conversationErrorMessage(error, 'stream_recovery_failed'),
        }
        // 恢复尚未收到基线时，旧闭包不能覆盖页面已显示的内容或游标
        setWorkspace((state) => updateConversation(state, threadId, (item) => (
          signal.aborted || !isMounted.current || isActiveThread(threadId) ? item : {
            ...item, runStatus: completed ? item.runStatus : 'detached', notice,
          }
        )))
      } finally {
        projector?.close()
      }
    })
  }, [clearActiveRunPersistence, clearCancelPending, isActiveThread, setWorkspace])

  const handoffTaskTraceFollow = useCallback(async (threadId: string) => {
    const handoff = await taskTraceFollowOwnership.current.handoff(threadId)
    for (const demotedThreadId of handoff.demotedThreadIds) {
      void followDetachedConversation(demotedThreadId)
    }
  }, [followDetachedConversation])

  const streamRun = useCallback(async (
    threadIdToStream: string,
    payload: ChatRequestPayload,
    mode: 'start' | 'resume',
    options: StreamRunOptions = { target: 'workspace' },
  ) => {
    if (threadIdToStream && isActiveThread(threadIdToStream)) return
    await taskTraceFollowOwnership.current.stop(threadIdToStream)
    if (threadIdToStream && isActiveThread(threadIdToStream)) return
    const streamEpoch = ++activeStreamEpoch.current
    const controller = new AbortController()
    // 同会话的新请求取得所有权后，旧请求的异步收尾永久失效
    for (const previous of streams.current.values()) {
      if (previous.threadId === threadIdToStream) previous.superseded = true
    }
    const owner = { threadId: threadIdToStream, runId: payload.runId, controller, receiving: true, superseded: false, todoProjector: null as LiveTodoTraceProjector | null }
    streams.current.set(streamEpoch, owner)
    liveEpochs.current.add(streamEpoch)
    if (options.target === 'draft') draftStreamEpoch.current = streamEpoch
    recoveryRequests.current.delete(threadIdToStream)

    const stream = mode === 'resume' ? resumeConversationRun : startConversationRun
    let target = options.target
    let targetThreadId = threadIdToStream
    let draftTarget = options.initialConversation
    let validationTarget = target === 'workspace'
      ? latestWorkspace.current.conversations.find(
          (item) => item.threadId === targetThreadId,
        )
      : draftTarget
    let todoProjector: LiveTodoTraceProjector | null = null
    let projectedTaskTrace = validationTarget?.taskTrace ?? { phase: 'unloaded' as const }
    let projectedSnapshot: TaskTraceSnapshot | null = null
    if (validationTarget?.taskTrace.phase !== 'unavailable') {
      todoProjector = new LiveTodoTraceProjector()
      const priorHead = validationTarget?.trace?.headRunId
        ?? latestUserTurn(validationTarget)?.runId
      if (
        validationTarget?.taskTrace.phase === 'ready'
        && priorHead
      ) {
        todoProjector.hydrate(validationTarget.taskTrace.snapshot, {
          headRunId: priorHead,
          latestTurn: latestUserTurn(validationTarget),
        })
      }
      const turn = mode === 'start'
        ? latestUserTurn(validationTarget)
        : undefined
      todoProjector.startRun({
        runId: payload.runId,
        inputKind: mode === 'start' ? 'ordinary' : 'resume',
        parentRunId: payload.parentRunId,
        turn,
      })
      projectedSnapshot = todoProjector.snapshot
      projectedTaskTrace = taskTraceView(projectedSnapshot)
      owner.todoProjector = todoProjector
    }
    const applyTaskTrace = (
      current: Conversation,
      event: Parameters<typeof applyConversationEvent>[1],
      receivedAt: string,
    ): Conversation => {
      if (!todoProjector) return current
      const snapshot = todoProjector.consume(event, {
        receivedAt,
        rootState: current.serverState,
      })
      if (snapshot !== projectedSnapshot) {
        projectedSnapshot = snapshot
        projectedTaskTrace = taskTraceView(snapshot)
      }
      return current.taskTrace === projectedTaskTrace
        ? current
        : { ...current, taskTrace: projectedTaskTrace }
    }
    const belongsToRun = (item: Conversation) => {
      return !owner.superseded
        && (item.activeRunId === undefined || item.activeRunId === payload.runId)
    }
    let inputAccepted = false
    let receivedEvent = false
    let mainTerminalReceived = false
    let traceAuthorityLoaded = false
    let requestPayload: ChatRequestPayload = { ...payload }
    let reconnectAttempt = 0
    let lastAppliedSeq: number | null = (
      options.initialAfterSeq
      ?? (target === 'draft'
        ? draftTarget?.lastSeq
        : latestWorkspace.current.conversations.find(
            (item) => item.threadId === targetThreadId,
          )?.lastSeq)
    ) ?? null
    // 持久化记录用 0 表示未知游标；线程投递的首条序号不一定从 1 开始
    if (lastAppliedSeq === 0) lastAppliedSeq = null
    const persistActiveRun = (immediate = false) => scheduleActiveRunPersistence({
      threadId: targetThreadId,
      payload: requestPayload,
      mode,
      // 尚未收到首帧时使用 0 作为刷新恢复记录中的未知游标哨兵
      lastSeq: lastAppliedSeq ?? 0,
    }, immediate)
    // 首次写入建立刷新恢复所有权，不能等待第一个节流周期
    persistActiveRun(true)

    try {
      while (!mainTerminalReceived) {
        try {
          for await (const { event, seq } of stream(
            requestPayload,
            controller.signal,
            reconnectAttempt === 0
              ? options.initialAfterSeq
              : lastAppliedSeq ?? undefined,
          )) {
        if (controller.signal.aborted || !liveEpochs.current.has(streamEpoch)) return
        receivedEvent = true
        if (!inputAccepted && event.type === 'RUN_STARTED' && !controller.signal.aborted && liveEpochs.current.has(streamEpoch)) {
          inputAccepted = true
          options.onAccepted?.()
        }
        const eventReceivedAt = new Date().toISOString()
        const reportedThreadId: string = 'threadId' in event && typeof event.threadId === 'string'
          ? event.threadId
          : (owner.threadId ?? targetThreadId)
        const canonicalIdentityChanged = targetThreadId !== reportedThreadId
          || requestPayload.threadId !== reportedThreadId
        owner.threadId = reportedThreadId
        targetThreadId = reportedThreadId
        if (requestPayload.threadId !== reportedThreadId) {
          requestPayload = { ...requestPayload, threadId: reportedThreadId }
        }

        if (seq != null && lastAppliedSeq != null && seq <= lastAppliedSeq) continue
        if (seq != null && lastAppliedSeq != null && seq !== lastAppliedSeq + 1) {
          throw new ConversationError(
            'stream_sequence_invalid',
            `expected=${lastAppliedSeq + 1}, actual=${seq}`,
          )
        }

        if (target === 'draft' && draftTarget) {
          const deferTextRender = event.type === 'TEXT_MESSAGE_CONTENT'
          const withEvent = applyTaskTrace(
            applyConversationEvent(draftTarget, event),
            event,
            eventReceivedAt,
          )
          const nextDraft = seq == null
            ? withEvent
            : { ...withEvent, lastSeq: seq, isHydrated: true }
          draftTarget = nextDraft
          if (seq != null) lastAppliedSeq = seq

          if (event.type === 'RUN_STARTED') {
            const candidateConversation = {
              ...nextDraft,
              threadId: reportedThreadId,
              isHydrated: true,
            }
            enqueueWorkspaceUpdate(streamEpoch, (state) => upsertConversation(
              draftStreamEpoch.current === streamEpoch ? selectCurrentConversation(state, reportedThreadId) : state,
              candidateConversation,
            ))
            enqueueDraftUpdate(streamEpoch, null)
            draftTarget = undefined
            validationTarget = candidateConversation
            target = 'workspace'
            targetThreadId = reportedThreadId
          } else {
            enqueueDraftUpdate(streamEpoch, nextDraft, deferTextRender)
            if (event.type === 'RUN_FINISHED' || event.type === 'RUN_ERROR') {
              enqueueWorkspaceUpdate(
                streamEpoch,
                (state) => upsertConversation(state, nextDraft),
              )
              enqueueDraftUpdate(streamEpoch, null)
            }
          }
          persistActiveRun(canonicalIdentityChanged)
          continue
        }

        // 先在顺序投影中验证协议事件，避免 Patch 等异常延迟到 React updater 后逃逸
        if (!validationTarget) {
          throw new ConversationError('stream_event_invalid', '缺少事件验证目标会话')
        }
        const validated = applyTaskTrace(
          applyConversationEvent(validationTarget, event),
          event,
          eventReceivedAt,
        )
        validationTarget = seq == null
          ? { ...validated, isHydrated: true }
          : { ...validated, lastSeq: seq, isHydrated: true }
        if (event.type === 'RUN_ERROR' && validationTarget.notice) {
          latestNoticeHandler.current?.(validationTarget.notice)
        }

        enqueueWorkspaceUpdate(streamEpoch, (state) => {
          const targetConversationId = reportedThreadId
          const nextState = updateConversation(state, targetConversationId, (item) => {
            const withEvent = applyConversationEvent(item, event)
            const withTaskTrace = state.currentThreadId === targetConversationId
              ? { ...withEvent, taskTrace: projectedTaskTrace }
              : withEvent
            return seq == null
              ? { ...withTaskTrace, isHydrated: true }
              : { ...withTaskTrace, lastSeq: seq, isHydrated: true }
          })
          return nextState
        }, event.type === 'TEXT_MESSAGE_CONTENT')
        if (seq != null) lastAppliedSeq = seq
        persistActiveRun(canonicalIdentityChanged)
        if (
          (event.type === 'RUN_FINISHED' && event.runId === payload.runId)
          || (
            event.type === 'RUN_ERROR'
            && (
              event.rawEvent?.runId === payload.runId
              || event.rawEvent?.source?.agentType !== 'subagent'
            )
          )
        ) {
          mainTerminalReceived = true
          owner.receiving = false
          clearActiveRunPersistence(payload.runId)
        }
          }
          if (mainTerminalReceived) break
          throw new ConversationError('stream_disconnected')
        } catch (error) {
          if (controller.signal.aborted) return
          const canRetry = (
            isTransportFailure(error)
            || hasConversationErrorCode(error, 'stream_disconnected')
            || hasConversationErrorCode(error, 'stream_sequence_invalid')
          )
            && (receivedEvent || options.initialAfterSeq != null)
          if (!canRetry || reconnectAttempt >= RECONNECT_LIMIT) throw error
          reconnectAttempt += 1
          await waitForReconnect(
            Math.min(250 * (2 ** (reconnectAttempt - 1)), RECONNECT_MAX_DELAY_MS),
            controller.signal,
          )
          if (controller.signal.aborted) return
        }
      }
      if (mainTerminalReceived && target === 'workspace') {
        const includeTaskTrace = latestWorkspace.current.currentThreadId === targetThreadId
        if (includeTaskTrace) await handoffTaskTraceFollow(targetThreadId)
        const detail = await fetchConversationHistoryDetail(targetThreadId, {
          includeTaskTrace,
          signal: controller.signal,
          suppressGlobalError: true,
        })
        const authoritative = restoreConversationFromTrace(detail, {
          previous: validationTarget ?? undefined,
          model: validationTarget?.model ?? payload.forwardedProps.model,
          lastDeliveredSeq: lastAppliedSeq ?? undefined,
          includeTaskTrace,
        })
        validationTarget = authoritative
        traceAuthorityLoaded = true
        enqueueWorkspaceUpdate(
          streamEpoch,
          (state) => updateConversation(
            state,
            targetThreadId,
            (item) => !belongsToRun(item) ? item : {
              ...authoritative,
              ...mergeConversationTitle(item, authoritative),
            },
          ),
        )
      }
    } catch (error) {
      if (controller.signal.aborted) return
      if (mainTerminalReceived) {
        if (recoveryRequests.current.get(targetThreadId)?.payload.runId === payload.runId) {
          recoveryRequests.current.delete(targetThreadId)
        }
        enqueueWorkspaceUpdate(streamEpoch, (state) => updateConversation(state, targetThreadId, (item) => !belongsToRun(item) ? item : ({
          ...item,
          notice: historySyncNotice(`${payload.runId}:history:${streamEpoch}`),
        })))
        return
      }
      const stableError = error instanceof InvalidStateDeltaError
        ? new ConversationError('state_patch_invalid', error)
        : error
      const acceptanceUnknown = !receivedEvent && (
        isTransportFailure(error)
        || hasConversationErrorCode(error, 'stream_disconnected')
      )
      if (!receivedEvent && error instanceof ApiError && error.status >= 400 && error.status < 500) {
        options.onRequestRejected?.()
      }
      // 协议诊断留在错误对象中，notice 只使用稳定错误码对应的恢复文案
      const message = acceptanceUnknown
        ? translateCurrent('连接已中断，尚无法确认任务状态，请恢复连接')
        : conversationErrorMessage(
        stableError,
        receivedEvent ? 'stream_event_invalid' : 'run_request_failed',
      )
      if (receivedEvent || acceptanceUnknown) {
        recoveryRequests.current.set(targetThreadId, {
          threadId: targetThreadId,
          payload: requestPayload,
          mode,
          options: {
            ...options,
            onRequestRejected: inputAccepted ? undefined : options.onRequestRejected,
            target,
            initialConversation: draftTarget,
            initialAfterSeq: lastAppliedSeq ?? 0,
          },
          receivedEvent,
        })
      } else {
        clearActiveRunPersistence(payload.runId)
      }

      if (target === 'draft' && draftTarget) {
        const erroredConversation: Conversation = {
          ...draftTarget,
          runStatus: acceptanceUnknown ? 'detached' : 'error',
          activeRunId: acceptanceUnknown ? payload.runId : undefined,
          notice: { kind: 'error', content: message, id: `${payload.runId}:connection:${streamEpoch}` },
          isHydrated: true,
        }
        draftTarget = erroredConversation
        enqueueDraftUpdate(streamEpoch, erroredConversation)
      } else if (receivedEvent || acceptanceUnknown) {
        const currentTargetThreadId = owner.threadId ?? targetThreadId
        enqueueWorkspaceUpdate(streamEpoch, (state) => updateConversation(
          state,
          currentTargetThreadId,
          (item) => ({
            ...markConversationDetached(item, message),
            runStatus: 'detached',
            activeRunId: item.activeRunId ?? payload.runId,
            notice: { kind: 'error', content: message, id: `${payload.runId}:connection:${streamEpoch}` },
          }),
        ))
      } else {
        if (!receivedEvent) clearActiveRunPersistence(payload.runId)
        const currentTargetThreadId = owner.threadId ?? targetThreadId
        enqueueWorkspaceUpdate(
          streamEpoch,
          (state) => updateConversation(state, currentTargetThreadId, (item) => ({
            ...item,
            runStatus: 'error',
            activeRunId: undefined,
            notice: { kind: 'error', content: message, id: `${payload.runId}:connection:${streamEpoch}` },
          })),
        )
      }
    } finally {
      todoProjector?.close()
      clearCancelPending(payload.runId)
      if (mainTerminalReceived) clearActiveRunPersistence(payload.runId)
      if (streams.current.get(streamEpoch) === owner) {
        if (!mainTerminalReceived) flushActiveRunPersistence(payload.runId)
        const currentTargetThreadId = owner.threadId ?? targetThreadId
        streams.current.delete(streamEpoch)

        if (target === 'draft' && draftTarget) {
          enqueueDraftUpdate(
            streamEpoch,
            markConversationDetached(
              draftTarget,
              translateCurrent('已停止接收实时输出，后端任务可能仍在继续'),
            ),
          )
        } else {
          enqueueWorkspaceUpdate(
            streamEpoch,
            (state) => updateConversation(
              state,
              currentTargetThreadId,
              (item) => !belongsToRun(item) ? item : markConversationDetached(
                item,
                translateCurrent('已停止接收实时输出，后端任务可能仍在继续'),
              ),
            ),
          )
        }
        if ((!mainTerminalReceived || !traceAuthorityLoaded) && target === 'workspace') {
          const timer = window.setTimeout(() => {
            delayedTraceFollowTimers.current.delete(timer)
            if (!isMounted.current || isActiveThread(currentTargetThreadId)) return
            void followDetachedConversation(currentTargetThreadId)
          }, 100)
          delayedTraceFollowTimers.current.add(timer)
        }
      }
      flushWorkspaceUpdates()
      flushDraftUpdate()
      liveEpochs.current.delete(streamEpoch)
    }
  }, [
    followDetachedConversation,
    isActiveThread,
    flushWorkspaceUpdates,
    flushDraftUpdate,
    handoffTaskTraceFollow,
    clearActiveRunPersistence,
    clearCancelPending,
    enqueueDraftUpdate,
    enqueueWorkspaceUpdate,
    flushActiveRunPersistence,
    scheduleActiveRunPersistence,
  ])

  const recoverConversation = useCallback(async (threadId: string) => {
    if (isActiveThread(threadId) || recoveryPending.current.has(threadId) || !isMounted.current) return
    const pending = recoveryRequests.current.get(threadId)
    const session = readActiveRunSession(threadId)
    const current = latestWorkspace.current.conversations.find((item) => item.threadId === threadId)
    const matchesPending = pending?.threadId === threadId && (
      current ? current.activeRunId === pending.payload.runId : pending.options.target === 'draft'
    )
    const request = matchesPending ? pending : (
      session?.threadId === threadId && current?.activeRunId === session.payload.runId
        ? { ...session, options: { target: 'workspace' as const, initialAfterSeq: Math.max(session.lastSeq, current.lastSeq ?? 0) } }
        : null
    )
    recoveryPending.current.add(threadId)
    try {
      if (current?.notice?.recovery === 'history') {
        await taskTraceFollowOwnership.current.follow(threadId, async ({ includeTaskTrace, signal }) => {
          try {
            const detail = await fetchConversationHistoryDetail(threadId, { includeTaskTrace, signal, suppressGlobalError: true })
            if (signal.aborted || !isMounted.current || isActiveThread(threadId)) return
            const previous = latestWorkspace.current.conversations.find((item) => item.threadId === threadId)
            if (!previous) return
            const restored = restoreConversationFromTrace(detail, {
              previous, model: previous.model, lastDeliveredSeq: previous.lastSeq, includeTaskTrace, taskTrace: previous.taskTrace,
            })
            setWorkspace((state) => updateConversation(state, threadId, (item) => (
              signal.aborted || !isMounted.current || isActiveThread(threadId)
                ? item
                : { ...restored, ...mergeConversationTitle(item, restored) }
            )))
          } catch {
            if (signal.aborted || !isMounted.current) return
            setWorkspace((state) => updateConversation(state, threadId, (item) => ({
              ...item, notice: historySyncNotice(crypto.randomUUID()),
            })))
          }
        })
        return
      }
      if (request?.options.target === 'workspace'
        && current?.runStatus === 'detached'
        && current.trace?.headRunId === request.payload.runId
        && current.trace.status.execution === 'running') {
        // 已确认受理的历史视图继续跟随 Trace，旧投递游标不能证明快照之后的内容边界
        recoveryRequests.current.delete(threadId)
        clearActiveRunPersistence(request.payload.runId)
        setWorkspace((state) => updateConversation(state, threadId, (item) => ({
          ...item, lastSeq: undefined, notice: undefined,
        })))
        await followDetachedConversation(threadId)
        return
      }
      if (!request) {
        await followDetachedConversation(threadId)
        return
      }
      if (request.options.target === 'draft') {
        setDraftConversation((item) => item ? { ...item, runStatus: 'streaming', notice: undefined } : item)
      } else {
        setWorkspace((state) => updateConversation(state, threadId, (item) => ({
          ...item, runStatus: 'streaming', notice: undefined,
        })))
      }
      await streamRun(threadId, request.payload, request.mode, request.options)
    } finally {
      recoveryPending.current.delete(threadId)
    }
  }, [clearActiveRunPersistence, followDetachedConversation, isActiveThread, setDraftConversation, setWorkspace, streamRun])

  const hasActiveStream = useCallback(() => streams.current.size > 0, [])
  const cancelRun = useCallback((threadId: string) => {
    const stream = [...streams.current.values()].find((candidate) => candidate.threadId === threadId && candidate.receiving)
    const conversation = latestWorkspace.current.conversations.find((item) => item.threadId === threadId)
    const observedRunId = conversation?.trace?.status.execution === 'running'
      && conversation.trace.headRunId === conversation.activeRunId ? conversation.activeRunId : undefined
    const runId = stream?.runId ?? observedRunId
    if (!runId) return Promise.resolve(false)
    const existing = cancelRequests.current.get(runId)
    if (existing) return existing
    if (cancelPending.current.has(runId)) return Promise.resolve(true)
    cancelPending.current.add(runId)
    setCancelPendingRunIds(new Set(cancelPending.current))
    const promise = cancelConversationRun(threadId, runId).then((result) => result.cancelled)
    cancelRequests.current.set(runId, promise)
    void promise.then((cancelled) => {
      cancelRequests.current.delete(runId)
      if (!cancelled) clearCancelPending(runId)
    }, () => {
      cancelRequests.current.delete(runId)
      clearCancelPending(runId)
    })
    return promise
  }, [clearCancelPending])
  const selectedRunId = workspace.conversations.find((item) => item.threadId === workspace.currentThreadId)?.activeRunId
  const cancelPendingRunId = selectedRunId && cancelPendingRunIds.has(selectedRunId) ? selectedRunId : null

  useEffect(() => {
    const handlePageHide = () => flushActiveRunPersistence()
    window.addEventListener('pagehide', handlePageHide)
    return () => window.removeEventListener('pagehide', handlePageHide)
  }, [flushActiveRunPersistence])

  useEffect(() => {
    const timers = delayedTraceFollowTimers.current
    const followOwnership = taskTraceFollowOwnership.current
    const connections = streams.current
    const epochs = liveEpochs.current
    const persistence = activeRunPersistence.current
    const cancellations = cancelRequests.current
    const pendingCancellations = cancelPending.current
    isMounted.current = true
    return () => {
      isMounted.current = false
      flushActiveRunPersistence()
      for (const owner of connections.values()) {
        owner.controller.abort()
        owner.todoProjector?.close()
      }
      connections.clear()
      epochs.clear()
      for (const owner of persistence.values()) {
        if (owner.timer != null) window.clearTimeout(owner.timer)
      }
      persistence.clear()
      cancellations.clear()
      pendingCancellations.clear()
      followOwnership.abortAll()
      for (const timer of timers) window.clearTimeout(timer)
      timers.clear()
      if (workspaceUpdateTimer.current != null) window.clearTimeout(workspaceUpdateTimer.current)
      if (draftUpdateTimer.current != null) window.clearTimeout(draftUpdateTimer.current)
      workspaceUpdateTimer.current = null
      draftUpdateTimer.current = null
      workspaceFrameUpdates.current = []
      pendingDraftFrameValue.current = undefined
    }
  }, [flushActiveRunPersistence])

  return {
    streamRun,
    recoverConversation,
    followDetachedConversation,
    handoffTaskTraceFollow,
    releaseDraft,
    cancelRun,
    cancelPendingRunId,
    hasActiveStream,
    isActiveThread,
  }
}
