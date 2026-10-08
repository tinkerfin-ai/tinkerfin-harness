import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import type { Dispatch, SetStateAction } from 'react'
import { watchResource } from '../../api/shared/watchResource'

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
import { useConversationTitle } from './useConversationTitle'

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

interface HydrationRequestIdentity extends TaskTraceRequestIdentity {
  isHydrated: Conversation['isHydrated']
}

interface HydrationRequest {
  identity: HydrationRequestIdentity
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

const matchesTraceRequestIdentity = (
  conversation: Conversation | undefined,
  request: TaskTraceRequestIdentity,
) => (
  conversation != null
  && conversation.trace?.asOfSeq === request.traceAsOfSeq
  && conversation.trace?.headRunId === request.traceHeadRunId
  && conversation.runStatus === request.runStatus
  && conversation.activeRunId === request.activeRunId
  && conversation.lastSeq === request.lastSeq
)

const matchesTaskTraceRequestIdentity = (
  conversation: Conversation | undefined,
  request: TaskTraceRequestIdentity,
) => conversation?.isHydrated === true && matchesTraceRequestIdentity(conversation, request)

const ownsHydrationRequest = (
  conversation: Conversation | undefined,
  request: HydrationRequestIdentity,
) => conversation?.isHydrated === request.isHydrated && matchesTraceRequestIdentity(conversation, request)

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
    projectId: detail.projectId,
    archived: detail.archived,
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
  projectId: item.projectId,
  archived: item.archived,
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
    // 同一主运行的确定终态不会重新执行；新主运行和未终止的审批状态仍需重验
    const sameTerminalHead = existing.trace
      && item.lastRunId === existing.trace.headRunId
      && ['succeeded', 'failed', 'cancelled', 'abandoned'].includes(existing.trace.status.execution)
    // 浏览器正在接收的流优先；其他未确认终态的详情按摘要新鲜度重验
    const advanceHydratedRuntime = Boolean(
      existing.isHydrated
      && existing.runStatus !== 'streaming'
      && !sameTerminalHead
      && summaryIsNotOlder
      && (headChanged || pendingChanged || statusChanged || summaryTime > existingTime),
    )
    const preserveRuntime = existing.runStatus === 'streaming'
      || Boolean(existing.isHydrated && !advanceHydratedRuntime)
    byId.set(item.threadId, {
      ...existing,
      ...mergeConversationTitle(existing, item),
      projectId: item.projectId,
      archived: item.archived,
      pinned: item.pinned,
      updatedAt: summaryIsNotOlder ? item.updatedAt : existing.updatedAt,
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
  projectId,
  archived,
  searchScope,
  searchOpen,
  preferDraft = false,
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
  projectId: string
  archived: boolean
  searchScope: 'project' | 'all'
  searchOpen: boolean
  preferDraft?: boolean
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
  const searchEnabled = searchOpen && normalizedHistoryQuery.length > 0
  const searchCriteria = useMemo(() => ({ projectId, archived, scope: searchScope, query: normalizedHistoryQuery }), [projectId, archived, searchScope, normalizedHistoryQuery])
  const [settledSearch, setSettledSearch] = useState<typeof searchCriteria | null>(null)
  const normalizedHistoryQueryRef = useRef(normalizedHistoryQuery)
  normalizedHistoryQueryRef.current = normalizedHistoryQuery
  const historyWatch = useRef<ReturnType<typeof watchResource> | null>(null)
  const historyRefreshing = useRef<AbortSignal | null>(null)
  const historyPageRequest = useRef<Promise<void> | null>(null)
  const historyExcludedIds = useRef(new Set<string>())
  const searchRefreshing = useRef<AbortSignal | null>(null)
  const searchPageRequest = useRef<Promise<void> | null>(null)
  const searchThreadIdsRef = useRef<string[]>([])
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
  const initialSelection = useRef<{
    threadId: string
    status: 'pending' | 'applied' | 'cancelled'
  }>({ threadId: readThreadFromLocation(), status: 'pending' })
  const historyInitialized = useRef(false)
  const latestWorkspace = useRef(workspace)
  const latestToast = useRef(onToast)
  latestToast.current = onToast
  const notifiedTaskTraceRequest = useRef<number | null>(null)
  latestWorkspace.current = workspace

  const listScope = useRef({ projectId, archived })
  useLayoutEffect(() => {
    if (listScope.current.projectId === projectId && listScope.current.archived === archived) return
    listScope.current = { projectId, archived }
    historyWatch.current?.close()
    historyWatch.current = null
    historyAbortController.current?.abort()
    historyAbortController.current = null
    historyPageRequest.current = null
    historyInFlightCursor.current = null
    historyLoadingRef.current = false
    historyRefreshing.current = null
    historyLastLoadSettledAt.current = null
    loadedHistoryCursors.current.clear()
    historyExcludedIds.current.clear()
    historyThreadIdsRef.current = []
    historyInitialized.current = false
    setHistoryThreadIds([])
    setHistoryCursor(null)
    setHistoryLoadingMore(false)
    setHistoryLoadError(null)
    setHistoryBootstrapStatus('loading')
  }, [projectId, archived])

  // 明确导航立即取消首屏恢复权，包括空白页再次点击新会话
  const cancelInitialSelection = useCallback(() => {
    initialSelection.current.status = 'cancelled'
  }, [])

  useEffect(() => {
    const failure = taskTraceLoadFailures.get(workspace.currentThreadId)
    const current = workspace.conversations.find((item) => item.threadId === workspace.currentThreadId)
    if (!failure || !ownsTaskTraceFailure(current, failure.identity)
      || notifiedTaskTraceRequest.current === failure.requestId) return
    notifiedTaskTraceRequest.current = failure.requestId
    latestToast.current('error', t('任务轨迹不可用'))
  }, [taskTraceLoadFailures, workspace, t])

  const refreshHistoryList = useCallback(async (
    options: { signal?: AbortSignal; preserveWindow?: boolean } = {},
  ) => {
    const selection = initialSelection.current
    const initialize = selection.status === 'pending'
    const selectedAtStart = latestWorkspace.current.currentThreadId
    const preferredThreadId = initialize ? selection.threadId : ''
    const retention = preferredThreadId ? retainConversationDetails(preferredThreadId) : undefined
    try {
      const [response, preferredDetail, groupConfig] = await Promise.all([
        (async () => {
          const pageCount = options.preserveWindow ? Math.max(1, Math.ceil(historyThreadIdsRef.current.length / HISTORY_PAGE_SIZE)) : 1
          const items: ConversationHistoryListItem[] = []
          let cursor: string | undefined
          let nextCursor: string | null = null
          for (let index = 0; index < pageCount; index += 1) {
            const page = await fetchConversationHistoryList({ projectId, archived, pageSize: HISTORY_PAGE_SIZE, cursor, signal: options.signal, suppressGlobalError: options.preserveWindow })
            items.push(...page.items)
            nextCursor = page.nextCursor ?? null
            if (!nextCursor || options.signal?.aborted) break
            cursor = nextCursor
          }
          return { items, nextCursor }
        })(),
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
      if (preferredDetail && preferredDetail.projectId !== projectId) throw new Error('会话不属于当前项目')
      const applyInitialSelection = initialize && selection.status === 'pending'
        && latestWorkspace.current.currentThreadId === selectedAtStart
      // 首次失败保留恢复机会；成功后通知和目录刷新只能更新列表
      if (initialize && selection.status === 'pending') selection.status = 'applied'
      const activeSession = readActiveRunSession(preferredThreadId, projectId)
      const preferredExists = preferredThreadId
        ? Boolean(preferredDetail || response.items.some((item) => item.threadId === preferredThreadId))
        : true
      if (
        activeSession && applyInitialSelection && (
          (response.items.length === 0 && !preferredDetail)
          || (!preferredExists && activeSession?.threadId === preferredThreadId)
        )
      ) clearActiveRunSession(activeSession.payload.runId)
      if (preferredDetail && applyInitialSelection) prefetchedHistoryDetails.current.set(preferredThreadId, preferredDetail)
      const historyItems = preferredDetail && !response.items.some((item) => item.threadId === preferredThreadId)
        ? [...response.items, historyItemFromDetail(preferredDetail)]
        : response.items
      const nextThreadIds = historyItems.map((item) => item.threadId)
      const listedThreadIds = new Set(nextThreadIds)
      if (options.preserveWindow) {
        for (const threadId of historyThreadIdsRef.current) {
          if (!listedThreadIds.has(threadId)) historyExcludedIds.current.add(threadId)
        }
      }
      historyThreadIdsRef.current = nextThreadIds
      setHistoryThreadIds(nextThreadIds)
      for (const threadId of nextThreadIds) {
        searchOnlyThreadIds.current.delete(threadId)
        historyExcludedIds.current.delete(threadId)
      }
      loadedHistoryCursors.current.clear()
      historyLastLoadSettledAt.current = null
      setHistoryCursor(response.nextCursor ?? null)
      setHistoryDayRanges(groupConfig.dayRanges)
      setWorkspace((state) => {
        if (options.signal?.aborted) return state
        const conversations = mergeHistoryConversations(
          state.conversations,
          historyItems,
          defaultModelId,
        )
        if (!applyInitialSelection || selection.status === 'cancelled'
          || state.currentThreadId !== selectedAtStart) {
          return { ...state, conversations }
        }
        const hasPreferred = preferredThreadId
          ? conversations.some((item) => item.threadId === preferredThreadId)
          : false
        const hasCurrent = state.currentThreadId
          ? conversations.some((item) => item.threadId === state.currentThreadId)
          : false
        const firstHistoryConversation = conversations.find(item => (
          listedThreadIds.has(item.threadId)
          && item.projectId === projectId
          && item.archived === archived
        ))
        return selectCurrentConversation(
          { conversations, currentThreadId: state.currentThreadId },
          hasPreferred
            ? preferredThreadId
            : hasCurrent
              ? state.currentThreadId
              : preferDraft ? '' : (firstHistoryConversation?.threadId ?? ''),
        )
      })
      return true
    } catch {
      return false
    } finally {
      retention?.release()
    }
  }, [projectId, archived, preferDraft, defaultModelId, prepareTaskTraceOwner, retainConversationDetails, setWorkspace])

  useEffect(() => {
    const known = new Set(historyThreadIdsRef.current)
    const additions = sortConversations(workspace.conversations)
      .filter(conversation => conversation.projectId === projectId && conversation.archived === archived)
      .map((conversation) => conversation.threadId)
      .filter((threadId) => (
        threadId
        && !known.has(threadId)
        && !historyExcludedIds.current.has(threadId)
        && !searchOnlyThreadIds.current.has(threadId)
      ))
    if (additions.length === 0) return
    const next = prependUniqueThreadIds(historyThreadIdsRef.current, additions)
    historyThreadIdsRef.current = next
    setHistoryThreadIds(next)
  }, [projectId, archived, workspace.conversations])

  useEffect(() => {
    const threadId = workspace.currentThreadId
    if (searchOpen || !threadId || !searchOnlyThreadIds.current.has(threadId)) return
    searchOnlyThreadIds.current.delete(threadId)
    const next = prependUniqueThreadIds(historyThreadIdsRef.current, [threadId])
    historyThreadIdsRef.current = next
    setHistoryThreadIds(next)
  }, [searchOpen, workspace.currentThreadId])

  useEffect(() => {
    historySearchGeneration.current += 1
    if (historySearchDebounceTimer.current != null) {
      window.clearTimeout(historySearchDebounceTimer.current)
      historySearchDebounceTimer.current = null
    }
    historySearchLastLoadSettledAt.current = null
    historySearchAbortController.current?.abort()
    historySearchAbortController.current = null
    searchPageRequest.current = null
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
    setSettledSearch(null)
    searchThreadIdsRef.current = []
    setSearchThreadIds([])
    setSearchCursor(null)
    setSearchLoadError(null)
    setSearchLoadingMore(false)

    if (!searchEnabled) {
      setHistorySearching(false)
      return
    }

    setHistorySearching(true)
    let watch: ReturnType<typeof watchResource> | undefined
    historySearchDebounceTimer.current = window.setTimeout(() => {
      historySearchDebounceTimer.current = null
      watch = watchResource({
        matches: change => change.topic === 'studio.conversation.changed' || change.topic === 'studio.conversation.title.changed',
        read: async signal => {
          searchRefreshing.current = signal
          try {
            await searchPageRequest.current
            if (signal.aborted) throw signal.reason
            const items: ConversationHistoryListItem[] = []
            const count = Math.max(1, Math.ceil(searchThreadIdsRef.current.length / HISTORY_PAGE_SIZE))
            let cursor: string | undefined
            let nextCursor: string | null = null
            for (let index = 0; index < count; index += 1) {
              const response = await fetchConversationHistoryList({ projectId, archived,
                pageSize: HISTORY_PAGE_SIZE, query: normalizedHistoryQuery, scope: searchScope, cursor, signal, suppressGlobalError: true,
              })
              items.push(...response.items)
              nextCursor = response.nextCursor ?? null
              if (!nextCursor || signal.aborted) break
              cursor = nextCursor
            }
            return { items, nextCursor }
          } finally {
            if (searchRefreshing.current === signal) searchRefreshing.current = null
          }
        },
        update: (response, signal) => {
          setSettledSearch(searchCriteria)
          const normalIds = new Set(historyThreadIdsRef.current)
          for (const item of response.items) {
            if (!normalIds.has(item.threadId)) searchOnlyThreadIds.current.add(item.threadId)
          }
          const nextThreadIds = response.items.map(item => item.threadId)
          searchThreadIdsRef.current = nextThreadIds
          setSearchThreadIds(nextThreadIds)
          setSearchCursor(response.nextCursor)
          loadedSearchCursors.current.clear()
          setHistorySearching(false)
          setSearchLoadError(null)
          setWorkspace(state => signal.aborted ? state : ({
            ...state,
            conversations: mergeHistoryConversations(state.conversations, response.items, defaultModelId),
          }))
        },
        onError: () => {
          setSettledSearch(searchCriteria)
          setHistorySearching(false)
          setSearchLoadError(t('搜索会话失败'))
        },
      })
    }, HISTORY_SEARCH_DEBOUNCE_MS)

    return () => {
      if (historySearchDebounceTimer.current != null) {
        window.clearTimeout(historySearchDebounceTimer.current)
        historySearchDebounceTimer.current = null
      }
      watch?.close()
      historySearchAbortController.current?.abort()
      historySearchAbortController.current = null
    }
  }, [projectId, archived, searchScope, defaultModelId, normalizedHistoryQuery, searchCriteria, searchEnabled, searchRetryVersion, setWorkspace, t])

  const retryHistoryBootstrap = useCallback(() => {
    setHistoryBootstrapStatus('loading')
    historyWatch.current?.refresh()
  }, [])

  const loadMoreNormalHistory = useCallback((isExplicitRetry = false) => {
    if (historyLoadingRef.current || historyRefreshing.current) return
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
    historyPageRequest.current = fetchConversationHistoryList({ projectId, archived,
      pageSize: HISTORY_PAGE_SIZE,
      cursor,
      signal: controller.signal,
      suppressGlobalError: true,
    }).then((response) => {
      if (controller.signal.aborted || historyAbortController.current !== controller) return
      historyLastLoadSettledAt.current = Date.now()
      loadedHistoryCursors.current.add(cursor)
      const incomingIds = response.items.map((item) => item.threadId)
      const nextThreadIds = appendUniqueThreadIds(
        historyThreadIdsRef.current,
        incomingIds,
      )
      historyThreadIdsRef.current = nextThreadIds
      setHistoryThreadIds(nextThreadIds)
      for (const threadId of incomingIds) {
        searchOnlyThreadIds.current.delete(threadId)
        historyExcludedIds.current.delete(threadId)
      }
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
      if (!controller.signal.aborted && historyAbortController.current === controller) {
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
      if (historyAbortController.current !== controller) return
      historyAbortController.current = null
      historyInFlightCursor.current = null
      historyLoadingRef.current = false
      if (!controller.signal.aborted) setHistoryLoadingMore(false)
    })
  }, [projectId, archived, defaultModelId, historyCursor, historyLoadError, setWorkspace, t])

  const loadMoreSearchHistory = useCallback((isExplicitRetry = false) => {
    if (
      historySearchLoadingRef.current || searchRefreshing.current
    ) return
    if (searchLoadError && !isExplicitRetry) return
    const cursor = searchCursor
    const query = normalizedHistoryQuery
    if (
      !searchEnabled
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
    searchPageRequest.current = fetchConversationHistoryList({ projectId, archived,
      pageSize: HISTORY_PAGE_SIZE,
      cursor,
      query,
      scope: searchScope,
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
      const nextThreadIds = appendUniqueThreadIds(
        searchThreadIdsRef.current,
        response.items.map((item) => item.threadId),
      )
      // 通知刷新等待分页后需要立即读到完整窗口
      searchThreadIdsRef.current = nextThreadIds
      setSearchThreadIds(nextThreadIds)
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
      }
    }).finally(() => {
      if (historySearchAbortController.current !== controller) return
      historySearchAbortController.current = null
      historySearchInFlightCursor.current = null
      historySearchLoadingRef.current = false
      if (!controller.signal.aborted) setSearchLoadingMore(false)
    })
  }, [projectId, archived, searchScope, defaultModelId, normalizedHistoryQuery, searchEnabled, searchCursor, searchLoadError, setWorkspace, t])

  const retryHistoryLoad = useCallback(() => loadMoreNormalHistory(true), [loadMoreNormalHistory])

  const retryHistorySearch = useCallback(() => {
    if (!searchEnabled) return
    if (searchLoadError && searchCursor == null) {
      setSearchRetryVersion(value => value + 1)
      return
    }
    loadMoreSearchHistory(true)
  }, [loadMoreSearchHistory, searchEnabled, searchCursor, searchLoadError])

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
    if (existingRequest && !existingRequest.controller.signal.aborted
      && ownsHydrationRequest(target, existingRequest.identity)) {
      existingRequest.foreground = latestForeground.current
      return existingRequest.completion
    }
    if (existingRequest) {
      existingRequest.controller.abort()
      hydrationRequests.current.delete(threadId)
    }

    const retention = retainConversationDetails(threadId)
    // 初次读取同样绑定会话状态，实时流接管后旧详情不能覆盖正文或报告过期失败
    const requestIdentity: HydrationRequestIdentity = {
      ...taskTraceRequestIdentity(target), isHydrated: target.isHydrated,
    }
    const controller = new AbortController()
    setHydrationState({ threadId, status: 'loading' })
    const request: HydrationRequest = {
      identity: requestIdentity,
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
              || !ownsHydrationRequest(owner, requestIdentity)) return
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
              || !ownsHydrationRequest(current, requestIdentity)) return
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
                || !ownsHydrationRequest(previous, requestIdentity)) return state
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
            && ownsHydrationRequest(current, requestIdentity)) {
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

  // 搜索结果共享会话元数据，但不替换侧栏的历史窗口
  const historyConversations = useMemo(() => {
    const byId = new Map(workspace.conversations.map(item => [item.threadId, item]))
    return historyThreadIds.map(id => byId.get(id)).filter((item): item is Conversation =>
      item != null && item.projectId === projectId && item.archived === archived)
  }, [historyThreadIds, archived, projectId, workspace.conversations])
  const searchConversations = useMemo(() => {
    if (!searchEnabled || settledSearch !== searchCriteria) return []
    const byId = new Map(workspace.conversations.map(item => [item.threadId, item]))
    return searchThreadIds.map(id => byId.get(id)).filter((item): item is Conversation =>
      item != null && item.archived === archived && (searchScope === 'all' || item.projectId === projectId))
  }, [searchThreadIds, archived, projectId, searchScope, workspace.conversations, searchEnabled, settledSearch, searchCriteria])

  useEffect(() => {
    if (modelCatalogStatus === 'loading') return
    const watch = watchResource({
      matches: change => change.topic === 'studio.conversation.changed' || change.topic === 'studio.conversation.title.changed',
      read: async signal => {
        historyRefreshing.current = signal
        try {
          await historyPageRequest.current
          if (signal.aborted) return false
          if (!historyInitialized.current) setHistoryBootstrapStatus('loading')
          return await refreshHistoryList({
            signal,
            preserveWindow: historyInitialized.current,
          })
        } finally {
          if (historyRefreshing.current === signal) historyRefreshing.current = null
        }
      },
      update: succeeded => {
        historyInitialized.current = true
        setHistoryBootstrapStatus(succeeded ? 'ready' : 'error')
        setHistoryBootstrapped(true)
      },
    })
    historyWatch.current = watch
    return () => {
      watch.close()
      if (historyWatch.current === watch) historyWatch.current = null
    }
  }, [modelCatalogStatus, refreshHistoryList])

  const selectedConversation = workspace.conversations.find(
    (item) => item.threadId === workspace.currentThreadId,
  )
  const selectedThreadId = selectedConversation?.threadId
  // 普通列表和搜索结果都携带标题；仅当前会话不在两者中时单独保持更新
  useConversationTitle(
    isHistoryBootstrapped && selectedThreadId
      && !historyThreadIds.includes(selectedThreadId) && !searchThreadIds.includes(selectedThreadId)
      ? selectedThreadId : undefined,
    setWorkspace,
  )
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
    selectedRunId,
    selectedRunStatus,
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
    historyWatch.current?.close()
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
    cancelInitialSelection,
    historyConversations,
    historyDayRanges,
    historyQuery,
    setHistoryQuery,
    isHistorySearching: searchEnabled && (isHistorySearching || settledSearch !== searchCriteria),
    searchConversations,
    searchCursor,
    isSearchLoadingMore,
    searchLoadError: settledSearch === searchCriteria ? searchLoadError : null,
    loadMoreSearchHistory,
    retryHistorySearch,
    historyCursor,
    isHistoryLoadingMore,
    historyLoadError,
    isHistoryBootstrapped,
    historyBootstrapStatus,
    hydrationState,
    historyRefresh,
    recheckConversationHistory,
    taskTraceLoadFailed: selectedTaskTraceLoadFailed,
    loadMoreHistory: loadMoreNormalHistory,
    retryHistoryLoad,
    retryHistoryBootstrap,
    hydrateConversation,
    hydrateTaskTrace,
    retryTaskTrace,
    loadOlderTrace,
  }
}
