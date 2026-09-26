import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import type { Dispatch, SetStateAction } from 'react'

import {
  fetchConversationHistoryDetail,
  fetchConversationHistoryGroupConfig,
  fetchConversationHistoryList,
  type ConversationHistoryDetail,
  type ConversationHistoryListItem,
} from '../../api/conversation/history'
import {
  clearActiveRunSession,
  readActiveRunSession,
} from '../conversation/stream/activeRunSession'
import { restoreConversationFromTrace } from '../conversation/trace/runtime'
import {
  isConversationUnavailable,
  type HistoryActivationRefresh,
  type HistoryRefreshResult,
} from '../conversation/trace/historyRefresh'
import { readThreadFromLocation } from '../../lib/threadRoute'
import {
  selectCurrentConversation,
  mergeConversationTitle,
  updateConversation,
  upsertConversation,
} from '../../lib/workspace'
import type { Conversation, WorkspaceState } from '../../types'
import { useI18n } from '../../i18n'
import type { RetainConversationDetails } from './useWorkspaceState'
import type { ModelCatalogStatus } from './useModelCatalog'
import { pruneSearchOnlyConversations } from './workspaceHistoryCache'

// 单页覆盖一次惯性滑动的浏览距离，避免用户在同一批数据内反复触底
const HISTORY_PAGE_SIZE = 100
// 历史分页从上一次请求完成后限制新手势，冷却期内的旧滚动意图不会排队执行
const HISTORY_LOAD_COOLDOWN_MS = 300
const HISTORY_SEARCH_DEBOUNCE_MS = 300

interface TracePageRequestIdentity {
  asOfSeq: number
  generation: string
  headRunId: string
  historyCursor: string
  runStatus: Conversation['runStatus']
  activeRunId?: string
  lastSeq?: number
}

interface TaskTraceRequestIdentity {
  traceAsOfSeq?: number
  traceHeadRunId?: string
  runStatus: Conversation['runStatus']
  activeRunId?: string
  lastSeq?: number
}

interface TaskTraceLoadFailure {
  requestId: number
  identity: TaskTraceRequestIdentity
}

interface HydrationRequest {
  controller: AbortController
  completion: Promise<void>
  foreground: number
  result?: HistoryRefreshResult
}

const ownsTracePageRequest = (
  conversation: Conversation | undefined,
  request: TracePageRequestIdentity,
) => (
  conversation?.trace?.asOfSeq === request.asOfSeq
  && conversation.trace.generation === request.generation
  && conversation.trace.headRunId === request.headRunId
  && conversation.trace.historyCursor === request.historyCursor
  && conversation.lastSeq === request.lastSeq
  && (
    (conversation.runStatus === request.runStatus
      && conversation.activeRunId === request.activeRunId)
    || (['idle', 'detached', 'waiting_approval', 'error'].includes(conversation.runStatus)
      && ['idle', 'detached', 'waiting_approval', 'error'].includes(request.runStatus)
      && (!conversation.activeRunId || conversation.activeRunId === request.headRunId)
      && (!request.activeRunId || request.activeRunId === request.headRunId))
  )
)

const taskTraceRequestIdentity = (
  conversation: Conversation,
): TaskTraceRequestIdentity => ({
  traceAsOfSeq: conversation.trace?.asOfSeq,
  traceHeadRunId: conversation.trace?.headRunId,
  runStatus: conversation.runStatus,
  activeRunId: conversation.activeRunId,
  lastSeq: conversation.lastSeq,
})

const matchesTaskTraceRequestIdentity = (
  conversation: Conversation | undefined,
  request: TaskTraceRequestIdentity,
) => (
  conversation?.isHydrated === true
  && conversation.trace?.asOfSeq === request.traceAsOfSeq
  && conversation.trace?.headRunId === request.traceHeadRunId
  && conversation.runStatus === request.runStatus
  && conversation.activeRunId === request.activeRunId
  && conversation.lastSeq === request.lastSeq
)

const ownsTaskTraceRequest = (
  conversation: Conversation | undefined,
  request: TaskTraceRequestIdentity,
) => (
  conversation?.taskTrace.phase === 'loading'
  && matchesTaskTraceRequestIdentity(conversation, request)
)

const ownsTaskTraceFailure = (
  conversation: Conversation | undefined,
  request: TaskTraceRequestIdentity,
) => (
  conversation?.taskTrace.phase === 'unloaded'
  && matchesTaskTraceRequestIdentity(conversation, request)
)

const withoutTaskTraceLoadFailure = (
  failures: Map<string, TaskTraceLoadFailure>,
  threadId: string,
) => {
  if (!failures.has(threadId)) return failures
  const next = new Map(failures)
  next.delete(threadId)
  return next
}

const canStartHistoryLoad = (lastSettledAt: number | null) => (
  lastSettledAt == null
  || Date.now() - lastSettledAt >= HISTORY_LOAD_COOLDOWN_MS
)

const sortConversations = (conversations: Conversation[]) =>
  [...conversations].sort((a, b) => Date.parse(b.updatedAt) - Date.parse(a.updatedAt))

const appendUniqueThreadIds = (current: string[], incoming: string[]) => {
  const seen = new Set(current)
  return [
    ...current,
    ...incoming.filter((threadId) => {
      if (seen.has(threadId)) return false
      seen.add(threadId)
      return true
    }),
  ]
}

const prependUniqueThreadIds = (current: string[], incoming: string[]) => {
  const incomingSet = new Set(incoming)
  return [...incoming, ...current.filter((threadId) => !incomingSet.has(threadId))]
}

const historyStatusToRunStatus = (status: string): Conversation['runStatus'] => {
  switch (status) {
    case 'running':
      return 'detached'
    case 'waiting_approval':
      return 'waiting_approval'
    case 'error':
      return 'error'
    default:
      return 'idle'
  }
}

export const historyItemFromDetail = (
  detail: ConversationHistoryDetail,
): ConversationHistoryListItem => {
  const pending = detail.interactions.filter((interaction) => interaction.status === 'pending')
  const pendingKinds = new Set(pending.map((interaction) => (
    interaction.kind === 'tinkerfin:plan_clarification'
      ? 'plan_clarification'
      : interaction.kind === 'tinkerfin:plan_review'
        ? 'plan_review'
        : interaction.kind === 'tool_approval'
          ? 'tool_approval'
          : 'input_required'
  )))
  const pendingKind = pendingKinds.size === 0
    ? null
    : pendingKinds.size === 1
      ? [...pendingKinds][0] ?? null
      : 'input_required'
  const status = detail.status.execution === 'running'
    ? 'running'
    : detail.status.execution === 'waiting'
      ? 'waiting_approval'
      : detail.status.execution === 'failed' || detail.status.execution === 'unknown'
        ? 'error'
        : 'idle'
  return {
    id: detail.id,
    threadId: detail.threadId,
    title: detail.title,
    titleSource: detail.titleSource,
    titleGenerationStatus: detail.titleGenerationStatus,
    titleSeq: detail.titleSeq,
    status,
    lastRunId: detail.headRunId,
    lastModel: detail.lastModel,
    accessMode: detail.accessMode,
    messageCount: detail.messageCount,
    toolCallCount: detail.toolCallCount,
    hasPendingInterrupt: pending.length > 0,
    pendingInteractionKind: pendingKind,
    pinned: detail.pinned,
    createdAt: detail.createdAt,
    updatedAt: detail.updatedAt,
  }
}

const conversationFromHistoryItem = (
  item: ConversationHistoryListItem,
  fallbackModel: string,
): Conversation => ({
  threadId: item.threadId,
  ...mergeConversationTitle(undefined, item),
  pinned: item.pinned,
  updatedAt: item.updatedAt,
  model: item.lastModel ?? fallbackModel,
  accessMode: item.accessMode,
  mode: 'default',
  messages: [],
  todos: [],
  taskTrace: { phase: 'unloaded' },
  pendingInteractionKind: item.pendingInteractionKind ?? undefined,
  runStatus: historyStatusToRunStatus(item.status),
  activeRunId: item.lastRunId ?? undefined,
  serverState: {},
  isHydrated: false,
  historySynchronized: false,
})

export const mergeHistoryConversations = (
  current: Conversation[],
  incoming: ConversationHistoryListItem[],
  fallbackModel: string,
) => {
  const byId = new Map(current.map((item) => [item.threadId, item]))
  for (const item of incoming) {
    const existing = byId.get(item.threadId)
    const summary = conversationFromHistoryItem(item, fallbackModel)
    if (!existing) {
      byId.set(item.threadId, summary)
      continue
    }
    const summaryTime = Date.parse(item.updatedAt)
    const existingTime = Date.parse(existing.updatedAt)
    const summaryIsNotOlder = Number.isFinite(summaryTime)
      && Number.isFinite(existingTime)
      && summaryTime >= existingTime
    const headChanged = item.lastRunId != null
      && item.lastRunId !== existing.trace?.headRunId
    const pendingChanged = summary.pendingInteractionKind !== existing.pendingInteractionKind
    const statusChanged = summary.runStatus !== existing.runStatus
    // 浏览器自有 live stream 优先；已水化快照只拒绝较旧摘要，不能屏蔽更新的权威状态
    const advanceHydratedRuntime = Boolean(
      existing.isHydrated
      && existing.runStatus !== 'streaming'
      && summaryIsNotOlder
      && (headChanged || pendingChanged || statusChanged || summaryTime > existingTime),
    )
    const preserveRuntime = existing.runStatus === 'streaming'
      || Boolean(existing.isHydrated && !advanceHydratedRuntime)
    byId.set(item.threadId, {
      ...existing,
      ...mergeConversationTitle(existing, item),
      pinned: item.pinned,
      updatedAt: preserveRuntime ? existing.updatedAt : item.updatedAt,
      model: preserveRuntime ? existing.model : summary.model,
      accessMode: preserveRuntime ? existing.accessMode : summary.accessMode,
      activeRunId: preserveRuntime ? existing.activeRunId : summary.activeRunId,
      pendingInteractionKind: existing.isHydrated
        && !advanceHydratedRuntime
        ? existing.pendingInteractionKind
        : summary.pendingInteractionKind,
      runStatus: preserveRuntime ? existing.runStatus : summary.runStatus,
      isHydrated: existing.isHydrated && !advanceHydratedRuntime,
    })
  }
  return sortConversations([...byId.values()])
}

export function useWorkspaceHistory({
  workspace,
  setWorkspace,
  retainConversationDetails,
  defaultModelId,
  modelCatalogStatus,
  refreshOnActivation = false,
  followDetachedConversation,
  prepareTaskTraceOwner,
  onToast,
}: {
  workspace: WorkspaceState
  setWorkspace: Dispatch<SetStateAction<WorkspaceState>>
  retainConversationDetails: RetainConversationDetails
  defaultModelId: string
  modelCatalogStatus: ModelCatalogStatus
  /** 当前页面重新显示或获得焦点时，重验所选会话的运行状态 */
  refreshOnActivation?: boolean
  followDetachedConversation: (threadId: string) => void | Promise<void>
  prepareTaskTraceOwner: (threadId: string) => Promise<void>
  onToast: (kind: 'error', message: string) => void
}) {
  const { t } = useI18n()
  const [historyCursor, setHistoryCursor] = useState<string | null>(null)
  const [historyThreadIds, setHistoryThreadIds] = useState<string[]>([])
  const [historyDayRanges, setHistoryDayRanges] = useState<number[]>([])
  const [historyQuery, setHistoryQuery] = useState('')
  const [searchThreadIds, setSearchThreadIds] = useState<string[]>([])
  const [searchCursor, setSearchCursor] = useState<string | null>(null)
  const [isHistorySearching, setHistorySearching] = useState(false)
  const [isSearchLoadingMore, setSearchLoadingMore] = useState(false)
  const [searchLoadError, setSearchLoadError] = useState<string | null>(null)
  const [searchRetryVersion, setSearchRetryVersion] = useState(0)
  const [isHistoryLoadingMore, setHistoryLoadingMore] = useState(false)
  const [historyLoadError, setHistoryLoadError] = useState<string | null>(null)
  const [isHistoryBootstrapped, setHistoryBootstrapped] = useState(false)
  const [historyBootstrapStatus, setHistoryBootstrapStatus] = useState<'loading' | 'ready' | 'error'>('loading')
  const [hydrationState, setHydrationState] = useState<{
    threadId: string
    status: 'loading' | 'failed'
    unavailable?: boolean
  } | null>(null)
  const [taskTraceLoadFailures, setTaskTraceLoadFailures] = useState(
    () => new Map<string, TaskTraceLoadFailure>(),
  )
  const historyThreadIdsRef = useRef<string[]>([])
  const searchOnlyThreadIds = useRef(new Set<string>())
  const normalizedHistoryQuery = historyQuery.trim()
  const normalizedHistoryQueryRef = useRef(normalizedHistoryQuery)
  normalizedHistoryQueryRef.current = normalizedHistoryQuery
  const hasHistoryBootstrapStarted = useRef(false)
  const historyBootstrapAbortController = useRef<AbortController | null>(null)
  const historyLoadingRef = useRef(false)
  const historyLastLoadSettledAt = useRef<number | null>(null)
  const historyInFlightCursor = useRef<string | null>(null)
  const loadedHistoryCursors = useRef(new Set<string>())
  const historyAbortController = useRef<AbortController | null>(null)
  const historySearchDebounceTimer = useRef<number | null>(null)
  const historySearchLastLoadSettledAt = useRef<number | null>(null)
  const historySearchAbortController = useRef<AbortController | null>(null)
  const historySearchGeneration = useRef(0)
  const historySearchLoadingRef = useRef(false)
  const historySearchInFlightCursor = useRef<string | null>(null)
  const loadedSearchCursors = useRef(new Set<string>())
  const prefetchedHistoryDetails = useRef(new Map<string, ConversationHistoryDetail>())
  const hydrationRequests = useRef(new Map<string, HydrationRequest>())
  const [foreground, setForeground] = useState(0)
  const latestForeground = useRef(foreground)
  const activation = useMemo(() => (
    refreshOnActivation && workspace.currentThreadId
      ? { threadId: workspace.currentThreadId, foreground }
      : null
  ), [foreground, refreshOnActivation, workspace.currentThreadId])
  const [settledActivation, setSettledActivation] = useState<typeof activation>(null)
  const taskTraceRequests = useRef(new Map<string, AbortController>())
  const taskTraceRequestSequence = useRef(0)
  const olderTraceRequests = useRef(new Map<string, AbortController>())
  const initialThreadId = useRef(readThreadFromLocation())
  const latestWorkspace = useRef(workspace)
  const latestToast = useRef(onToast)
  latestToast.current = onToast
  const notifiedTaskTraceRequest = useRef<number | null>(null)
  latestWorkspace.current = workspace

  useEffect(() => {
    const failure = taskTraceLoadFailures.get(workspace.currentThreadId)
    const current = workspace.conversations.find((item) => item.threadId === workspace.currentThreadId)
    if (!failure || !ownsTaskTraceFailure(current, failure.identity)
      || notifiedTaskTraceRequest.current === failure.requestId) return
    notifiedTaskTraceRequest.current = failure.requestId
    latestToast.current('error', t('任务轨迹不可用'))
  }, [taskTraceLoadFailures, workspace, t])

  const refreshHistoryList = useCallback(async (
    options: { preferredThreadId?: string; signal?: AbortSignal } = {},
  ) => {
    const preferredThreadId = options.preferredThreadId ?? ''
    const retention = preferredThreadId ? retainConversationDetails(preferredThreadId) : undefined
    try {
      const [response, preferredDetail, groupConfig] = await Promise.all([
        fetchConversationHistoryList({
          pageSize: HISTORY_PAGE_SIZE,
          signal: options.signal,
        }),
        preferredThreadId
          ? prepareTaskTraceOwner(preferredThreadId)
            .then(() => fetchConversationHistoryDetail(preferredThreadId, {
              includeTaskTrace: true,
              signal: options.signal,
            }))
            .catch(() => undefined)
          : Promise.resolve(undefined),
        fetchConversationHistoryGroupConfig({ signal: options.signal }),
      ])
      if (options.signal?.aborted) return false
      const activeSession = readActiveRunSession(preferredThreadId)
      const preferredExists = preferredThreadId
        ? Boolean(preferredDetail || response.items.some((item) => item.threadId === preferredThreadId))
        : true
      if (
        (response.items.length === 0 && !preferredDetail)
        || (!preferredExists && activeSession?.threadId === preferredThreadId)
      ) clearActiveRunSession(activeSession?.payload.runId)
      if (preferredDetail) prefetchedHistoryDetails.current.set(preferredThreadId, preferredDetail)
      const historyItems = preferredDetail && !response.items.some((item) => item.threadId === preferredThreadId)
        ? [...response.items, historyItemFromDetail(preferredDetail)]
        : response.items
      const nextThreadIds = historyItems.map((item) => item.threadId)
      historyThreadIdsRef.current = nextThreadIds
      setHistoryThreadIds(nextThreadIds)
      for (const threadId of nextThreadIds) searchOnlyThreadIds.current.delete(threadId)
      loadedHistoryCursors.current.clear()
      historyLastLoadSettledAt.current = null
      setHistoryCursor(response.nextCursor ?? null)
      setHistoryDayRanges(groupConfig.dayRanges)
      setWorkspace((state) => {
        const conversations = mergeHistoryConversations(
          state.conversations,
          historyItems,
          defaultModelId,
        )
        const hasPreferred = preferredThreadId
          ? conversations.some((item) => item.threadId === preferredThreadId)
          : false
        const hasCurrent = state.currentThreadId
          ? conversations.some((item) => item.threadId === state.currentThreadId)
          : false
        return selectCurrentConversation(
          { conversations, currentThreadId: state.currentThreadId },
          hasPreferred
            ? preferredThreadId
            : hasCurrent
              ? state.currentThreadId
              : (conversations[0]?.threadId ?? ''),
        )
      })
      return true
    } catch {
      return false
    } finally {
      retention?.release()
    }
  }, [defaultModelId, prepareTaskTraceOwner, retainConversationDetails, setWorkspace])

  useEffect(() => {
    const known = new Set(historyThreadIdsRef.current)
    const additions = sortConversations(workspace.conversations)
      .map((conversation) => conversation.threadId)
      .filter((threadId) => (
        threadId
        && !known.has(threadId)
        && !searchOnlyThreadIds.current.has(threadId)
      ))
    if (additions.length === 0) return
    const next = prependUniqueThreadIds(historyThreadIdsRef.current, additions)
    historyThreadIdsRef.current = next
    setHistoryThreadIds(next)
  }, [workspace.conversations])

  useEffect(() => {
    const threadId = workspace.currentThreadId
    if (normalizedHistoryQuery || !threadId || !searchOnlyThreadIds.current.has(threadId)) return
    searchOnlyThreadIds.current.delete(threadId)
    const next = prependUniqueThreadIds(historyThreadIdsRef.current, [threadId])
    historyThreadIdsRef.current = next
    setHistoryThreadIds(next)
  }, [normalizedHistoryQuery, workspace.currentThreadId])

  useEffect(() => {
    historySearchGeneration.current += 1
    const generation = historySearchGeneration.current
    if (historySearchDebounceTimer.current != null) {
      window.clearTimeout(historySearchDebounceTimer.current)
      historySearchDebounceTimer.current = null
    }
    historySearchLastLoadSettledAt.current = null
    historySearchAbortController.current?.abort()
    historySearchAbortController.current = null
    historySearchLoadingRef.current = false
    historySearchInFlightCursor.current = null
    loadedSearchCursors.current.clear()
    if (searchOnlyThreadIds.current.size > 0) {
      const searchOnly = searchOnlyThreadIds.current
      const normal = new Set(historyThreadIdsRef.current)
      const retained = pruneSearchOnlyConversations(
        latestWorkspace.current,
        searchOnly,
        normal,
      )
      searchOnlyThreadIds.current = retained.retainedSearchOnlyThreadIds
      setWorkspace((state) => pruneSearchOnlyConversations(
        state,
        searchOnly,
        normal,
      ).state)
    }
    setSearchThreadIds([])
    setSearchCursor(null)
    setSearchLoadError(null)
    setSearchLoadingMore(false)

    if (!normalizedHistoryQuery) {
      setHistorySearching(false)
      return
    }

    setHistorySearching(true)
    historySearchDebounceTimer.current = window.setTimeout(() => {
      historySearchDebounceTimer.current = null
      const controller = new AbortController()
      historySearchAbortController.current = controller
      void fetchConversationHistoryList({
        pageSize: HISTORY_PAGE_SIZE,
        query: normalizedHistoryQuery,
        signal: controller.signal,
        suppressGlobalError: true,
      }).then((response) => {
        if (
          controller.signal.aborted
          || historySearchGeneration.current !== generation
        ) return
        const normalIds = new Set(historyThreadIdsRef.current)
        for (const item of response.items) {
          if (!normalIds.has(item.threadId)) searchOnlyThreadIds.current.add(item.threadId)
        }
        setSearchThreadIds(response.items.map((item) => item.threadId))
        setSearchCursor(response.nextCursor ?? null)
        setWorkspace((state) => ({
          ...state,
          conversations: mergeHistoryConversations(
            state.conversations,
            response.items,
            defaultModelId,
          ),
        }))
      }).catch(() => {
        if (
          !controller.signal.aborted
          && historySearchGeneration.current === generation
        ) {
          setSearchLoadError(t('搜索会话失败'))
          latestToast.current('error', t('搜索会话失败'))
        }
      }).finally(() => {
        if (historySearchGeneration.current !== generation) return
        if (historySearchAbortController.current === controller) {
          historySearchAbortController.current = null
        }
        if (!controller.signal.aborted) setHistorySearching(false)
      })
    }, HISTORY_SEARCH_DEBOUNCE_MS)

    return () => {
      if (historySearchDebounceTimer.current != null) {
        window.clearTimeout(historySearchDebounceTimer.current)
        historySearchDebounceTimer.current = null
      }
      historySearchAbortController.current?.abort()
      historySearchAbortController.current = null
    }
  }, [defaultModelId, normalizedHistoryQuery, searchRetryVersion, setWorkspace, t])

  const retryHistoryBootstrap = useCallback(() => {
    historyBootstrapAbortController.current?.abort()
    const controller = new AbortController()
    historyBootstrapAbortController.current = controller
    setHistoryBootstrapStatus('loading')
    void refreshHistoryList({
      preferredThreadId: readThreadFromLocation(),
      signal: controller.signal,
    }).then((succeeded) => {
      if (controller.signal.aborted) return
      setHistoryBootstrapStatus(succeeded ? 'ready' : 'error')
      setHistoryBootstrapped(true)
    }).finally(() => {
      if (historyBootstrapAbortController.current === controller) {
        historyBootstrapAbortController.current = null
      }
    })
  }, [refreshHistoryList])

  const loadMoreNormalHistory = useCallback((isExplicitRetry = false) => {
    if (historyLoadingRef.current) return
    if (historyLoadError && !isExplicitRetry) return
    const cursor = historyCursor
    if (
      !cursor
      || loadedHistoryCursors.current.has(cursor)
      || (
        !isExplicitRetry
        && !canStartHistoryLoad(historyLastLoadSettledAt.current)
      )
    ) return
    historyLoadingRef.current = true
    historyInFlightCursor.current = cursor
    setHistoryLoadingMore(true)
    setHistoryLoadError(null)
    const controller = new AbortController()
    historyAbortController.current = controller
    void fetchConversationHistoryList({
      pageSize: HISTORY_PAGE_SIZE,
      cursor,
      signal: controller.signal,
      suppressGlobalError: true,
    }).then((response) => {
      if (historyInFlightCursor.current !== cursor) return
      historyLastLoadSettledAt.current = Date.now()
      loadedHistoryCursors.current.add(cursor)
      const incomingIds = response.items.map((item) => item.threadId)
      const nextThreadIds = appendUniqueThreadIds(
        historyThreadIdsRef.current,
        incomingIds,
      )
      historyThreadIdsRef.current = nextThreadIds
      setHistoryThreadIds(nextThreadIds)
      for (const threadId of incomingIds) searchOnlyThreadIds.current.delete(threadId)
      setHistoryCursor(response.nextCursor ?? null)
      setWorkspace((state) => ({
        ...state,
        conversations: mergeHistoryConversations(
          state.conversations,
          response.items,
          defaultModelId,
        ),
      }))
    }).catch(() => {
      if (!controller.signal.aborted && historyInFlightCursor.current === cursor) {
        // 错误一旦对用户可见，重试所有权必须同时释放，不能留下不可观察的忙碌窗口
        historyLastLoadSettledAt.current = Date.now()
        historyAbortController.current = null
        historyInFlightCursor.current = null
        historyLoadingRef.current = false
        setHistoryLoadingMore(false)
        setHistoryLoadError(t('历史记录加载失败'))
        latestToast.current('error', t('历史记录加载失败'))
      }
    }).finally(() => {
      if (historyInFlightCursor.current !== cursor) return
      historyAbortController.current = null
      historyInFlightCursor.current = null
      historyLoadingRef.current = false
      if (!controller.signal.aborted) setHistoryLoadingMore(false)
    })
  }, [defaultModelId, historyCursor, historyLoadError, setWorkspace, t])

  const loadMoreSearchHistory = useCallback((isExplicitRetry = false) => {
    if (
      historySearchLoadingRef.current
    ) return
    if (searchLoadError && !isExplicitRetry) return
    const cursor = searchCursor
    const query = normalizedHistoryQuery
    if (
      !query
      || !cursor
      || loadedSearchCursors.current.has(cursor)
      || (
        !isExplicitRetry
        && !canStartHistoryLoad(historySearchLastLoadSettledAt.current)
      )
    ) return
    historySearchLoadingRef.current = true
    historySearchInFlightCursor.current = cursor
    setSearchLoadingMore(true)
    setSearchLoadError(null)
    const controller = new AbortController()
    historySearchAbortController.current = controller
    void fetchConversationHistoryList({
      pageSize: HISTORY_PAGE_SIZE,
      cursor,
      query,
      signal: controller.signal,
      suppressGlobalError: true,
    }).then((response) => {
      if (
        controller.signal.aborted
        || historySearchInFlightCursor.current !== cursor
        || normalizedHistoryQueryRef.current !== query
      ) return
      historySearchLastLoadSettledAt.current = Date.now()
      loadedSearchCursors.current.add(cursor)
      const normalIds = new Set(historyThreadIdsRef.current)
      for (const item of response.items) {
        if (!normalIds.has(item.threadId)) searchOnlyThreadIds.current.add(item.threadId)
      }
      setSearchThreadIds((current) => appendUniqueThreadIds(
        current,
        response.items.map((item) => item.threadId),
      ))
      setSearchCursor(response.nextCursor ?? null)
      setWorkspace((state) => ({
        ...state,
        conversations: mergeHistoryConversations(
          state.conversations,
          response.items,
          defaultModelId,
        ),
      }))
    }).catch(() => {
      if (
        !controller.signal.aborted
        && historySearchInFlightCursor.current === cursor
        && normalizedHistoryQueryRef.current === query
      ) {
        historySearchLastLoadSettledAt.current = Date.now()
        historySearchAbortController.current = null
        historySearchInFlightCursor.current = null
        historySearchLoadingRef.current = false
        setSearchLoadingMore(false)
        setSearchLoadError(t('搜索会话失败'))
        latestToast.current('error', t('搜索会话失败'))
      }
    }).finally(() => {
      if (historySearchInFlightCursor.current !== cursor) return
      historySearchAbortController.current = null
      historySearchInFlightCursor.current = null
      historySearchLoadingRef.current = false
      if (!controller.signal.aborted) setSearchLoadingMore(false)
    })
  }, [defaultModelId, normalizedHistoryQuery, searchCursor, searchLoadError, setWorkspace, t])

  const loadMoreHistory = useCallback((isExplicitRetry = false) => {
    if (normalizedHistoryQuery) loadMoreSearchHistory(isExplicitRetry)
    else loadMoreNormalHistory(isExplicitRetry)
  }, [loadMoreNormalHistory, loadMoreSearchHistory, normalizedHistoryQuery])

  const retryHistoryLoad = useCallback(() => {
    if (
      normalizedHistoryQuery
      && searchLoadError
      && searchCursor == null
    ) {
      setSearchRetryVersion((value) => value + 1)
      return
    }
    loadMoreHistory(true)
  }, [loadMoreHistory, normalizedHistoryQuery, searchCursor, searchLoadError])

  const hydrateTaskTrace = useCallback(async (threadId: string, isRetry = false) => {
    const target = latestWorkspace.current.conversations.find(
      (item) => item.threadId === threadId,
    )
    if (!target?.isHydrated) return false
    if (
      target.taskTrace.phase === 'ready'
      || (target.taskTrace.phase === 'unavailable' && !isRetry)
    ) {
      const retention = retainConversationDetails(threadId)
      try {
        await prepareTaskTraceOwner(threadId)
        return true
      } finally {
        retention.release()
      }
    }
    const priorFailure = taskTraceLoadFailures.get(threadId)
    if (
      priorFailure
      && ownsTaskTraceFailure(target, priorFailure.identity)
      && !isRetry
    ) return false
    const existing = taskTraceRequests.current.get(threadId)
    if (existing && !existing.signal.aborted) return false
    if (existing) taskTraceRequests.current.delete(threadId)
    const retention = retainConversationDetails(threadId)
    const requestIdentity = taskTraceRequestIdentity(target)
    const requestId = taskTraceRequestSequence.current + 1
    taskTraceRequestSequence.current = requestId
    const controller = new AbortController()
    taskTraceRequests.current.set(threadId, controller)
    setWorkspace((state) => updateConversation(state, threadId, (item) => ({
      ...item,
      taskTrace: { phase: 'loading' },
    })))
    try {
      await prepareTaskTraceOwner(threadId)
      if (controller.signal.aborted) return false
      const detail = await fetchConversationHistoryDetail(threadId, {
        includeTaskTrace: true,
        signal: controller.signal,
        suppressGlobalError: true,
      })
      if (
        controller.signal.aborted
        || taskTraceRequests.current.get(threadId) !== controller
        || latestWorkspace.current.currentThreadId !== threadId
        || !ownsTaskTraceRequest(
          latestWorkspace.current.conversations.find((item) => item.threadId === threadId),
          requestIdentity,
        )
      ) return false
      const previous = latestWorkspace.current.conversations.find((item) => item.threadId === threadId)
      if (!previous) return false
      const restored = restoreConversationFromTrace(detail, {
        previous,
        model: previous.model,
        lastDeliveredSeq: previous.lastSeq,
        includeTaskTrace: true,
      })
      setWorkspace((state) => (
        state.currentThreadId !== threadId
          ? state
          : updateConversation(
              state,
              threadId,
              (item) => !controller.signal.aborted && ownsTaskTraceRequest(item, requestIdentity)
                // 这里只补任务轨迹；正文和连接状态仍由会话流管理
                ? { ...item, taskTrace: restored.taskTrace }
                : item,
            )
      ))
      setTaskTraceLoadFailures((current) => (
        withoutTaskTraceLoadFailure(current, threadId)
      ))
      return true
    } catch {
      const current = latestWorkspace.current.conversations.find(
        (item) => item.threadId === threadId,
      )
      if (!controller.signal.aborted && ownsTaskTraceRequest(current, requestIdentity)) {
        setWorkspace((state) => (
          state.currentThreadId !== threadId
            ? state
            : updateConversation(state, threadId, (item) => (
                ownsTaskTraceRequest(item, requestIdentity)
                  ? { ...item, taskTrace: { phase: 'unloaded' } }
                  : item
              ))
        ))
        setTaskTraceLoadFailures((failures) => {
          const next = new Map(failures)
          next.set(threadId, { identity: requestIdentity, requestId })
          return next
        })
      }
      return false
    } finally {
      retention.release()
      if (taskTraceRequests.current.get(threadId) === controller) {
        taskTraceRequests.current.delete(threadId)
      }
    }
  }, [prepareTaskTraceOwner, retainConversationDetails, setWorkspace, taskTraceLoadFailures])

  const retryTaskTrace = useCallback((threadId: string) => {
    setTaskTraceLoadFailures((current) => (
      withoutTaskTraceLoadFailure(current, threadId)
    ))
    void hydrateTaskTrace(threadId, true)
  }, [hydrateTaskTrace])

  const hydrateConversation = useCallback((
    threadId: string,
    options: { refresh?: boolean } = {},
  ): Promise<void> => {
    const target = latestWorkspace.current.conversations.find((item) => item.threadId === threadId)
    if (!target || (options.refresh && target.runStatus === 'streaming')) return Promise.resolve()
    if (target.isHydrated && !options.refresh) {
      return hydrateTaskTrace(threadId).then((loaded) => {
        if (loaded) void followDetachedConversation(threadId)
      })
    }
    const existingRequest = hydrationRequests.current.get(threadId)
    if (existingRequest && !existingRequest.controller.signal.aborted) {
      existingRequest.foreground = latestForeground.current
      return existingRequest.completion
    }
    if (existingRequest) hydrationRequests.current.delete(threadId)

    const retention = retainConversationDetails(threadId)
    const requestIdentity = target.isHydrated ? taskTraceRequestIdentity(target) : undefined
    const controller = new AbortController()
    setHydrationState({ threadId, status: 'loading' })
    const request: HydrationRequest = {
      controller,
      foreground: latestForeground.current,
      // 请求归所选会话持有；激活消费者退出后，仍等待同一份结果
      completion: Promise.resolve().then(async () => {
        try {
          if (controller.signal.aborted) return
          await prepareTaskTraceOwner(threadId)
          while (!controller.signal.aborted) {
            const owner = latestWorkspace.current.conversations.find((item) => item.threadId === threadId)
            if (!owner || latestWorkspace.current.currentThreadId !== threadId
              || (requestIdentity && !matchesTaskTraceRequestIdentity(owner, requestIdentity))) return
            const requestedForeground = latestForeground.current
            const prefetched = prefetchedHistoryDetails.current.get(threadId)
            prefetchedHistoryDetails.current.delete(threadId)
            let detail: ConversationHistoryDetail
            try {
              detail = (options.refresh ? undefined : prefetched)
                ?? await fetchConversationHistoryDetail(threadId, {
                  includeTaskTrace: true,
                  signal: controller.signal,
                  suppressGlobalError: true,
                })
            } catch (error) {
              if (request.foreground > requestedForeground && !controller.signal.aborted) continue
              throw error
            }
            if (
              controller.signal.aborted
              || hydrationRequests.current.get(threadId) !== request
            ) return
            const current = latestWorkspace.current.conversations.find((item) => item.threadId === threadId)
            if (!current || latestWorkspace.current.currentThreadId !== threadId
              || (requestIdentity && !matchesTaskTraceRequestIdentity(current, requestIdentity))) return
            // 恢复焦点后要求的新观测不能由失焦前已发出的读取替代
            if (request.foreground > requestedForeground) continue
            const restored = restoreConversationFromTrace(detail, {
              previous: current,
              preserveHistory: options.refresh,
              model: detail.lastModel ?? target.model,
              lastDeliveredSeq: target.lastSeq,
              includeTaskTrace: true,
            })
            request.result = { phase: 'ready', observation: restored.trace ?? detail }
            setHydrationState((current) => current?.threadId === threadId ? null : current)
            setWorkspace((state) => {
              const previous = state.conversations.find((item) => item.threadId === threadId)
              if (controller.signal.aborted || !previous || state.currentThreadId !== threadId
                || (requestIdentity && !matchesTaskTraceRequestIdentity(previous, requestIdentity))) return state
              return upsertConversation(state, {
                ...restored,
                ...mergeConversationTitle(previous, restored),
                isHydrated: true,
              })
            })
            return
          }
        } catch (error) {
          const current = latestWorkspace.current.conversations.find((item) => item.threadId === threadId)
          if (current && latestWorkspace.current.currentThreadId === threadId
            && !controller.signal.aborted && hydrationRequests.current.get(threadId) === request
            && (!requestIdentity || matchesTaskTraceRequestIdentity(current, requestIdentity))) {
            const unavailable = isConversationUnavailable(error)
            request.result = { phase: unavailable ? 'unavailable' : 'failed' }
            setHydrationState({ threadId, status: 'failed', unavailable })
            onToast('error', t('会话加载失败，请重试'))
          }
        } finally {
          retention.release()
          if (hydrationRequests.current.get(threadId) === request) {
            hydrationRequests.current.delete(threadId)
            setHydrationState((current) => (
              current?.threadId === threadId && current.status === 'loading' ? null : current
            ))
          }
        }
      }),
    }
    hydrationRequests.current.set(threadId, request)
    return request.completion
  }, [
    followDetachedConversation,
    hydrateTaskTrace,
    onToast,
    prepareTaskTraceOwner,
    retainConversationDetails,
    setWorkspace,
    t,
  ])

  useEffect(() => {
    let stale = document.visibilityState === 'hidden'
    const markStale = () => { stale = true }
    const refreshIfStale = () => {
      if (!stale || document.visibilityState === 'hidden') return
      stale = false
      latestForeground.current += 1
      setForeground(latestForeground.current)
    }
    const updateVisibility = () => {
      if (document.visibilityState === 'hidden') markStale()
      else refreshIfStale()
    }
    window.addEventListener('blur', markStale)
    window.addEventListener('focus', refreshIfStale)
    document.addEventListener('visibilitychange', updateVisibility)
    return () => {
      window.removeEventListener('blur', markStale)
      window.removeEventListener('focus', refreshIfStale)
      document.removeEventListener('visibilitychange', updateVisibility)
    }
  }, [])

  const latestHydrateConversation = useRef(hydrateConversation)
  latestHydrateConversation.current = hydrateConversation
  useEffect(() => {
    if (!activation || document.visibilityState === 'hidden') return
    let disposed = false
    void latestHydrateConversation.current(activation.threadId, { refresh: true }).then(() => {
      if (!disposed) setSettledActivation(activation)
    })
    return () => { disposed = true }
  }, [activation])

  const recheckConversationHistory = useCallback(async (threadId: string): Promise<HistoryRefreshResult> => {
    const completion = hydrateConversation(threadId, { refresh: true })
    const request = hydrationRequests.current.get(threadId)
    await completion
    return request?.result ?? { phase: 'failed' }
  }, [hydrateConversation])

  const loadOlderTrace = useCallback(async (
    threadId: string,
    options: { signal?: AbortSignal } = {},
  ) => {
    if (options.signal?.aborted) return false
    const target = latestWorkspace.current.conversations.find(
      (item) => item.threadId === threadId,
    )
    const trace = target?.trace
    const cursor = trace?.historyCursor
    if (!target || !trace || !cursor) return false
    const existingRequest = olderTraceRequests.current.get(threadId)
    if (existingRequest) {
      if (!options.signal) return false
      existingRequest.abort()
    }
    const retention = retainConversationDetails(threadId)
    const requestIdentity: TracePageRequestIdentity = {
      asOfSeq: trace.asOfSeq,
      generation: trace.generation,
      headRunId: trace.headRunId,
      historyCursor: cursor,
      runStatus: target.runStatus,
      activeRunId: target.activeRunId,
      lastSeq: target.lastSeq,
    }
    const controller = new AbortController()
    const abortFromCaller = () => controller.abort(options.signal?.reason)
    options.signal?.addEventListener('abort', abortFromCaller, { once: true })
    olderTraceRequests.current.set(threadId, controller)
    try {
      const detail = await fetchConversationHistoryDetail(threadId, {
        includeTaskTrace: false,
        historyCursor: cursor,
        limit: 100,
        signal: controller.signal,
        suppressGlobalError: true,
      })
      if (controller.signal.aborted || olderTraceRequests.current.get(threadId) !== controller) {
        return false
      }
      const latest = latestWorkspace.current.conversations.find(
        (item) => item.threadId === threadId,
      )
      if (
        !ownsTracePageRequest(latest, requestIdentity)
        || detail.asOfSeq !== requestIdentity.asOfSeq
        || detail.generation !== requestIdentity.generation
        || detail.headRunId !== requestIdentity.headRunId
      ) return false
      const restored = restoreConversationFromTrace(detail, {
        previous: latest,
        expandHistory: true,
        model: latest?.model ?? target.model,
        lastDeliveredSeq: latest?.lastSeq,
        includeTaskTrace: false,
        taskTrace: latest?.taskTrace,
      })
      setWorkspace((state) => {
        const current = state.conversations.find((item) => item.threadId === threadId)
        // follow 已推进权威前缀时丢弃旧分页，不能让 fixed-as-of 响应回退新状态
        if (controller.signal.aborted || !ownsTracePageRequest(current, requestIdentity)) return state
        return upsertConversation(state, { ...restored, ...mergeConversationTitle(current, restored) })
      })
      return true
    } catch {
      if (!controller.signal.aborted) onToast('error', t('会话加载失败，请重试'))
      return false
    } finally {
      retention.release()
      options.signal?.removeEventListener('abort', abortFromCaller)
      if (olderTraceRequests.current.get(threadId) === controller) {
        olderTraceRequests.current.delete(threadId)
      }
    }
  }, [onToast, retainConversationDetails, setWorkspace, t])

  const activeHistoryThreadIds = normalizedHistoryQuery
    ? searchThreadIds
    : historyThreadIds
  const historyConversations = useMemo(() => {
    const byId = new Map(workspace.conversations.map((item) => [item.threadId, item]))
    return activeHistoryThreadIds
      .map((threadId) => byId.get(threadId))
      .filter((item): item is Conversation => item != null)
  }, [activeHistoryThreadIds, workspace.conversations])
  const activeHistoryCursor = normalizedHistoryQuery ? searchCursor : historyCursor
  const activeHistoryLoadingMore = normalizedHistoryQuery
    ? isSearchLoadingMore
    : isHistoryLoadingMore
  const activeHistoryLoadError = normalizedHistoryQuery
    ? searchLoadError
    : historyLoadError

  useEffect(() => {
    if (modelCatalogStatus === 'loading' || isHistoryBootstrapped) return
    if (hasHistoryBootstrapStarted.current) return
    hasHistoryBootstrapStarted.current = true
    const controller = new AbortController()
    historyBootstrapAbortController.current = controller
    setHistoryBootstrapStatus('loading')
    void refreshHistoryList({
      preferredThreadId: initialThreadId.current,
      signal: controller.signal,
    }).then((succeeded) => {
      if (!controller.signal.aborted) {
        setHistoryBootstrapStatus(succeeded ? 'ready' : 'error')
        setHistoryBootstrapped(true)
      }
    })
    return () => {
      controller.abort()
      if (historyBootstrapAbortController.current === controller) {
        historyBootstrapAbortController.current = null
      }
      hasHistoryBootstrapStarted.current = false
    }
  }, [isHistoryBootstrapped, modelCatalogStatus, refreshHistoryList])

  const selectedConversation = workspace.conversations.find(
    (item) => item.threadId === workspace.currentThreadId,
  )
  const selectedThreadId = selectedConversation?.threadId
  const selectedIsHydrated = selectedConversation?.isHydrated
  const selectedRunStatus = selectedConversation?.runStatus
  const selectedRunId = selectedConversation?.activeRunId
  const selectedHydrationStatus = hydrationState?.threadId === selectedThreadId
    ? hydrationState?.status
    : undefined
  const selectedTaskTracePhase = selectedConversation?.taskTrace.phase
  const selectedTaskTraceFailure = taskTraceLoadFailures.get(workspace.currentThreadId)
  const selectedTaskTraceLoadFailed = Boolean(
    selectedTaskTraceFailure
    && ownsTaskTraceFailure(selectedConversation, selectedTaskTraceFailure.identity),
  )
  useEffect(() => {
    if (selectedThreadId && selectedIsHydrated && !selectedHydrationStatus && selectedRunStatus === 'detached') {
      void followDetachedConversation(selectedThreadId)
    }
  }, [followDetachedConversation, selectedHydrationStatus, selectedIsHydrated, selectedRunId, selectedRunStatus, selectedThreadId])
  useEffect(() => {
    if (taskTraceLoadFailures.size === 0) return
    const stale = new Map<string, number>()
    for (const [threadId, failure] of taskTraceLoadFailures) {
      const conversation = workspace.conversations.find((item) => item.threadId === threadId)
      if (!ownsTaskTraceFailure(conversation, failure.identity)) {
        stale.set(threadId, failure.requestId)
      }
    }
    if (stale.size === 0) return
    setTaskTraceLoadFailures((current) => {
      let next: Map<string, TaskTraceLoadFailure> | undefined
      for (const [threadId, requestId] of stale) {
        if (current.get(threadId)?.requestId !== requestId) continue
        next ??= new Map(current)
        next.delete(threadId)
      }
      return next ?? current
    })
  }, [taskTraceLoadFailures, workspace])
  useEffect(() => {
    if (!workspace.currentThreadId || !selectedThreadId) return
    const needsConversation = !selectedIsHydrated
    const needsTaskTrace = selectedIsHydrated
      && (
        selectedTaskTracePhase === 'unloaded'
        || selectedTaskTracePhase === 'loading'
      )
    if (!needsConversation && !needsTaskTrace) return
    const threadId = workspace.currentThreadId
    void hydrateConversation(threadId)
  }, [
    hydrateConversation,
    selectedIsHydrated,
    selectedTaskTracePhase,
    selectedThreadId,
    workspace.currentThreadId,
  ])

  useEffect(() => {
    const currentThreadId = workspace.currentThreadId
    const threadIds = new Set(workspace.conversations.map((item) => item.threadId))
    for (const threadId of prefetchedHistoryDetails.current.keys()) {
      if (threadId !== currentThreadId || !threadIds.has(threadId)) {
        prefetchedHistoryDetails.current.delete(threadId)
      }
    }
    for (const [threadId, controller] of olderTraceRequests.current) {
      if (threadId === currentThreadId && threadIds.has(threadId)) continue
      controller.abort()
      olderTraceRequests.current.delete(threadId)
    }
    for (const [threadId, request] of hydrationRequests.current) {
      if (threadId === currentThreadId && threadIds.has(threadId)) continue
      request.controller.abort()
      hydrationRequests.current.delete(threadId)
    }
    for (const [threadId, controller] of taskTraceRequests.current) {
      if (threadId === currentThreadId && threadIds.has(threadId)) continue
      controller.abort()
      taskTraceRequests.current.delete(threadId)
    }
    setHydrationState((current) => (
      current && current.threadId !== currentThreadId ? null : current
    ))
  }, [workspace.currentThreadId, workspace.conversations])

  useEffect(() => () => {
    if (historySearchDebounceTimer.current != null) window.clearTimeout(historySearchDebounceTimer.current)
    historyAbortController.current?.abort()
    historySearchAbortController.current?.abort()
    historyBootstrapAbortController.current?.abort()
    for (const request of hydrationRequests.current.values()) request.controller.abort()
    hydrationRequests.current.clear()
    prefetchedHistoryDetails.current.clear()
    for (const controller of olderTraceRequests.current.values()) controller.abort()
    olderTraceRequests.current.clear()
    for (const controller of taskTraceRequests.current.values()) controller.abort()
    taskTraceRequests.current.clear()
  }, [])

  const historyRefresh = useMemo<HistoryActivationRefresh | undefined>(() => {
    if (!activation) return undefined
    const selectedHydration = hydrationState?.threadId === activation.threadId ? hydrationState : undefined
    return {
      epoch: activation.foreground,
      phase: settledActivation !== activation || selectedHydration?.status === 'loading'
        ? 'pending'
        : selectedHydration?.status === 'failed'
          ? selectedHydration.unavailable ? 'unavailable' : 'failed'
          : 'ready',
    }
  }, [activation, hydrationState, settledActivation])

  return {
    historyConversations,
    historyDayRanges,
    historyQuery,
    setHistoryQuery,
    isHistorySearchActive: Boolean(normalizedHistoryQuery),
    isHistorySearching,
    historyCursor: activeHistoryCursor,
    isHistoryLoadingMore: activeHistoryLoadingMore,
    historyLoadError: activeHistoryLoadError,
    isHistoryBootstrapped,
    historyBootstrapStatus,
    hydrationState,
    historyRefresh,
    recheckConversationHistory,
    taskTraceLoadFailed: selectedTaskTraceLoadFailed,
    loadMoreHistory,
    retryHistoryLoad,
    retryHistoryBootstrap,
    hydrateConversation,
    hydrateTaskTrace,
    retryTaskTrace,
    loadOlderTrace,
  }
}
