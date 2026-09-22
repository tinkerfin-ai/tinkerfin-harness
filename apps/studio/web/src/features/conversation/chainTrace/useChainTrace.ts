import { useEffect, useMemo, useRef, useState } from 'react'

import {
  followTraceGraph,
  parseTraceGraphPage,
  queryTraceGraph,
  type TraceGraphDelta,
  type TraceGraphFilter,
  type TraceGraphPage,
} from '../../../api/conversation/traceGraph'
import { ConversationError } from '../../../api/conversation/errors'

export type ChainTraceState =
  | { phase: 'idle' }
  | { phase: 'loading' }
  | { phase: 'ready'; page: TraceGraphPage }
  | { phase: 'error' }

const GRAPH_RENDER_INTERVAL_MS = 50

const sameIds = (left: readonly string[], right: readonly string[]) => (
  left.length === right.length && left.every((id, index) => id === right[index])
)

const applyUpdate = (
  page: TraceGraphPage,
  update: TraceGraphDelta,
): TraceGraphPage => {
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
  return parseTraceGraphPage({
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
  observedAt,
  waitingForHistory = false,
  filter,
  limit,
}: {
  threadId: string
  active: boolean
  live: boolean
  /** 会话权威观测到达后，重新读取终态链路以补齐最后提交的节点 */
  observedAt?: string
  /** 先重验所选会话，再读取非实时链路；等待期间保留同一查询的展示 */
  waitingForHistory?: boolean
  filter: TraceGraphFilter
  limit: number
}) {
  const [retryEpoch, setRetryEpoch] = useState(0)
  const [visible, setVisible] = useState(document.visibilityState !== 'hidden')
  const [state, setState] = useState<ChainTraceState>({ phase: 'idle' })
  const filterKey = useMemo(() => JSON.stringify(filter), [filter])
  const currentFilter = useRef(filter)
  currentFilter.current = filter
  const snapshotObservedAt = live ? undefined : observedAt
  const snapshotWaiting = !live && waitingForHistory
  const displayedQuery = useRef<string | null>(null)

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
    const resolvedFilter = currentFilter.current
    const queryKey = JSON.stringify([threadId, filterKey, limit])
    const sameQuery = displayedQuery.current === queryKey
    displayedQuery.current = queryKey
    // 同一查询刷新时保留阅读内容，新快照到达后再替换；查询范围变化则显示加载状态
    setState(current => sameQuery && current.phase === 'ready' ? current : { phase: 'loading' })
    if (snapshotWaiting) return
    const controller = new AbortController()
    let disposed = false
    let currentPage: TraceGraphPage | undefined
    let publishTimer: number | undefined
    const clearPublishTimer = () => {
      window.clearTimeout(publishTimer)
      publishTimer = undefined
    }
    const fail = () => {
      clearPublishTimer()
      controller.abort()
      setState({ phase: 'error' })
    }
    const schedulePublish = () => {
      if (publishTimer !== undefined) return
      // 增量逐个校验，仅合并页面展示，避免重算未变化的链路布局
      publishTimer = window.setTimeout(() => {
        publishTimer = undefined
        if (!disposed && !controller.signal.aborted && currentPage) {
          setState({ phase: 'ready', page: currentPage })
        }
      }, GRAPH_RENDER_INTERVAL_MS)
    }
    const load = async () => {
      try {
        if (!live) {
          const page = await queryTraceGraph(threadId, resolvedFilter, {
            limit,
            signal: controller.signal,
          })
          if (!disposed && !controller.signal.aborted) setState({ phase: 'ready', page })
          return
        }
        for await (const event of followTraceGraph(threadId, resolvedFilter, {
          limit,
          signal: controller.signal,
        })) {
          if (disposed || controller.signal.aborted) return
          if (event.type === 'snapshot') {
            if (currentPage) throw new ConversationError('stream_event_invalid')
            currentPage = event.snapshot
            setState({ phase: 'ready', page: event.snapshot })
          } else if (event.type === 'update') {
            if (!currentPage) throw new ConversationError('stream_event_invalid')
            currentPage = applyUpdate(currentPage, event.update)
            schedulePublish()
          } else {
            fail()
            return
          }
        }
        if (!disposed && !controller.signal.aborted) {
          fail()
        }
      } catch {
        if (!disposed) {
          fail()
        }
      }
    }
    // StrictMode 会先同步重放 setup/cleanup；只让仍存活的 Effect 发起读取
    queueMicrotask(() => {
      if (!disposed) void load()
    })
    return () => {
      disposed = true
      clearPublishTimer()
      controller.abort()
    }
  }, [active, filterKey, limit, live, retryEpoch, snapshotObservedAt, snapshotWaiting, threadId, visible])

  return {
    state,
    retry: () => setRetryEpoch((value) => value + 1),
  }
}
