import type { AccessMode, JsonObject, JsonValue, PendingInteractionKind } from "../../types"
import { requestEventStream, requestJson } from '../shared/http'
import { parseJsonSseStream } from '../shared/sse'
import { ConversationError, isConversationPreparationFailure } from './errors'
import { isInterrupt, isMessageSource, parseConversationAgUiEvent } from './eventParser'
import type {
  ConversationGraph,
} from './historyGraph'
import { parseConversationGraph } from './historyGraph'
import { parsePlanResults, type PlanResult } from './planResults'
import {
  parseTaskTraceSnapshot,
  type TaskTraceSnapshot,
} from './taskTrace'
import type { ConversationTitleSnapshot } from "./titles"
import type { ConversationAgUiEvent, InterruptEvent, MessageSource } from './types'

export interface ConversationHistoryListItem extends ConversationTitleSnapshot {
  projectId: string
  archived: boolean
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
  source?: MessageSource | null
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
      || (item.retryable && !isConversationPreparationFailure(item.errorCode))) {
      throw new ConversationError('stream_event_invalid')
    }
    traceObservationTime(item.failedAt)
    ids.add(item.runId)
    return { runId: item.runId, errorCode: item.errorCode, failedAt: item.failedAt, retryable: item.retryable }
  })
}

export interface InteractionAvailability {
  interruptId: string
  state: 'available' | 'confirming' | 'resolved' | 'cancelled'
  submissionRunId: string | null
}

/** 指定提交未保存的完整凭据，与当前交互由谁认领无关 */
export interface InteractionSubmissionResult {
  submissionRunId: string
  interruptIds: string[]
  state: 'not_saved'
}

export interface ConversationHistoryDetail extends ConversationTitleSnapshot {
  projectId: string
  archived: boolean
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
  graph: ConversationGraph
  state: TraceState
  interactions: TraceInteraction[]
  interactionAvailability: InteractionAvailability[]
  submissionResult: InteractionSubmissionResult | null
  planResults: PlanResult[]
  status: TraceStatus
  completeness: TraceCompleteness
  taskTrace: TaskTraceSnapshot | null
  createdAt: string
  updatedAt: string
}

export type ConversationHistoryCoreDetail = Omit<ConversationHistoryDetail, 'taskTrace'>

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
    projectId: string
    archived?: boolean
    scope?: 'project' | 'all'
    pageSize?: number
    cursor?: string | null
    query?: string | null
    signal?: AbortSignal
    suppressGlobalError?: boolean
  },
): Promise<ConversationHistoryListResponse> => {
  const search = new URLSearchParams()
  search.set('projectId', params.projectId)
  if (params.archived) search.set('archived', 'true')
  if (params.scope) search.set('scope', params.scope)
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
    submissionRunId?: string
    limit?: number
    signal?: AbortSignal
    suppressGlobalError?: boolean
  },
): Promise<ConversationHistoryDetail> => {
  const search = new URLSearchParams()
  search.set('includeTaskTrace', String(options.includeTaskTrace))
  if (options.historyCursor) search.set('historyCursor', options.historyCursor)
  if (options.submissionRunId) search.set('submissionRunId', options.submissionRunId)
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
    if (message.source != null && !isMessageSource(message.source)) throw new ConversationError('stream_event_invalid')
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
  const interactionAvailability = parseInteractionAvailability(value.interactionAvailability)
  const submissionResult = parseSubmissionResult(value.submissionResult)
  const planResults = parsePlanResults(value.planResults)
  const graph = parseConversationGraph(value.graph)
  if (graph.asOfSeq !== value.asOfSeq) {
    throw new ConversationError('stream_event_invalid')
  }
  return { ...value, graph, interactionAvailability, submissionResult, planResults, runFailures: parseRunFailures(value.runFailures) } as unknown as ConversationHistoryDetail
}

export const parseSubmissionResult = (value: unknown): InteractionSubmissionResult | null => {
  if (value === null) return null
  const isId = (id: unknown): id is string => typeof id === 'string' && id.length > 0 && id === id.trim()
  if (!isRecord(value) || value.state !== 'not_saved' || !isId(value.submissionRunId)
    || !Array.isArray(value.interruptIds) || !value.interruptIds.length || !value.interruptIds.every(isId)
    || new Set(value.interruptIds).size !== value.interruptIds.length
    || Object.keys(value).some(key => !['submissionRunId', 'interruptIds', 'state'].includes(key))) {
    throw new ConversationError('stream_event_invalid')
  }
  return { submissionRunId: value.submissionRunId, interruptIds: [...value.interruptIds], state: 'not_saved' }
}

export const parseInteractionAvailability = (value: unknown): InteractionAvailability[] => {
  if (!Array.isArray(value)) throw new ConversationError('stream_event_invalid')
  const ids = new Set<string>()
  return value.map(item => {
    if (!isRecord(item) || typeof item.interruptId !== 'string' || !item.interruptId.trim()
      || item.interruptId !== item.interruptId.trim() || ids.has(item.interruptId)
      || (item.state !== 'available' && item.state !== 'confirming'
        && item.state !== 'resolved' && item.state !== 'cancelled')
      || !(item.submissionRunId === null || (typeof item.submissionRunId === 'string'
        && item.submissionRunId.length > 0 && item.submissionRunId.trim() === item.submissionRunId))
      || (item.state === 'available' ? item.submissionRunId !== null : item.submissionRunId === null)) {
      throw new ConversationError('stream_event_invalid')
    }
    ids.add(item.interruptId)
    return item as unknown as InteractionAvailability
  })
}

/** 重命名或置顶会话，仅传需要修改的字段并返回更新后的会话摘要 */
export const patchConversation = (
  threadId: string,
  body: { title?: string; pinned?: boolean; projectId?: string; archived?: boolean },
  signal?: AbortSignal,
): Promise<ConversationHistoryListItem> =>
  requestJson<ConversationHistoryListItem>(
    CONVERSATION_API_PATH + '/' + encodeURIComponent(threadId),
    {
      method: 'PATCH',
      body,
      signal,
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
