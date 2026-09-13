import { isConversationTitle } from "./titles"
import { isAttachment } from '../../features/conversation/attachments/content'
import type { JsonObject, JsonValue } from '../../types'
import type {
  ConversationAgUiEvent,
  EventSourceInfo,
  RawEventContext,
} from './types'

const INVALID_EVENT_MESSAGE = '事件流包含无效的 AG-UI 事件'
const MAX_JSON_DEPTH = 64

const isRecord = (value: unknown): value is Record<string, unknown> => (
  value !== null && typeof value === 'object' && !Array.isArray(value)
)

const isStringArray = (value: unknown): value is string[] => (
  Array.isArray(value) && value.every((item) => typeof item === 'string')
)

const isJsonValue = (value: unknown): value is JsonValue => {
  const pending: Array<{ value: unknown; leaving: boolean; depth: number }> = [{
    value,
    leaving: false,
    depth: 1,
  }]
  const activeAncestors = new Set<object>()

  while (pending.length > 0) {
    const frame = pending.pop()
    if (!frame) continue
    const current = frame.value
    if (
      current === null
      || typeof current === 'string'
      || typeof current === 'boolean'
      || (typeof current === 'number' && Number.isFinite(current))
    ) continue

    if (typeof current !== 'object') return false
    if (frame.depth > MAX_JSON_DEPTH) return false
    if (frame.leaving) {
      activeAncestors.delete(current)
      continue
    }
    if (activeAncestors.has(current)) return false
    activeAncestors.add(current)
    pending.push({ value: current, leaving: true, depth: frame.depth })

    const children = Array.isArray(current) ? current : Object.values(current)
    for (const child of children) {
      pending.push({ value: child, leaving: false, depth: frame.depth + 1 })
    }
  }

  return true
}

const isJsonObject = (value: unknown): value is JsonObject => (
  isRecord(value) && isJsonValue(value)
)

const hasOptionalString = (
  value: Record<string, unknown>,
  key: string,
  allowNull = false,
) => value[key] === undefined
  || typeof value[key] === 'string'
  || (allowNull && value[key] === null)

const hasOptionalBoolean = (value: Record<string, unknown>, key: string) => (
  value[key] === undefined || typeof value[key] === 'boolean'
)

const isEventSourceInfo = (value: unknown): value is EventSourceInfo => {
  if (!isRecord(value)) return false
  const graphNamespace = value.graphNamespace
  if (!isStringArray(graphNamespace)) return false
  const commonFieldsValid = hasOptionalString(value, 'graphTaskId', true)
    && hasOptionalString(value, 'nodeName', true)
    && (value.parentGraphNamespace === undefined
      || value.parentGraphNamespace === null
      || isStringArray(value.parentGraphNamespace))
    && hasOptionalString(value, 'parentToolCallId', true)
    && hasOptionalString(value, 'subagentInput', true)
    && hasOptionalString(value, 'subagentInvocationId', true)
  if (!commonFieldsValid) return false

  switch (value.kind) {
    case 'root':
      return graphNamespace.length === 0
        && value.agentType === 'main'
        && typeof value.agentName === 'string'
        && value.agentName.length > 0
    case 'compiled_subgraph':
      return graphNamespace.length > 0
        && value.agentType === undefined
        && value.agentName === undefined
    case 'deep_agent_subagent':
      return graphNamespace.length > 0
        && value.agentType === 'subagent'
        && typeof value.agentName === 'string'
        && value.agentName.length > 0
    default:
      return false
  }
}

const isRawEventContext = (value: unknown): value is RawEventContext => {
  if (!isRecord(value)) return false
  return (value.streamMode === undefined
      || value.streamMode === 'messages'
      || value.streamMode === 'tasks'
      || value.streamMode === 'values')
    && (value.source === undefined || isEventSourceInfo(value.source))
    && hasOptionalString(value, 'runId')
    && hasOptionalString(value, 'relatedSubagentInvocationId')
    && hasOptionalString(value, 'parentToolCallId')
    && hasOptionalString(value, 'subagentInput')
    && hasOptionalString(value, 'langgraphNode')
    && hasOptionalString(value, 'interruptId')
    && hasOptionalBoolean(value, 'initializationFailed')
    && (value.toolResultStatus === undefined
      || value.toolResultStatus === 'success'
      || value.toolResultStatus === 'error')
}

const hasOptionalRawEvent = (value: Record<string, unknown>) => (
  value.rawEvent === undefined || isRawEventContext(value.rawEvent)
)

const isMessageSnapshot = (value: unknown) => (
  isRecord(value)
  && typeof value.id === 'string'
  && typeof value.role === 'string'
  && (value.role !== 'tool' || (typeof value.toolCallId === 'string' && value.toolCallId.length > 0 && typeof value.content === 'string'))
  && hasOptionalString(value, 'toolCallId')
  && hasOptionalString(value, 'error')
  && (value.content === undefined || isJsonValue(value.content))
  && (value.attachments === undefined || (Array.isArray(value.attachments) && value.attachments.every(isAttachment)))
)

const isJsonPointer = (value: string) => {
  if (value === '') return true
  if (!value.startsWith('/')) return false
  for (let index = 1; index < value.length; index += 1) {
    if (value[index] !== '~') continue
    const escaped = value[index + 1]
    if (escaped !== '0' && escaped !== '1') return false
    index += 1
  }
  return true
}

const isStateDeltaOperation = (value: unknown) => {
  if (!isRecord(value) || typeof value.path !== 'string' || !isJsonPointer(value.path)) {
    return false
  }
  if (value.op === 'remove') {
    return value.value === undefined || isJsonValue(value.value)
  }
  return (value.op === 'add' || value.op === 'replace')
    && Object.hasOwn(value, 'value')
    && isJsonValue(value.value)
}

export const isInterrupt = (value: unknown) => (
  isRecord(value)
  && typeof value.id === 'string'
  && typeof value.reason === 'string'
  && hasOptionalString(value, 'message', true)
  && hasOptionalString(value, 'toolCallId', true)
  && (value.responseSchema == null || isJsonObject(value.responseSchema))
  && (value.metadata == null || isJsonObject(value.metadata))
)

const isRunFinishedOutcome = (value: unknown) => {
  if (!isRecord(value)) return false
  if (value.type === 'success') return true
  return value.type === 'interrupt'
    && Array.isArray(value.interrupts)
    && value.interrupts.length > 0
    && value.interrupts.every(isInterrupt)
}

const isConversationAgUiEvent = (value: unknown): value is ConversationAgUiEvent => {
  if (!isRecord(value) || typeof value.type !== 'string') return false

  switch (value.type) {
    case 'RUN_STARTED':
      return typeof value.threadId === 'string'
        && typeof value.runId === 'string'
        && hasOptionalString(value, 'parentRunId')
        && (value.title === undefined || isConversationTitle(value))
        && hasOptionalRawEvent(value)
        && value.input === undefined

    case 'MESSAGES_SNAPSHOT':
      return hasOptionalRawEvent(value)
        && Array.isArray(value.messages)
        && value.messages.every(isMessageSnapshot)

    case 'STATE_SNAPSHOT':
      return hasOptionalRawEvent(value) && isJsonObject(value.snapshot)

    case 'STATE_DELTA':
      return hasOptionalRawEvent(value)
        && Array.isArray(value.delta)
        && value.delta.every(isStateDeltaOperation)

    case 'TEXT_MESSAGE_START':
      return hasOptionalRawEvent(value)
        && typeof value.messageId === 'string'
        && typeof value.role === 'string'
        && hasOptionalString(value, 'name')

    case 'TEXT_MESSAGE_CONTENT':
    case 'REASONING_MESSAGE_CONTENT':
      return hasOptionalRawEvent(value)
        && typeof value.messageId === 'string'
        && typeof value.delta === 'string'

    case 'TEXT_MESSAGE_END':
    case 'REASONING_START':
    case 'REASONING_MESSAGE_END':
    case 'REASONING_END':
      return hasOptionalRawEvent(value) && typeof value.messageId === 'string'

    case 'REASONING_MESSAGE_START':
      return hasOptionalRawEvent(value)
        && typeof value.messageId === 'string'
        && value.role === 'reasoning'

    case 'TOOL_CALL_START':
      return hasOptionalRawEvent(value)
        && typeof value.toolCallId === 'string'
        && typeof value.toolCallName === 'string'
        && hasOptionalString(value, 'parentMessageId')

    case 'TOOL_CALL_ARGS':
      return hasOptionalRawEvent(value)
        && typeof value.toolCallId === 'string'
        && typeof value.delta === 'string'

    case 'TOOL_CALL_END':
      return hasOptionalRawEvent(value) && typeof value.toolCallId === 'string'

    case 'TOOL_CALL_RESULT':
      return (value.attachments === undefined || (Array.isArray(value.attachments) && value.attachments.every(isAttachment)))
        && hasOptionalRawEvent(value)
        && typeof value.messageId === 'string'
        && typeof value.toolCallId === 'string'
        && typeof value.content === 'string'
        && typeof value.role === 'string'

    case 'CUSTOM':
      if (value.name === 'studio.conversation.title.updated') return hasOptionalRawEvent(value) && isConversationTitle(value.value)
      if (value.name === 'tinkerfin.message.attachments') return hasOptionalRawEvent(value) && isRecord(value.value) && typeof value.value.messageId === 'string' && Array.isArray(value.value.attachments) && value.value.attachments.every(isAttachment)
      return hasOptionalRawEvent(value)
        && typeof value.name === 'string'
        && isJsonValue(value.value)

    case 'RAW':
      return (value.rawEvent === undefined || isJsonObject(value.rawEvent))
        && isJsonObject(value.event)
        && hasOptionalString(value, 'source')

    case 'RUN_FINISHED':
      return hasOptionalRawEvent(value)
        && typeof value.threadId === 'string'
        && typeof value.runId === 'string'
        && (value.outcome === undefined || isRunFinishedOutcome(value.outcome))

    case 'RUN_ERROR':
      return hasOptionalRawEvent(value)
        && hasOptionalString(value, 'message')
        && hasOptionalString(value, 'code')
        && (value.details === undefined || isJsonValue(value.details))

    default:
      return false
  }
}

/** 将 JSON 边界中的未知值收窄为 Studio 当前支持的 AG-UI 事件 */
export const parseConversationAgUiEvent = (value: unknown): ConversationAgUiEvent => {
  if (!isConversationAgUiEvent(value)) throw new Error(INVALID_EVENT_MESSAGE)
  return value
}
