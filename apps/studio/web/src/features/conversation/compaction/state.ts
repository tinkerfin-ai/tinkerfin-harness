import type { ConversationAgUiEvent } from '../../../api/conversation/types'
import type { TraceGraphNode } from '../../../api/conversation/traceGraph'
import { ConversationError } from '../../../api/conversation/errors'
import type { Conversation, DeepReadonly, JsonObject, Message } from '../../../types'

export interface ContextCompaction {
  runId: string
  createdAt: string
  afterMessageId?: string
  status: 'generating' | 'saving' | 'compacted' | 'nothing_to_compact' | 'not_reduced' | 'cancelled' | 'failed' | 'unconfirmed'
  summary?: string
  compactedMessages?: number
}

const isObject = (value: unknown): value is Record<string, unknown> => value != null && typeof value === 'object' && !Array.isArray(value)

const parseResult = (value: unknown) => {
  if (!isObject(value) || typeof value.run_id !== 'string'
    || !['compacted', 'nothing_to_compact', 'not_reduced'].includes(String(value.status))
    || typeof value.compacted_messages !== 'number' || !Number.isInteger(value.compacted_messages) || value.compacted_messages < 0
    || (value.status === 'compacted' && (typeof value.summary !== 'string' || !value.summary.trim()))
  ) throw new ConversationError('stream_event_invalid')
  return {
    runId: value.run_id,
    status: value.status as 'compacted' | 'nothing_to_compact' | 'not_reduced',
    summary: typeof value.summary === 'string' ? value.summary : undefined,
    compactedMessages: value.compacted_messages,
  }
}

export const beginCompaction = (conversation: Conversation, runId: string): Conversation => ({
  ...conversation,
  activeRunId: runId,
  runStatus: 'streaming',
  notice: undefined,
  lastSeq: undefined,
  compactions: [...(conversation.compactions ?? []), {
    runId, createdAt: new Date().toISOString(), status: 'generating',
    afterMessageId: conversation.messages.at(-1)?.id,
  }],
})

export const compactionsFromTrace = (nodes: readonly DeepReadonly<TraceGraphNode>[], messages: readonly Message[]): ContextCompaction[] => nodes
  .filter(node => node.kind === 'custom' && node.contextKind === 'compaction' && node.compactionOrigin === 'manual' && node.graphNamespace.length === 0)
  .sort((left, right) => left.startedSeq - right.startedSeq)
  .map(node => ({
    runId: node.runId,
    createdAt: node.startedAt,
    afterMessageId: messages.filter(message => nodes.some(prior => prior.runId === message.meta?.runId && prior.startedSeq < node.startedSeq)).at(-1)?.id,
    status: node.status === 'running'
      ? isObject(node.result) && node.result.status === 'saving' ? 'saving' : 'generating'
      : isObject(node.result) && node.result.status === 'saving' ? 'unconfirmed'
        : node.status === 'cancelled' ? 'cancelled' : node.status === 'failed' ? 'failed' : 'unconfirmed',
    ...(isObject(node.result) && node.result.status === 'compacted' || isObject(node.result) && ['nothing_to_compact', 'not_reduced'].includes(String(node.result.status))
      ? parseResult(node.result) : {}),
  }))

export const syncCompactionState = (conversation: Conversation, state: DeepReadonly<JsonObject>): Conversation => {
  if (state.context_compaction == null) return conversation
  const result = parseResult(state.context_compaction)
  // 下一轮聊天可能携带此前的结果；只有对应操作卡接收该状态
  if (!conversation.compactions?.some(operation => operation.runId === result.runId)) return conversation
  return { ...conversation, compactions: conversation.compactions.map(operation => operation.runId === result.runId ? { ...operation, ...result } : operation) }
}

/** 压缩的停止或失败只结束本次操作，不改写聊天消息、任务清单或计划 */
export const reduceCompactionEvent = (conversation: Conversation, event: ConversationAgUiEvent): Conversation | undefined => {
  const operation = conversation.compactions?.find(item => item.runId === conversation.activeRunId)
  if (!operation) return undefined
  const progress = event.type === 'RAW' ? event.event.data : undefined
  if (event.type === 'RAW' && event.source === 'langgraph.custom'
    && isObject(progress) && progress.operation === 'context_compaction' && progress.runId === operation.runId && progress.phase === 'saving') {
    return { ...conversation, compactions: conversation.compactions?.map(item => item === operation ? { ...item, status: 'saving' } : item) }
  }
  if (event.type !== 'RUN_FINISHED' && event.type !== 'RUN_ERROR') return undefined
  const runId = event.type === 'RUN_FINISHED' ? event.runId : event.rawEvent?.runId
  if (runId && runId !== operation.runId) return conversation
  const cancelled = event.type === 'RUN_ERROR' && event.code === 'cancelled'
  const status = ['compacted', 'nothing_to_compact', 'not_reduced'].includes(operation.status)
    ? operation.status
    : event.type === 'RUN_FINISHED' || operation.status === 'saving'
      ? 'unconfirmed'
      : cancelled ? 'cancelled' : 'failed'
  return {
    ...conversation,
    activeRunId: undefined,
    runStatus: event.type === 'RUN_ERROR' && !cancelled ? 'error' : 'idle',
    compactions: conversation.compactions?.map(item => item === operation ? { ...item, status } : item),
  }
}
