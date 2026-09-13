import type {
  TraceGraph,
  TraceGraphDelta,
  TraceGraphNode,
  TraceGraphTurn,
} from '../api/conversation/traceGraph'
import {
  compareTraceGraphIds,
  compareTraceGraphNodes,
} from '../api/conversation/traceGraphOrder'

const completeness = () => ({
  callTrackingMissing: false,
  relationshipEvidenceMissing: false,
  detailsOmitted: false,
})

export const emptyTraceGraph = (asOfSeq: number): TraceGraph => ({
  turns: [],
  nodes: [],
  orderedNodeIds: [],
  matchedNodeIds: [],
  asOfSeq,
  completeness: completeness(),
})

export const emptyTraceGraphDelta = (asOfSeq: number): TraceGraphDelta => ({
  asOfSeq,
  nextCursor: null,
  turnUpserts: [],
  turnRemoves: [],
  nodeUpserts: [],
  nodeRemoves: [],
  orderedNodeIds: [],
  matchedNodeIds: [],
  completeness: completeness(),
})

export const traceGraphNode = (
  overrides: Partial<TraceGraphNode> & Pick<TraceGraphNode, 'id'>,
): TraceGraphNode => {
  const startedSeq = overrides.startedSeq ?? 1
  return {
    agui: null,
    turnId: 'turn-fixture',
    parentSubagentId: null,
    modelCallId: null,
    kind: 'tool',
    status: 'succeeded',
    name: 'tool',
    runId: 'run-fixture',
    graphNamespace: [],
    startedAt: '2026-08-28T00:00:00Z',
    completedAt: '2026-08-28T00:00:00Z',
    startedSeq,
    updatedSeq: overrides.updatedSeq ?? startedSeq,
    contentOmitted: false,
    toolCallOnly: false,
    requestOmitted: false,
    resultOmitted: false,
    linkIssues: [],
    ...overrides,
  }
}

const orderNodes = (turns: readonly TraceGraphTurn[], nodes: TraceGraphNode[]) => {
  const byTurn = new Map<string, TraceGraphNode[]>()
  nodes.forEach((node) => byTurn.set(node.turnId, [
    ...(byTurn.get(node.turnId) ?? []),
    node,
  ]))
  const ordered: TraceGraphNode[] = []
  turns.forEach((turn) => {
    const children = new Map<string | null, TraceGraphNode[]>()
    ;(byTurn.get(turn.id) ?? []).forEach((node) => {
      const owner = node.parentSubagentId ?? null
      children.set(owner, [...(children.get(owner) ?? []), node])
    })
    children.forEach((values) => values.sort(compareTraceGraphNodes))
    const stack = [...(children.get(null) ?? [])].reverse()
    while (stack.length > 0) {
      const node = stack.pop()
      if (!node) continue
      ordered.push(node)
      if (node.kind === 'subagent') {
        stack.push(...[...(children.get(node.id) ?? [])].reverse())
      }
    }
  })
  if (ordered.length !== nodes.length) throw new Error('invalid Trace fixture scope')
  return ordered
}

export const traceGraphWithNodes = (
  nodes: TraceGraphNode[],
  asOfSeq: number,
): TraceGraph => {
  const turnStarts = new Map<string, TraceGraphNode>()
  nodes.forEach((node) => {
    const current = turnStarts.get(node.turnId)
    if (!current || node.startedSeq < current.startedSeq) turnStarts.set(node.turnId, node)
  })
  const turns = [...turnStarts.entries()]
    .sort(([, left], [, right]) => (
      left.startedSeq - right.startedSeq
      || compareTraceGraphIds(left.turnId, right.turnId)
    ))
    .map(([id, first], index) => ({
      id,
      ordinal: index + 1,
      startedAt: first.startedAt,
    }))
  const orderedNodes = orderNodes(turns, nodes)
  const orderedNodeIds = orderedNodes.map((node) => node.id)
  return {
    turns,
    nodes: orderedNodes,
    orderedNodeIds,
    matchedNodeIds: orderedNodeIds,
    asOfSeq,
    completeness: completeness(),
  }
}
