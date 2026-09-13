import type { JsonValue } from '../../types'
import { requestEventStream, requestJson } from '../shared/http'
import { ConversationError } from './errors'
import { parseJsonSseStream } from './sse'
import {
  compareTraceGraphIds,
  compareTraceGraphNodes,
} from './traceGraphOrder'

export {
  compareTraceGraphIds,
  compareTraceGraphNodes,
} from './traceGraphOrder'

export type TraceGraphNodeKind =
  | 'human_message'
  | 'assistant_message'
  | 'context'
  | 'model'
  | 'tool'
  | 'subagent'
  | 'memory'
  | 'guardrail'
  | 'retrieval'
  | 'custom'
  | 'plan'
  | 'interaction'

export type TraceGraphNodeStatus =
  | 'running'
  | 'waiting'
  | 'succeeded'
  | 'failed'
  | 'cancelled'
  | 'abandoned'
  | 'unknown'

export type TraceGraphLinkIssue =
  | 'missing_subagent'
  | 'missing_model_call'
  | 'missing_tool_proposal'

export interface TraceGraphFailure {
  errorType: string
  message?: string | null
}

export interface TraceGraphTurn {
  id: string
  ordinal: number
  startedAt: string
}

export type TraceNodeReference =
  | { kind: 'tool'; toolCallId: string }
  | { kind: 'subagent'; parentToolCallId: string; subagentInvocationId: string }

export interface TraceGraphNode {
  agui: TraceNodeReference | null
  id: string
  turnId: string
  parentSubagentId?: string | null
  modelCallId?: string | null
  kind: TraceGraphNodeKind
  status: TraceGraphNodeStatus
  name: string
  runId: string
  graphNamespace: string[]
  agentName?: string | null
  provider?: string | null
  model?: string | null
  sourceId?: string | null
  startedAt: string
  firstOutputAt?: string | null
  completedAt?: string | null
  startedSeq: number
  updatedSeq: number
  content?: JsonValue | null
  contentOmitted: boolean
  toolCallOnly: boolean
  request?: JsonValue | null
  requestOmitted: boolean
  result?: JsonValue | null
  resultOmitted: boolean
  usage?: JsonValue | null
  responseMetadata?: JsonValue | null
  failure?: TraceGraphFailure | null
  linkIssues: TraceGraphLinkIssue[]
}

export interface TraceGraphCompleteness {
  callTrackingMissing: boolean
  relationshipEvidenceMissing: boolean
  detailsOmitted: boolean
}

export interface TraceGraphPage {
  turns: TraceGraphTurn[]
  nodes: TraceGraphNode[]
  orderedNodeIds: string[]
  matchedNodeIds: string[]
  nextCursor: string | null
  asOfSeq: number
  completeness: TraceGraphCompleteness
}

export type TraceGraph = Omit<TraceGraphPage, 'nextCursor'>

export interface TraceGraphDelta {
  asOfSeq: number
  nextCursor: string | null
  turnUpserts: TraceGraphTurn[]
  turnRemoves: string[]
  nodeUpserts: TraceGraphNode[]
  nodeRemoves: string[]
  orderedNodeIds: string[]
  matchedNodeIds: string[]
  completeness: TraceGraphCompleteness
}

export interface TraceGraphFilter {
  kinds?: TraceGraphNodeKind[]
  statuses?: TraceGraphNodeStatus[]
  modelCallId?: string
  agents?: string[]
  providers?: string[]
  models?: string[]
  graphNamespaces?: string[][]
  query?: string
  startedAfter?: string
  startedBefore?: string
}

export type TraceGraphEvent =
  | { type: 'snapshot'; snapshot: TraceGraphPage }
  | { type: 'update'; update: TraceGraphDelta }
  | { type: 'error'; code: 'trace_unavailable' }

const isRecord = (value: unknown): value is Record<string, unknown> => (
  value !== null && typeof value === 'object' && !Array.isArray(value)
)

const NODE_KINDS = new Set<TraceGraphNodeKind>([
  'human_message',
  'assistant_message',
  'context',
  'model',
  'tool',
  'subagent',
  'memory',
  'guardrail',
  'retrieval',
  'custom',
  'plan',
  'interaction',
])

const NODE_STATUSES = new Set<TraceGraphNodeStatus>([
  'running',
  'waiting',
  'succeeded',
  'failed',
  'cancelled',
  'abandoned',
  'unknown',
])

const LINK_ISSUES = new Set<TraceGraphLinkIssue>([
  'missing_subagent',
  'missing_model_call',
  'missing_tool_proposal',
])

const TURN_KEYS = new Set(['id', 'ordinal', 'startedAt'])
const FAILURE_KEYS = new Set(['errorType', 'message'])
const NODE_KEYS = new Set([
  'agui',
  'id',
  'turnId',
  'parentSubagentId',
  'modelCallId',
  'kind',
  'status',
  'name',
  'runId',
  'graphNamespace',
  'agentName',
  'provider',
  'model',
  'sourceId',
  'startedAt',
  'firstOutputAt',
  'completedAt',
  'startedSeq',
  'updatedSeq',
  'content',
  'contentOmitted',
  'toolCallOnly',
  'request',
  'requestOmitted',
  'result',
  'resultOmitted',
  'usage',
  'responseMetadata',
  'failure',
  'linkIssues',
])
const COMPLETENESS_KEYS = new Set([
  'callTrackingMissing',
  'relationshipEvidenceMissing',
  'detailsOmitted',
])
const GRAPH_KEYS = new Set([
  'turns',
  'nodes',
  'orderedNodeIds',
  'matchedNodeIds',
  'asOfSeq',
  'completeness',
])
const PAGE_KEYS = new Set([...GRAPH_KEYS, 'nextCursor'])
const DELTA_KEYS = new Set([
  'asOfSeq',
  'nextCursor',
  'turnUpserts',
  'turnRemoves',
  'nodeUpserts',
  'nodeRemoves',
  'orderedNodeIds',
  'matchedNodeIds',
  'completeness',
])
const SNAPSHOT_EVENT_KEYS = new Set(['type', 'snapshot'])
const UPDATE_EVENT_KEYS = new Set(['type', 'update'])
const ERROR_EVENT_KEYS = new Set(['type', 'code'])

const hasOnlyKeys = (value: Record<string, unknown>, keys: Set<string>) => (
  Object.keys(value).every((key) => keys.has(key))
)

const isCanonicalString = (value: unknown, maxLength: number) => (
  typeof value === 'string'
  && Array.from(value).length > 0
  && Array.from(value).length <= maxLength
  && value.trim() === value
)

const isBoundedString = (value: unknown, maxLength: number) => (
  typeof value === 'string'
  && Array.from(value).length > 0
  && Array.from(value).length <= maxLength
)

const isOptionalCanonicalString = (value: unknown, maxLength: number) => (
  value === undefined || value === null || isCanonicalString(value, maxLength)
)

const isTimestamp = (value: unknown) => (
  typeof value === 'string'
  && (value.endsWith('Z') || value.endsWith('+00:00'))
  && Number.isFinite(Date.parse(value))
)

const isOptionalTimestamp = (value: unknown) => (
  value === undefined || value === null || isTimestamp(value)
)

const isStringArray = (
  value: unknown,
  options: { maxItems: number; maxLength: number },
): value is string[] => (
  Array.isArray(value)
  && value.length <= options.maxItems
  && value.every((item) => isCanonicalString(item, options.maxLength))
)

const parseTurn = (value: unknown): TraceGraphTurn => {
  if (!isRecord(value)
    || !hasOnlyKeys(value, TURN_KEYS)
    || !isCanonicalString(value.id, 2048)
    || !Number.isSafeInteger(value.ordinal)
    || Number(value.ordinal) < 1
    || !isTimestamp(value.startedAt)
  ) throw new ConversationError('stream_event_invalid')
  return value as unknown as TraceGraphTurn
}

const parseFailure = (value: unknown): TraceGraphFailure | null | undefined => {
  if (value === undefined || value === null) return value
  if (!isRecord(value)
    || !hasOnlyKeys(value, FAILURE_KEYS)
    || !isCanonicalString(value.errorType, 1024)
    || (value.message !== undefined
      && value.message !== null
      && !isBoundedString(value.message, 4096))
  ) throw new ConversationError('stream_event_invalid')
  return value as unknown as TraceGraphFailure
}

const TERMINAL_STATUSES = new Set<TraceGraphNodeStatus>([
  'succeeded',
  'failed',
  'cancelled',
  'abandoned',
])

const isReferenceId = (value: unknown) => (
  typeof value === 'string' && value.length > 0 && value.trim() === value
)

const parseNode = (value: unknown): TraceGraphNode => {
  if (!isRecord(value)
    || !hasOnlyKeys(value, NODE_KEYS)
    || !isCanonicalString(value.id, 2048)
    || !isCanonicalString(value.turnId, 2048)
    || !isCanonicalString(value.name, 1024)
    || !isCanonicalString(value.runId, 1024)
    || typeof value.kind !== 'string'
    || !NODE_KINDS.has(value.kind as TraceGraphNodeKind)
    || typeof value.status !== 'string'
    || !NODE_STATUSES.has(value.status as TraceGraphNodeStatus)
    || !isStringArray(value.graphNamespace, { maxItems: 64, maxLength: 1024 })
    || !isTimestamp(value.startedAt)
    || !Number.isSafeInteger(value.startedSeq)
    || Number(value.startedSeq) < 1
    || !Number.isSafeInteger(value.updatedSeq)
    || Number(value.updatedSeq) < Number(value.startedSeq)
    || !isOptionalCanonicalString(value.parentSubagentId, 2048)
    || !isOptionalCanonicalString(value.modelCallId, 2048)
    || !isOptionalCanonicalString(value.agentName, 1024)
    || !isOptionalCanonicalString(value.provider, 1024)
    || !isOptionalCanonicalString(value.model, 1024)
    || !isOptionalCanonicalString(value.sourceId, 2048)
    || !isOptionalTimestamp(value.firstOutputAt)
    || !isOptionalTimestamp(value.completedAt)
    || typeof value.contentOmitted !== 'boolean'
    || typeof value.toolCallOnly !== 'boolean'
    || typeof value.requestOmitted !== 'boolean'
    || typeof value.resultOmitted !== 'boolean'
    || !Array.isArray(value.linkIssues)
    || value.linkIssues.some((item) => (
      typeof item !== 'string' || !LINK_ISSUES.has(item as TraceGraphLinkIssue)
    ))
    || new Set(value.linkIssues).size !== value.linkIssues.length
  ) throw new ConversationError('stream_event_invalid')
  const ref = value.agui
  if (ref !== null && (!isRecord(ref) || ref.kind !== value.kind
    || (ref.kind !== 'tool' && ref.kind !== 'subagent')
    || (ref.kind === 'tool' && (!isReferenceId(ref.toolCallId)
      || !hasOnlyKeys(ref, new Set(['kind', 'toolCallId']))))
    || (ref.kind === 'subagent' && (!isReferenceId(ref.parentToolCallId)
      || !isReferenceId(ref.subagentInvocationId)
      || !hasOnlyKeys(ref, new Set(['kind', 'parentToolCallId', 'subagentInvocationId']))))
  )) throw new ConversationError('stream_event_invalid')
  const status = value.status as TraceGraphNodeStatus
  const startedAt = Date.parse(value.startedAt as string)
  const firstOutputAt = value.firstOutputAt == null
    ? undefined
    : Date.parse(value.firstOutputAt as string)
  const completedAt = value.completedAt == null
    ? undefined
    : Date.parse(value.completedAt as string)
  if ((firstOutputAt != null && firstOutputAt < startedAt)
    || (completedAt != null && completedAt < startedAt)
    || (firstOutputAt != null && completedAt != null && firstOutputAt > completedAt)
  ) throw new ConversationError('stream_event_invalid')
  if ((status === 'running' || status === 'waiting') && value.completedAt != null) {
    throw new ConversationError('stream_event_invalid')
  }
  if (TERMINAL_STATUSES.has(status) && value.completedAt == null) {
    throw new ConversationError('stream_event_invalid')
  }
  if (value.modelCallId != null
    && value.kind !== 'assistant_message'
    && value.kind !== 'tool'
    && value.kind !== 'subagent'
  ) throw new ConversationError('stream_event_invalid')
  if (value.toolCallOnly && value.kind !== 'assistant_message') {
    throw new ConversationError('stream_event_invalid')
  }
  if (value.toolCallOnly && value.contentOmitted) {
    throw new ConversationError('stream_event_invalid')
  }
  parseFailure(value.failure)
  return value as unknown as TraceGraphNode
}

const parseCompleteness = (value: unknown): TraceGraphCompleteness => {
  if (!isRecord(value)
    || !hasOnlyKeys(value, COMPLETENESS_KEYS)
    || typeof value.callTrackingMissing !== 'boolean'
    || typeof value.relationshipEvidenceMissing !== 'boolean'
    || typeof value.detailsOmitted !== 'boolean'
  ) throw new ConversationError('stream_event_invalid')
  return value as unknown as TraceGraphCompleteness
}

const uniqueIds = (ids: string[]) => new Set(ids).size === ids.length

const expectedNodeOrder = (
  turns: TraceGraphTurn[],
  nodes: TraceGraphNode[],
): string[] => {
  const byTurn = new Map<string, TraceGraphNode[]>()
  nodes.forEach((node) => byTurn.set(node.turnId, [
    ...(byTurn.get(node.turnId) ?? []),
    node,
  ]))
  const ordered: string[] = []
  const visited = new Set<string>()
  ;[...turns]
    .sort((left, right) => (
      left.ordinal - right.ordinal || compareTraceGraphIds(left.id, right.id)
    ))
    .forEach((turn) => {
      const children = new Map<string | null, TraceGraphNode[]>()
      ;(byTurn.get(turn.id) ?? []).forEach((node) => {
        const owner = node.parentSubagentId ?? null
        children.set(owner, [...(children.get(owner) ?? []), node])
      })
      children.forEach((values) => values.sort(compareTraceGraphNodes))
      const stack = [...(children.get(null) ?? [])]
        .reverse()
        .map((node) => ({
          node,
          scopeDepth: node.kind === 'subagent' ? 1 : 0,
        }))
      while (stack.length > 0) {
        const current = stack.pop()
        if (!current || current.scopeDepth > 64 || visited.has(current.node.id)) {
          throw new ConversationError('stream_event_invalid')
        }
        visited.add(current.node.id)
        ordered.push(current.node.id)
        if (current.node.kind === 'subagent') {
          stack.push(
            ...(children.get(current.node.id) ?? [])
              .slice()
              .reverse()
              .map((node) => ({
                node,
                scopeDepth: current.scopeDepth + (node.kind === 'subagent' ? 1 : 0),
              })),
          )
        }
      }
    })
  if (ordered.length !== nodes.length) throw new ConversationError('stream_event_invalid')
  return ordered
}

const parseTraceGraphValue = (
  value: unknown,
  allowedKeys: Set<string>,
): TraceGraph => {
  if (!isRecord(value)
    || !hasOnlyKeys(value, allowedKeys)
    || !Array.isArray(value.turns)
    || !Array.isArray(value.nodes)
    || !isStringArray(value.orderedNodeIds, { maxItems: 20_000, maxLength: 2048 })
    || !isStringArray(value.matchedNodeIds, { maxItems: 10_000, maxLength: 2048 })
    || !Number.isSafeInteger(value.asOfSeq)
    || Number(value.asOfSeq) < 1
  ) throw new ConversationError('stream_event_invalid')
  const turns = value.turns.map(parseTurn)
  const nodes = value.nodes.map(parseNode)
  const nodeIds = nodes.map((node) => node.id)
  const nodeIdSet = new Set(nodeIds)
  const orderedNodeIds = value.orderedNodeIds
  const matchedNodeIds = value.matchedNodeIds
  const matchedNodeIdSet = new Set(matchedNodeIds)
  const turnsById = new Map(turns.map((turn) => [turn.id, turn]))
  const nodesById = new Map(nodes.map((node) => [node.id, node]))
  const turnIds = turns.map((turn) => turn.id)
  const turnOrdinals = turns.map((turn) => String(turn.ordinal))
  if (!uniqueIds(turnIds)
    || !uniqueIds(turnOrdinals)
    || turns.some((turn, index) => index > 0 && turns[index - 1]!.ordinal >= turn.ordinal)
    || !uniqueIds(nodeIds)
    || !uniqueIds(orderedNodeIds)
    || orderedNodeIds.length !== nodeIds.length
    || orderedNodeIds.some((id) => !nodeIdSet.has(id))
    || orderedNodeIds.some((id, index) => nodeIds[index] !== id)
    || nodes.some((node) => node.updatedSeq > Number(value.asOfSeq))
    || !uniqueIds(matchedNodeIds)
    || matchedNodeIds.some((id) => !nodeIdSet.has(id))
    || orderedNodeIds.filter((id) => matchedNodeIdSet.has(id))
      .some((id, index) => matchedNodeIds[index] !== id)
    || nodes.some((node) => !turnsById.has(node.turnId))
    || nodes.some((node) => {
      if (!node.parentSubagentId) return false
      const parent = nodesById.get(node.parentSubagentId)
      return !parent || parent.kind !== 'subagent' || parent.turnId !== node.turnId
    })
    || expectedNodeOrder(turns, nodes)
      .some((id, index) => orderedNodeIds[index] !== id)
  ) throw new ConversationError('stream_event_invalid')
  return {
    turns,
    nodes,
    orderedNodeIds,
    matchedNodeIds,
    asOfSeq: value.asOfSeq as number,
    completeness: parseCompleteness(value.completeness),
  }
}

export const parseTraceGraph = (value: unknown): TraceGraph => (
  parseTraceGraphValue(value, GRAPH_KEYS)
)

export const parseTraceGraphPage = (value: unknown): TraceGraphPage => {
  if (!isRecord(value)
    || (value.nextCursor !== null && typeof value.nextCursor !== 'string')
  ) throw new ConversationError('stream_event_invalid')
  return {
    ...parseTraceGraphValue(value, PAGE_KEYS),
    nextCursor: value.nextCursor,
  }
}

export const parseTraceGraphDelta = (value: unknown): TraceGraphDelta => {
  if (!isRecord(value)
    || !hasOnlyKeys(value, DELTA_KEYS)
    || !Number.isSafeInteger(value.asOfSeq)
    || Number(value.asOfSeq) < 1
    || (value.nextCursor !== null && typeof value.nextCursor !== 'string')
    || !Array.isArray(value.turnUpserts)
    || !isStringArray(value.turnRemoves, { maxItems: 20_000, maxLength: 2048 })
    || !Array.isArray(value.nodeUpserts)
    || !isStringArray(value.nodeRemoves, { maxItems: 20_000, maxLength: 2048 })
    || !isStringArray(value.orderedNodeIds, { maxItems: 20_000, maxLength: 2048 })
    || !isStringArray(value.matchedNodeIds, { maxItems: 10_000, maxLength: 2048 })
  ) throw new ConversationError('stream_event_invalid')
  const orderedNodeIds = value.orderedNodeIds
  const matchedNodeIds = value.matchedNodeIds
  const turnRemoves = value.turnRemoves
  const nodeRemoves = value.nodeRemoves
  const turnUpserts = value.turnUpserts.map(parseTurn)
  const nodeUpserts = value.nodeUpserts.map(parseNode)
  const orderedNodeIdSet = new Set(orderedNodeIds)
  const matchedNodeIdSet = new Set(matchedNodeIds)
  const turnUpsertIds = turnUpserts.map((turn) => turn.id)
  const nodeUpsertIds = nodeUpserts.map((node) => node.id)
  if (!uniqueIds(orderedNodeIds)
    || !uniqueIds(matchedNodeIds)
    || !uniqueIds(turnRemoves)
    || !uniqueIds(nodeRemoves)
    || !uniqueIds(turnUpsertIds)
    || !uniqueIds(nodeUpsertIds)
    || turnUpsertIds.some((id) => turnRemoves.includes(id))
    || nodeUpsertIds.some((id) => nodeRemoves.includes(id))
    || nodeUpsertIds.some((id) => !orderedNodeIdSet.has(id))
    || matchedNodeIds.some((id) => !orderedNodeIdSet.has(id))
    || orderedNodeIds.filter((id) => matchedNodeIdSet.has(id))
      .some((id, index) => matchedNodeIds[index] !== id)
    || nodeUpserts.some((node) => node.updatedSeq > Number(value.asOfSeq))
  ) throw new ConversationError('stream_event_invalid')
  return {
    asOfSeq: value.asOfSeq as number,
    nextCursor: value.nextCursor,
    turnUpserts,
    turnRemoves,
    nodeUpserts,
    nodeRemoves,
    orderedNodeIds,
    matchedNodeIds,
    completeness: parseCompleteness(value.completeness),
  }
}

const parseTraceGraphEvent = (value: unknown): TraceGraphEvent => {
  if (!isRecord(value)) throw new ConversationError('stream_event_invalid')
  if (value.type === 'snapshot' && hasOnlyKeys(value, SNAPSHOT_EVENT_KEYS)) {
    return { type: 'snapshot', snapshot: parseTraceGraphPage(value.snapshot) }
  }
  if (value.type === 'update' && hasOnlyKeys(value, UPDATE_EVENT_KEYS)) {
    return { type: 'update', update: parseTraceGraphDelta(value.update) }
  }
  if (value.type === 'error'
    && value.code === 'trace_unavailable'
    && hasOnlyKeys(value, ERROR_EVENT_KEYS)
  ) return { type: 'error', code: 'trace_unavailable' }
  throw new ConversationError('stream_event_invalid')
}

const appendFilter = (search: URLSearchParams, filter: TraceGraphFilter) => {
  filter.kinds?.forEach((value) => search.append('kind', value))
  filter.statuses?.forEach((value) => search.append('status', value))
  if (filter.modelCallId) search.set('modelCallId', filter.modelCallId)
  filter.agents?.forEach((value) => search.append('agent', value))
  filter.providers?.forEach((value) => search.append('provider', value))
  filter.models?.forEach((value) => search.append('model', value))
  filter.graphNamespaces?.forEach((value) => search.append(
    'graph_namespace',
    value.length === 0 ? 'root' : value.join('|'),
  ))
  if (filter.query) search.set('query', filter.query)
  if (filter.startedAfter) search.set('startedAfter', filter.startedAfter)
  if (filter.startedBefore) search.set('startedBefore', filter.startedBefore)
}

const graphUrl = (
  threadId: string,
  filter: TraceGraphFilter,
  options: { follow: boolean; limit: number },
) => {
  const search = new URLSearchParams()
  appendFilter(search, filter)
  search.set('limit', String(options.limit))
  const suffix = options.follow ? '/follow' : ''
  return `/api/conversation/${encodeURIComponent(threadId)}/trace/graph${suffix}?${search}`
}

export const queryTraceGraph = async (
  threadId: string,
  filter: TraceGraphFilter,
  options: { limit?: number; signal?: AbortSignal } = {},
): Promise<TraceGraphPage> => parseTraceGraphPage(await requestJson<unknown>(
  graphUrl(threadId, filter, {
    follow: false,
    limit: options.limit ?? 100,
  }),
  { signal: options.signal, suppressGlobalError: true },
))

export async function* followTraceGraph(
  threadId: string,
  filter: TraceGraphFilter,
  options: { limit?: number; signal?: AbortSignal } = {},
): AsyncGenerator<TraceGraphEvent> {
  const response = await requestEventStream(
    graphUrl(threadId, filter, {
      follow: true,
      limit: options.limit ?? 100,
    }),
    { signal: options.signal, suppressGlobalError: true },
  )
  if (!response.body) throw new ConversationError('stream_body_missing')
  for await (const frame of parseJsonSseStream(response.body, options.signal)) {
    yield parseTraceGraphEvent(frame.data)
  }
}
