import type { AccessMode } from "../../types"
import { isInterrupt, parseConversationAgUiEvent } from './eventParser'
import type { InterruptEvent, ConversationAgUiEvent } from './types'
import type { ConversationTitleSnapshot } from "./titles"
import type { PendingInteractionKind, JsonObject, JsonValue } from '../../types'
import { requestEventStream, requestJson } from '../shared/http'
import { ConversationError } from './errors'
import { parseJsonSseStream } from './sse'
import type {
  TraceGraph,
  TraceGraphDelta,
} from './traceGraph'
import { parseTraceGraph, parseTraceGraphDelta } from './traceGraph'
import {
  parseTaskTraceSnapshot,
  type TaskTraceSnapshot,
} from './taskTrace'

export interface ConversationHistoryListItem extends ConversationTitleSnapshot {
  id: number
  threadId: string
  title: string
  status: string
  lastRunId?: string | null
  lastModel?: string | null
  accessMode: AccessMode
  messageCount: number
  toolCallCount: number
  hasPendingInterrupt: boolean
  pendingInteractionKind: PendingInteractionKind | null
  pinned: boolean
  createdAt: string
  updatedAt: string
}

export interface ConversationHistoryListResponse {
  items: ConversationHistoryListItem[]
  nextCursor?: string | null
}

export interface ConversationHistoryGroupConfig {
  dayRanges: number[]
}

export type TraceMessageReference =
  | { kind: 'message'; messageId: string }
  | { kind: 'tool_message'; messageId: string; toolCallId: string }

export interface TraceMessage {
  agui: TraceMessageReference | null
  id: string
  traceSeq: number
  sourceId?: string | null
  graphNamespace: string[]
  runId: string
  role: 'user' | 'assistant' | 'tool' | 'system' | 'other'
  content?: JsonValue | null
  contentOmitted: boolean
  name?: string | null
  toolCallId?: string | null
  status: 'streaming' | 'completed'
  createdAt: string
  completedAt?: string | null
}

export interface TraceReasoning {
  id: string
  traceSeq: number
  messageId: string
  graphNamespace: string[]
  runId: string
  extractor: string
  content?: JsonValue | null
  contentOmitted: boolean
  status: 'streaming' | 'completed'
  createdAt: string
  completedAt?: string | null
}

export interface TraceInteraction {
  agui: InterruptEvent[] | null
  id: string
  traceSeq: number
  sourceId: string
  graphNamespace: string[]
  runId: string
  kind: string
  toolCallIds: string[]
  status: 'pending' | 'resolved' | 'cancelled'
  payload?: JsonValue | null
  payloadOmitted: boolean
  openedAt: string
  resolvedAt?: string | null
}

export interface TraceState {
  root: JsonObject
  subgraphs: Record<string, JsonObject>
}

export interface TraceStatus {
  execution: 'running' | 'waiting' | 'succeeded' | 'failed' | 'cancelled' | 'abandoned' | 'unknown'
  headRunId: string
}

export interface TraceCompleteness {
  missingPrefix: boolean
  missingTail: boolean
  payloadOmitted: boolean
}

export interface ConversationRunFailure {
  runId: string
  errorCode: string | null
  failedAt: string
  retryable: boolean
}

export const parseRunFailures = (value: unknown): ConversationRunFailure[] => {
  if (!Array.isArray(value)) throw new ConversationError('stream_event_invalid')
  const ids = new Set<string>()
  return value.map(item => {
    if (!isRecord(item) || typeof item.runId !== 'string' || !item.runId.trim()
      || item.runId !== item.runId.trim() || ids.has(item.runId)
      || !(item.errorCode === null || typeof item.errorCode === 'string')
      || typeof item.failedAt !== 'string' || typeof item.retryable !== 'boolean'
      || (item.retryable && item.errorCode !== 'runtime_initialization_error')) {
      throw new ConversationError('stream_event_invalid')
    }
    traceObservationTime(item.failedAt)
    ids.add(item.runId)
    return { runId: item.runId, errorCode: item.errorCode, failedAt: item.failedAt, retryable: item.retryable }
  })
}

export interface ConversationHistoryDetail extends ConversationTitleSnapshot {
  id: number
  threadId: string
  title: string
  lastModel?: string | null
  accessMode: AccessMode
  pinned: boolean
  asOfSeq: number
  generation: string
  observedAt: string
  headRunId: string
  availableHeads: string[]
  historyCursor?: string | null
  messageCount: number
  toolCallCount: number
  messages: TraceMessage[]
  runFailures: ConversationRunFailure[]
  reasoning: TraceReasoning[]
  graph: TraceGraph
  state: TraceState
  interactions: TraceInteraction[]
  status: TraceStatus
  completeness: TraceCompleteness
  taskTrace: TaskTraceSnapshot | null
  createdAt: string
  updatedAt: string
}

export type ConversationHistoryCoreDetail = Omit<ConversationHistoryDetail, 'taskTrace'>

export interface TraceEntityDelta<T> {
  upserts: T[]
  removes: string[]
}

export interface ConversationTraceUpdate {
  runFailures: ConversationRunFailure[]
  asOfSeq: number
  generation: string
  observedAt: string
  events: JsonValue[]
  facts: JsonValue[]
  messages: TraceEntityDelta<TraceMessage>
  reasoning: TraceEntityDelta<TraceReasoning>
  graph: TraceGraphDelta
  interactions: TraceEntityDelta<TraceInteraction>
  state: TraceState
  status: TraceStatus
  completeness: TraceCompleteness
  messageCount: number
  toolCallCount: number
  projections: Record<string, JsonValue>
}

export type ConversationTraceEvent =
  | { type: 'snapshot'; snapshot: ConversationHistoryDetail }
  | {
      type: 'update'
      update: ConversationTraceUpdate
      taskTrace: TaskTraceSnapshot | null
    }
  | { type: 'error'; code: 'trace_unavailable' }

const CONVERSATION_API_PATH = '/api/conversation'

export const traceObservationTime = (value: string): bigint => {
  const match = /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,6}))?(?:Z|\+00:00)$/.exec(value)
  if (!match) throw new ConversationError('stream_event_invalid')
  const seconds = Date.parse(`${match[1]}Z`)
  if (!Number.isFinite(seconds)) throw new ConversationError('stream_event_invalid')
  // 保留数据库微秒精度；Date.parse 单独使用会把不同观测压缩到同一毫秒
  return BigInt(seconds) * 1000n + BigInt((match[2] ?? '').padEnd(6, '0'))
}

const validateTraceObservation = (value: Record<string, unknown>) => {
  if (typeof value.generation !== 'string' || !value.generation
    || typeof value.observedAt !== 'string') {
    throw new ConversationError('stream_event_invalid')
  }
  traceObservationTime(value.observedAt)
}

export const fetchConversationHistoryList = (
  params: {
    pageSize?: number
    cursor?: string | null
    query?: string | null
    signal?: AbortSignal
    suppressGlobalError?: boolean
  } = {},
): Promise<ConversationHistoryListResponse> => {
  const search = new URLSearchParams()
  if (params.pageSize) search.set('pageSize', String(params.pageSize))
  if (params.cursor) search.set('cursor', params.cursor)
  if (params.query) search.set('query', params.query)
  const query = search.toString() ? '?' + search.toString() : ''
  return requestJson<ConversationHistoryListResponse>(
    CONVERSATION_API_PATH + '/history' + query,
    {
      signal: params.signal,
      suppressGlobalError: params.suppressGlobalError,
    },
  )
}

export const fetchConversationHistoryGroupConfig = (
  options: { signal?: AbortSignal; suppressGlobalError?: boolean } = {},
): Promise<ConversationHistoryGroupConfig> => requestJson<ConversationHistoryGroupConfig>(
  CONVERSATION_API_PATH + '/config',
  options,
)

export const fetchConversationHistoryDetail = (
  threadId: string,
  options: {
    includeTaskTrace: boolean
    historyCursor?: string | null
    limit?: number
    signal?: AbortSignal
    suppressGlobalError?: boolean
  },
): Promise<ConversationHistoryDetail> => {
  const search = new URLSearchParams()
  search.set('includeTaskTrace', String(options.includeTaskTrace))
  if (options.historyCursor) search.set('historyCursor', options.historyCursor)
  if (options.limit) search.set('limit', String(options.limit))
  const query = search.toString() ? '?' + search.toString() : ''
  return requestJson<unknown>(
    CONVERSATION_API_PATH + '/' + encodeURIComponent(threadId) + '/history' + query,
    {
      signal: options.signal,
      suppressGlobalError: options.suppressGlobalError,
    },
  ).then((value) => parseHistoryDetail(value, options.includeTaskTrace))
}

const isRecord = (value: unknown): value is Record<string, unknown> => (
  value !== null && typeof value === 'object' && !Array.isArray(value)
)

const validateMessageReferences = (value: unknown) => {
  if (!Array.isArray(value)) throw new ConversationError('stream_event_invalid')
  for (const message of value) {
    if (!isRecord(message) || !Object.hasOwn(message, 'agui')) {
      throw new ConversationError('stream_event_invalid')
    }
    const ref = message.agui
    if (ref === null) continue
    if (!isRecord(ref) || typeof ref.messageId !== 'string' || !ref.messageId
      || (ref.kind !== 'message' && ref.kind !== 'tool_message')
      || (ref.kind === 'tool_message' && (message.role !== 'tool'
        || typeof ref.toolCallId !== 'string' || !ref.toolCallId))
      || (ref.kind === 'message' && message.role === 'tool')
      || Object.keys(ref).some(key => !(
        ref.kind === 'message' ? ['kind', 'messageId'] : ['kind', 'messageId', 'toolCallId']
      ).includes(key))) throw new ConversationError('stream_event_invalid')
  }
}

const validateInteractionReferences = (value: unknown) => {
  if (!Array.isArray(value)) throw new ConversationError('stream_event_invalid')
  for (const interaction of value) {
    if (!isRecord(interaction) || !Object.hasOwn(interaction, 'agui')) {
      throw new ConversationError('stream_event_invalid')
    }
    if (interaction.agui !== null && (!Array.isArray(interaction.agui)
      || (interaction.status === 'pending' && interaction.agui.length === 0)
      || !interaction.agui.every(isInterrupt))) {
      throw new ConversationError('stream_event_invalid')
    }
  }
}

const parseHistoryDetail = (
  value: unknown,
  includeTaskTrace: boolean,
): ConversationHistoryDetail => {
  if (!isRecord(value) || !Object.hasOwn(value, 'taskTrace')) {
    throw new ConversationError('stream_event_invalid')
  }
  validateTraceObservation(value)
  if (includeTaskTrace) {
    if (value.taskTrace === null) throw new ConversationError('stream_event_invalid')
    parseTaskTraceSnapshot(value.taskTrace)
  } else if (value.taskTrace !== null) {
    throw new ConversationError('stream_event_invalid')
  }
  validateMessageReferences(value.messages)
  validateInteractionReferences(value.interactions)
  const graph = parseTraceGraph(value.graph)
  if (graph.asOfSeq !== value.asOfSeq) {
    throw new ConversationError('stream_event_invalid')
  }
  return { ...value, graph, runFailures: parseRunFailures(value.runFailures) } as unknown as ConversationHistoryDetail
}

const parseTraceEvent = (
  value: unknown,
  includeTaskTrace: boolean,
): ConversationTraceEvent => {
  if (!isRecord(value)) throw new ConversationError('stream_event_invalid')
  const record = value as Record<string, unknown>
  if (record.type === 'snapshot') {
    return {
      type: 'snapshot',
      snapshot: parseHistoryDetail(record.snapshot, includeTaskTrace),
    }
  }
  if (record.type === 'update') {
    if (!isRecord(record.update) || !Object.hasOwn(record, 'taskTrace')) {
      throw new ConversationError('stream_event_invalid')
    }
    validateTraceObservation(record.update)
    if (record.taskTrace !== null) {
      if (!includeTaskTrace) throw new ConversationError('stream_event_invalid')
      parseTaskTraceSnapshot(record.taskTrace)
    }
    if (!isRecord(record.update.messages) || !isRecord(record.update.interactions)) {
      throw new ConversationError('stream_event_invalid')
    }
    validateMessageReferences(record.update.messages.upserts)
    validateInteractionReferences(record.update.interactions.upserts)
    const graph = parseTraceGraphDelta(record.update.graph)
    if (graph.asOfSeq !== record.update.asOfSeq) {
      throw new ConversationError('stream_event_invalid')
    }
    return {
      ...record,
      update: { ...record.update, graph, runFailures: parseRunFailures(record.runFailures) },
    } as unknown as ConversationTraceEvent
  }
  if (record.type === 'error' && record.code === 'trace_unavailable') {
    return { type: 'error', code: 'trace_unavailable' }
  }
  throw new ConversationError('stream_event_invalid')
}

export async function* followConversationTrace(
  threadId: string,
  options: { includeTaskTrace: boolean; signal?: AbortSignal },
): AsyncGenerator<ConversationTraceEvent> {
  const search = new URLSearchParams({
    includeTaskTrace: String(options.includeTaskTrace),
  })
  const response = await requestEventStream(
    CONVERSATION_API_PATH + '/' + encodeURIComponent(threadId) + '/trace?' + search,
    { signal: options.signal, suppressGlobalError: true },
  )
  if (!response.body) throw new ConversationError('stream_body_missing')
  for await (const frame of parseJsonSseStream(response.body, options.signal)) {
    if (frame.event !== 'trace') throw new ConversationError('stream_event_invalid')
    yield parseTraceEvent(frame.data, options.includeTaskTrace)
  }
}

/** 重命名或置顶会话，仅传需要修改的字段并返回更新后的会话摘要 */
export const patchConversation = (
  threadId: string,
  body: { title?: string; pinned?: boolean },
): Promise<ConversationHistoryListItem> =>
  requestJson<ConversationHistoryListItem>(
    CONVERSATION_API_PATH + '/' + encodeURIComponent(threadId),
    {
      method: 'PATCH',
      body,
      suppressGlobalError: true,
    },
  )

/** 删除 Trace、Checkpoint、Messaging 与 Studio 自有会话记录 */
export const deleteConversation = async (threadId: string): Promise<void> => {
  await requestJson<null>(CONVERSATION_API_PATH + '/' + encodeURIComponent(threadId), {
    method: 'DELETE',
    suppressGlobalError: true,
  })
}


/** 从服务端基线恢复已有运行；游标仅用于仍保留完整视图的同页重连 */
export async function* followConversationRun(
  threadId: string,
  runId: string,
  options: { includeTaskTrace: boolean; signal: AbortSignal; afterSeq?: number },
): AsyncGenerator<
  | { type: 'snapshot'; snapshot: ConversationHistoryDetail; replay: boolean }
  | { type: 'event'; event: ConversationAgUiEvent; seq: number; replayed: boolean }
> {
  const response = await requestEventStream(
    `/api/conversation/${encodeURIComponent(threadId)}/runs/${encodeURIComponent(runId)}/events?includeTaskTrace=${options.includeTaskTrace}`,
    {
      signal: options.signal,
      headers: options.afterSeq == null ? undefined : { 'Last-Event-ID': String(options.afterSeq) },
      suppressGlobalError: true,
    },
  )
  if (!response.body) throw new ConversationError('stream_body_missing')
  let needsSnapshot = options.afterSeq == null
  for await (const frame of parseJsonSseStream(response.body, options.signal)) {
    if (isRecord(frame.data) && frame.data.type === 'snapshot') {
      if (!needsSnapshot || typeof frame.data.replay !== 'boolean') throw new ConversationError('stream_event_invalid')
      needsSnapshot = false
      yield { type: 'snapshot', snapshot: parseHistoryDetail(frame.data.snapshot, options.includeTaskTrace), replay: frame.data.replay }
      continue
    }
    if (needsSnapshot) throw new ConversationError('stream_event_invalid')
    const seq = frame.id !== null && /^[1-9]\d*$/.test(frame.id) ? Number(frame.id) : NaN
    if (!Number.isSafeInteger(seq)) throw new ConversationError('stream_sequence_invalid')
    if (frame.event !== null && frame.event !== 'message' && frame.event !== 'replay') throw new ConversationError('stream_event_invalid')
    yield { type: 'event', event: parseConversationAgUiEvent(frame.data), seq, replayed: frame.event === 'replay' }
  }
  if (needsSnapshot) throw new ConversationError('stream_disconnected')
}
