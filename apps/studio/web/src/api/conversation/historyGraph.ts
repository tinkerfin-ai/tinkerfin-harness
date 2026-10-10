import { ConversationError } from './errors'
import {
  parseTraceGraphNode,
  parseTraceGraphWithNodes,
  type TraceGraph,
  type TraceGraphNode,
  type TraceGraphNodeKind,
} from './traceGraph'

/** 对话恢复保留卡片内容；模型请求仅由独立链路查询提供 */
export type ConversationGraphNode =
  | (Omit<TraceGraphNode, 'kind' | 'request'> & { kind: 'model' })
  | (Omit<TraceGraphNode, 'kind'> & { kind: Exclude<TraceGraphNodeKind, 'model'> })

export type ConversationGraph = Omit<TraceGraph, 'nodes'> & {
  nodes: ConversationGraphNode[]
}

const isConversationNode = (node: TraceGraphNode): node is ConversationGraphNode => (
  node.kind !== 'model' || !Object.hasOwn(node, 'request')
)

const parseConversationNode = (value: unknown): ConversationGraphNode => {
  const node = parseTraceGraphNode(value)
  if (!isConversationNode(node)) {
    throw new ConversationError('stream_event_invalid')
  }
  return node
}

export const parseConversationGraph = (value: unknown): ConversationGraph => (
  parseTraceGraphWithNodes(value, parseConversationNode)
)
