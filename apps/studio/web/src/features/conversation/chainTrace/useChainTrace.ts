import { useEffect, useMemo, useRef, useState } from 'react'

import {
  followTraceGraph,
  parseTraceGraphQueryPage,
  queryTraceGraph,
  type TraceGraphDelta,
  type TraceGraphFilter,
  type TraceGraphQueryPage,
} from '../../../api/conversation/traceGraph'
import { ConversationError } from '../../../api/conversation/errors'
import {
  isConversationUnavailable,
  type ConversationObservation,
  type HistoryActivationRefresh,
  type HistoryRefreshResult,
} from '../trace/historyRefresh'

export type ChainTraceState =
  | { phase: 'idle' }
  | { phase: 'loading' }
  | { phase: 'ready'; page: TraceGraphQueryPage }
  | { phase: 'error' }

const GRAPH_RENDER_INTERVAL_MS = 50

const sameIds = (left: readonly string[], right: readonly string[]) => (
  left.length === right.length && left.every((id, index) => id === right[index])
)

const sameObservation = (left: ConversationObservation | undefined, right: ConversationObservation | undefined) => (
  left?.generation === right?.generation && left?.headRunId === right?.headRunId && left?.asOfSeq === right?.asOfSeq
)

const applyUpdate = (
  page: TraceGraphQueryPage,
  update: TraceGraphDelta,
): TraceGraphQueryPage => {
  if (update.asOfSeq <= page.asOfSeq) {
    throw new ConversationError('stream_event_invalid')
  }
  if (update.turnUpserts.length === 0 && update.turnRemoves.length === 0
    && update.nodeUpserts.length === 0 && update.nodeRemoves.length === 0
    && sameIds(page.orderedNodeIds, update.orderedNodeIds)
    && sameIds(page.matchedNodeIds, update.matchedNodeIds)
  ) {
    return {
      ...page,
      asOfSeq: update.asOfSeq,
      nextCursor: update.nextCursor,
      completeness: update.completeness,
    }
  }
  const turns = new Map(page.turns.map((turn) => [turn.id, turn]))
  update.turnRemoves.forEach((turnId) => turns.delete(turnId))
  update.turnUpserts.forEach((turn) => {
    const current = turns.get(turn.id)
    if (current && (
      current.ordinal !== turn.ordinal
      || current.startedAt !== turn.startedAt
    )) throw new ConversationError('stream_event_invalid')
    turns.set(turn.id, turn)
  })
  const nodes = new Map(page.nodes.map((node) => [node.id, node]))
  update.nodeRemoves.forEach((nodeId) => nodes.delete(nodeId))
  update.nodeUpserts.forEach((node) => {
    const current = nodes.get(node.id)
    if (current && (
      node.updatedSeq < current.updatedSeq
      || node.turnId !== current.turnId
      || node.kind !== current.kind
      || node.name !== current.name
      || JSON.stringify(node.graphNamespace) !== JSON.stringify(current.graphNamespace)
    )) throw new ConversationError('stream_event_invalid')
    nodes.set(node.id, node)
  })
  const orderedNodes = update.orderedNodeIds.map((nodeId) => {
    const node = nodes.get(nodeId)
    if (!node) throw new ConversationError('stream_event_invalid')
    return node
  })
  if (orderedNodes.length !== nodes.size) {
    throw new ConversationError('stream_event_invalid')
  }
  return parseTraceGraphQueryPage({
    ...page,
    asOfSeq: update.asOfSeq,
    nextCursor: update.nextCursor,
    turns: [...turns.values()].sort((left, right) => (
      left.ordinal - right.ordinal || left.id.localeCompare(right.id)
    )),
    nodes: orderedNodes,
    orderedNodeIds: update.orderedNodeIds,
    matchedNodeIds: update.matchedNodeIds,
    completeness: update.completeness,
  })
}

export function useChainTrace({
  threadId,
  active,
  live,
  liveRunId,
  observation,
  historyRefresh,
  onRecheckHistory,
  filter,
  limit,
}: {
  threadId: string
  active: boolean
  live: boolean
  liveRunId?: string
  observation?: ConversationObservation
  historyRefresh?: HistoryActivationRefresh
  onRecheckHistory?: (threadId: string) => Promise<HistoryRefreshResult>
  filter: TraceGraphFilter
  limit: number
}) {
  const [retryEpoch, setRetryEpoch] = useState(0)
  const [runEpoch, setRunEpoch] = useState(0)
  const [visible, setVisible] = useState(document.visibilityState !== 'hidden')
  const [state, setState] = useState<ChainTraceState>({ phase: 'idle' })
  const [historyStatus, setHistoryStatus] = useState<HistoryActivationRefresh['phase']>('ready')
  const filterKey = useMemo(() => JSON.stringify(filter), [filter])
  const latest = useRef({ filter, observation, historyRefresh, onRecheckHistory })
  latest.current = { filter, observation, historyRefresh, onRecheckHistory }
  const displayedQuery = useRef<string | null>(null)
  const reconcileCurrent = useRef<(() => void) | undefined>(undefined)
  const retryHistory = useRef(false)
  const foregroundEpoch = historyRefresh?.epoch
  const liveIdentity = live ? liveRunId ?? observation?.headRunId : undefined

  useEffect(() => {
    const updateVisibility = () => setVisible(document.visibilityState !== 'hidden')
    document.addEventListener('visibilitychange', updateVisibility)
    return () => document.removeEventListener('visibilitychange', updateVisibility)
  }, [])

  useEffect(() => {
    if (!active || !visible || !threadId) {
      setState({ phase: 'idle' })
      return
    }
    const resolvedFilter = latest.current.filter
    const queryKey = JSON.stringify([threadId, filterKey, limit])
    const sameQuery = displayedQuery.current === queryKey
    displayedQuery.current = queryKey
    setState(current => sameQuery && current.phase === 'ready' ? current : { phase: 'loading' })
    setHistoryStatus(latest.current.historyRefresh?.phase ?? 'ready')
    const controller = new AbortController()
    let disposed = false
    let currentPage: TraceGraphQueryPage | undefined
    let publishTimer: number | undefined
    let reading = false
    let recheckingHistory = false
    let correctedGraph = false
    let recheckedHistory = false
    let acceptedObservation: ConversationObservation | undefined
    let recheckedObservation: { previous: ConversationObservation | undefined; current: ConversationObservation } | undefined
    const clearPublishTimer = () => {
      window.clearTimeout(publishTimer)
      publishTimer = undefined
    }
    const current = () => !disposed && !controller.signal.aborted
    const fail = (unavailable = false) => {
      clearPublishTimer()
      controller.abort()
      if (unavailable) setHistoryStatus('unavailable')
      setState({ phase: 'error' })
    }
    const schedulePublish = () => {
      if (publishTimer !== undefined) return
      // 增量逐个校验，仅合并页面展示，避免重算未变化的链路布局
      publishTimer = window.setTimeout(() => {
        publishTimer = undefined
        if (current() && currentPage) setState({ phase: 'ready', page: currentPage })
      }, GRAPH_RENDER_INTERVAL_MS)
    }
    const readGraph = async () => {
      if (reading || !current()) return
      reading = true
      try {
        const page = await queryTraceGraph(threadId, resolvedFilter, { limit, signal: controller.signal })
        if (!current()) return
        currentPage = page
        setState({ phase: 'ready', page })
        reading = false
        reconcile()
      } catch (error) {
        reading = false
        if (current()) fail(isConversationUnavailable(error))
      }
    }
    const recheckHistory = async (identityConflict: boolean) => {
      const recheck = latest.current.onRecheckHistory
      if (!recheck) return
      recheckedHistory = true
      recheckingHistory = true
      setHistoryStatus('pending')
      if (identityConflict) setState({ phase: 'loading' })
      const previous = latest.current.observation
      let result: HistoryRefreshResult
      try { result = await recheck(threadId) }
      catch (error) { result = { phase: isConversationUnavailable(error) ? 'unavailable' : 'failed' } }
      if (!current()) return
      recheckingHistory = false
      if (result.phase !== 'ready') {
        setHistoryStatus(result.phase)
        if (identityConflict || result.phase === 'unavailable') fail(result.phase === 'unavailable')
        return
      }
      recheckedObservation = { previous, current: result.observation }
      reconcile()
    }
    const reconcile = (): void => {
      if (!current()) return
      const refresh = latest.current.historyRefresh
      if (refresh?.phase === 'unavailable') { fail(true); return }
      if (recheckingHistory || refresh?.phase === 'pending') {
        setHistoryStatus('pending')
        return
      }
      if (refresh?.phase === 'failed') { setHistoryStatus('failed'); return }
      setHistoryStatus('ready')
      if (reading || !currentPage) return
      const authority = recheckedObservation && sameObservation(recheckedObservation.previous, latest.current.observation)
        ? recheckedObservation.current
        : latest.current.observation
      if (!authority) return
      if (authority.generation !== currentPage.generation) { fail(); return }
      if (live) return
      if (acceptedObservation && authority.headRunId !== acceptedObservation.headRunId) {
        // 已对齐后出现真实新运行，开启它自己的读取需求；校正结果不能重置本次预算
        disposed = true
        clearPublishTimer()
        controller.abort()
        setRunEpoch(value => value + 1)
        return
      }
      if (authority.headRunId === currentPage.headRunId && currentPage.asOfSeq >= authority.asOfSeq) {
        acceptedObservation = authority
        const page = currentPage
        setState(currentState => currentState.phase === 'ready' && currentState.page === currentPage
          ? currentState : { phase: 'ready', page })
        return
      }
      if (authority.headRunId !== currentPage.headRunId && !recheckedHistory && latest.current.onRecheckHistory) {
        void recheckHistory(true)
        return
      }
      // 每次激活、前台恢复、筛选或终态需求最多补查一次，不能由迟到结果循环续查
      if (correctedGraph) { fail(); return }
      correctedGraph = true
      setHistoryStatus('pending')
      void readGraph()
    }
    reconcileCurrent.current = reconcile
    const load = async () => {
      if (latest.current.historyRefresh?.phase === 'unavailable') { fail(true); return }
      if (retryHistory.current) {
        retryHistory.current = false
        void recheckHistory(false)
      }
      if (!live) { await readGraph(); return }
      try {
        for await (const event of followTraceGraph(threadId, resolvedFilter, { limit, signal: controller.signal })) {
          if (!current()) return
          if (event.type === 'snapshot') {
            if (currentPage) throw new ConversationError('stream_event_invalid')
            currentPage = event.snapshot
            setState({ phase: 'ready', page: event.snapshot })
            reconcile()
          } else if (event.type === 'update') {
            if (!currentPage) throw new ConversationError('stream_event_invalid')
            currentPage = applyUpdate(currentPage, event.update)
            schedulePublish()
          } else { fail(); return }
        }
        if (current()) fail()
      } catch (error) {
        if (current()) fail(isConversationUnavailable(error))
      }
    }
    // StrictMode 同步重放期间只让仍存活的请求归属发起读取
    queueMicrotask(() => { if (current()) void load() })
    return () => {
      disposed = true
      if (reconcileCurrent.current === reconcile) reconcileCurrent.current = undefined
      clearPublishTimer()
      controller.abort()
    }
  }, [active, filterKey, foregroundEpoch, limit, live, liveIdentity, retryEpoch, runEpoch, threadId, visible])

  useEffect(() => { reconcileCurrent.current?.() }, [historyRefresh, observation])

  return {
    state,
    historyStatus,
    retry: () => {
      const recheck = latest.current.onRecheckHistory
      if (latest.current.historyRefresh?.phase === 'unavailable' && recheck) {
        const owner = reconcileCurrent.current
        if (!owner) return
        // 失去会话访问权后，显式重试先重新确认历史；不能展示此前的图
        setHistoryStatus('pending')
        void recheck(threadId).then(result => {
          if (reconcileCurrent.current !== owner) return
          setHistoryStatus(result.phase)
          if (result.phase === 'ready') setRetryEpoch(value => value + 1)
        }, error => {
          if (reconcileCurrent.current === owner) {
            setHistoryStatus(isConversationUnavailable(error) ? 'unavailable' : 'failed')
          }
        })
        return
      }
      retryHistory.current = true
      setRetryEpoch(value => value + 1)
    },
  }
}
