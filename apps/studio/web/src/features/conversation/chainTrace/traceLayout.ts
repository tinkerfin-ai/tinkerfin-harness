import type {
  TraceGraphNode,
  TraceGraphTurn,
} from '../../../api/conversation/traceGraph'
import {
  compareTraceGraphIds,
  compareTraceGraphNodes,
} from '../../../api/conversation/traceGraph'
import {
  traceVisualCategory,
  type TracePublicCategory,
} from './tracePresentation'

export type TraceTimelineLaneId = TracePublicCategory

export interface TraceTimelineBar {
  node: TraceGraphNode
  track: number
  startMilliseconds: number
  endMilliseconds: number
  leftPercent: number
  widthPercent: number
}

export interface TraceTimelineLane {
  id: TraceTimelineLaneId
  trackCount: number
  bars: TraceTimelineBar[]
}

export interface TraceTimelineLayout {
  durationMilliseconds: number
  ticks: number[]
  lanes: TraceTimelineLane[]
}

export interface TraceSequenceItem {
  node: TraceGraphNode
  column: number
}

export interface TraceSequenceLane {
  id: TraceTimelineLaneId
  items: TraceSequenceItem[]
}

export interface TraceSequenceLayout {
  columnCount: number
  lanes: TraceSequenceLane[]
  turnBoundaries: number[]
}

export interface TraceTurnRows {
  turn: TraceGraphTurn
  nodes: TraceGraphNode[]
}

const LANE_ORDER: readonly TraceTimelineLaneId[] = [
  'user',
  'context',
  'model',
  'tool',
  'subagent',
  'assistant',
]

const nodeTimes = (node: TraceGraphNode) => {
  const start = Date.parse(node.startedAt)
  const end = node.completedAt
    ? Date.parse(node.completedAt)
    : node.firstOutputAt ? Date.parse(node.firstOutputAt) : start
  return { start, end: Math.max(start, end) }
}

export const traceParentId = (node: TraceGraphNode | undefined) => node?.parentNodeId ?? node?.parentSubagentId

/** 压缩与所属上下文合并展示，模型仍保留独立详情 */
export const foldCompactionNodes = (nodes: readonly TraceGraphNode[]) => {
  const byId = new Map(nodes.map(node => [node.id, node]))
  const foldedIds = new Map(nodes.flatMap(node => (
    node.kind === 'custom' && node.contextKind === 'compaction'
    && node.parentNodeId && byId.get(node.parentNodeId)?.kind === 'context'
      ? [[node.id, node.parentNodeId] as const] : []
  )))
  return {
    foldedIds,
    nodes: nodes.filter(node => !foldedIds.has(node.id)).map(node => {
      const owner = node.parentNodeId && foldedIds.get(node.parentNodeId)
      return owner ? { ...node, parentNodeId: owner } : node
    }),
  }
}

export const groupTraceNodesByTurn = (
  turns: readonly TraceGraphTurn[],
  nodes: readonly TraceGraphNode[],
): TraceTurnRows[] => {
  const byTurn = new Map<string, TraceGraphNode[]>()
  nodes.forEach((node) => byTurn.set(node.turnId, [
    ...(byTurn.get(node.turnId) ?? []),
    node,
  ]))
  return [...turns]
    .sort((left, right) => (
      left.ordinal - right.ordinal || compareTraceGraphIds(left.id, right.id)
    ))
    .flatMap((turn) => {
      const values = byTurn.get(turn.id)
      if (!values?.length) return []
      const byId = new Map(values.map(node => [node.id, node]))
      const children = new Map<string, TraceGraphNode[]>()
      const roots: TraceGraphNode[] = []
      values.forEach(node => {
        const parent = traceParentId(node)
        if (parent && byId.has(parent)) children.set(parent, [...(children.get(parent) ?? []), node])
        else roots.push(node)
      })
      const ordered: TraceGraphNode[] = []
      const visit = (node: TraceGraphNode) => {
        ordered.push(node)
        children.get(node.id)?.forEach(visit)
      }
      roots.forEach(visit)
      return [{ turn, nodes: ordered }]
    })
}

/** 根据可见层级延续祖先主线，最后一条子分支在节点处收尾 */
export const traceRailAncestors = (nodes: readonly TraceGraphNode[]) => {
  const lastByParent = new Map<string | null, string>()
  nodes.forEach(node => lastByParent.set(traceParentId(node) ?? null, node.id))
  const paths = new Map<string, TraceGraphNode[]>()
  const rails = new Map<string, { ancestorLevels: number[]; continues: boolean }>()
  nodes.forEach(node => {
    const parentId = traceParentId(node)
    const ancestors = parentId ? paths.get(parentId) ?? [] : []
    const path = [...ancestors, node]
    paths.set(node.id, path)
    const hasLaterSibling = (item: TraceGraphNode) => (
      item.id !== lastByParent.get(traceParentId(item) ?? null)
    )
    rails.set(node.id, {
      ancestorLevels: ancestors.flatMap((ancestor, level) => hasLaterSibling(ancestor) ? [level] : []),
      continues: hasLaterSibling(node),
    })
  })
  return rails
}

export const buildTraceSequenceLayout = (
  turns: readonly TraceGraphTurn[],
  nodes: readonly TraceGraphNode[],
): TraceSequenceLayout => {
  const rows = groupTraceNodesByTurn(turns, nodes.filter(node => !node.parentNodeId))
  const positioned: TraceSequenceItem[] = []
  const turnBoundaries: number[] = []
  rows.forEach(({ nodes: turnNodes }, turnIndex) => {
    turnNodes.forEach((node) => positioned.push({
      node,
      column: positioned.length + 1,
    }))
    if (turnIndex < rows.length - 1) turnBoundaries.push(positioned.length)
  })
  return {
    columnCount: positioned.length,
    lanes: LANE_ORDER.flatMap((id) => {
      const items = positioned.filter((item) => (
        traceVisualCategory(item.node.kind) === id
      ))
      return items.length > 0 ? [{ id, items }] : []
    }),
    turnBoundaries,
  }
}

export const buildTraceTimelineLayout = (
  turns: readonly TraceGraphTurn[],
  nodes: readonly TraceGraphNode[],
): TraceTimelineLayout | null => {
  if (nodes.length === 0) return null
  let activeOffset = 0
  const timed = groupTraceNodesByTurn(turns, nodes.filter(node => !node.parentNodeId)).flatMap(({ turn, nodes: turnNodes }) => {
    const turnStart = Date.parse(turn.startedAt)
    const values = turnNodes.map((node) => ({ node, ...nodeTimes(node) }))
    const turnEnd = values.reduce(
      (latest, item) => Math.max(latest, item.end),
      turnStart,
    )
    const positioned = values.map(({ node, start, end }) => {
      const relativeStart = Math.max(0, start - turnStart)
      const relativeEnd = Math.max(relativeStart, end - turnStart)
      return {
        node,
        start: activeOffset + relativeStart,
        end: activeOffset + relativeEnd,
      }
    })
    activeOffset += Math.max(0, turnEnd - turnStart)
    return positioned
  })
  const durationMilliseconds = Math.max(1, activeOffset)
  const grouped = new Map<TraceTimelineLaneId, typeof timed>()
  timed.forEach((item) => {
    const lane = traceVisualCategory(item.node.kind)
    grouped.set(lane, [...(grouped.get(lane) ?? []), item])
  })
  const lanes = LANE_ORDER.flatMap((id) => {
    const values = grouped.get(id)
    if (!values?.length) return []
    const trackEnds: number[] = []
    const bars = [...values]
      .sort((left, right) => (
        left.start - right.start
        || compareTraceGraphNodes(left.node, right.node)
      ))
      .map(({ node, start, end }) => {
        const occupiedUntil = Math.max(start + 1, end)
        let track = trackEnds.findIndex((value) => value <= start)
        if (track < 0) track = trackEnds.length
        trackEnds[track] = occupiedUntil
        return {
          node,
          track,
          startMilliseconds: start,
          endMilliseconds: end,
          leftPercent: start / durationMilliseconds * 100,
          widthPercent: (end - start) / durationMilliseconds * 100,
        }
      })
    return [{ id, bars, trackCount: trackEnds.length } satisfies TraceTimelineLane]
  })
  return {
    durationMilliseconds,
    ticks: Array.from({ length: 7 }, (_, index) => durationMilliseconds * index / 6),
    lanes,
  }
}

const latest = (nodes: readonly TraceGraphNode[]) => nodes.reduce<TraceGraphNode | undefined>(
  (current, node) => !current || compareTraceGraphNodes(current, node) < 0
    ? node
    : current,
  undefined,
)

export const preferredTraceNode = (
  nodes: readonly TraceGraphNode[],
): TraceGraphNode | undefined => {
  const selected = latest(nodes.filter(node => node.failure || node.status === 'failed')) ?? latest(nodes)
  return nodes.find(node => node.id === selected?.parentNodeId && node.contextKind === 'compaction') ?? selected
}
