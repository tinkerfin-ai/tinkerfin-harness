import type { TraceGraphNode, TraceGraphNodeKind } from './traceGraph'

const KIND_ORDER: Partial<Record<TraceGraphNodeKind, number>> = {
  human_message: 0,
  context: 1,
  model: 2,
  tool: 3,
  subagent: 4,
  assistant_message: 5,
}

export const compareTraceGraphIds = (left: string, right: string) => {
  if (left === right) return 0
  const leftPoints = Array.from(left, (value) => value.codePointAt(0) as number)
  const rightPoints = Array.from(right, (value) => value.codePointAt(0) as number)
  const length = Math.min(leftPoints.length, rightPoints.length)
  for (let index = 0; index < length; index += 1) {
    if (leftPoints[index] !== rightPoints[index]) {
      return (leftPoints[index] as number) - (rightPoints[index] as number)
    }
  }
  return leftPoints.length - rightPoints.length
}

export const compareTraceGraphNodes = (
  left: TraceGraphNode,
  right: TraceGraphNode,
) => (
  left.startedSeq - right.startedSeq
  || (KIND_ORDER[left.kind] ?? 1) - (KIND_ORDER[right.kind] ?? 1)
  || compareTraceGraphIds(left.id, right.id)
)
