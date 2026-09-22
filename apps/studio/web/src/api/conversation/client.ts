import type { ChatRequestPayload, CompactRequestPayload, ConversationAgUiEvent } from './types'
import { conversationChatUrl } from './config'
import { ConversationError } from './errors'
import { parseConversationAgUiEvent } from './eventParser'
import { requestEventStream, requestJson } from '../shared/http'
import { parseJsonSseStream } from './sse'

export interface StreamedAgUiEvent {
  event: ConversationAgUiEvent
  /** SSE `id:` 行提供的持久化事件序号；生产端省略时为 null */
  seq: number | null
}

async function* parseAgUiSseStream(
  body: ReadableStream<Uint8Array>,
  signal?: AbortSignal,
): AsyncGenerator<StreamedAgUiEvent> {
  for await (const frame of parseJsonSseStream(body, signal)) {
    const parsedId = frame.id != null && /^\d+$/.test(frame.id)
      ? Number(frame.id)
      : Number.NaN
    const seq = Number.isSafeInteger(parsedId) && parsedId > 0 ? parsedId : null
    try {
      yield { event: parseConversationAgUiEvent(frame.data), seq }
    } catch (error) {
      throw new ConversationError('stream_event_invalid', error)
    }
  }
}

async function* streamConversationEvents(
  payload: ChatRequestPayload,
  signal?: AbortSignal,
  afterSeq?: number,
): AsyncGenerator<StreamedAgUiEvent> {
  const response = await requestEventStream(conversationChatUrl(), {
    method: 'POST',
    body: payload,
    signal,
    headers: afterSeq == null
      ? undefined
      : { 'Last-Event-ID': String(afterSeq) },
    suppressGlobalError: true,
  })
  if (!response.body) throw new ConversationError('stream_body_missing')
  for await (const item of parseAgUiSseStream(response.body, signal)) {
    yield item
  }
}

export const startConversationRun = streamConversationEvents
export const resumeConversationRun = streamConversationEvents

export async function* compactConversationContext(
  payload: CompactRequestPayload,
  signal?: AbortSignal,
  afterSeq?: number,
): AsyncGenerator<StreamedAgUiEvent> {
  const response = await requestEventStream(`/api/conversation/${encodeURIComponent(payload.threadId)}/compact`, {
    method: 'POST',
    body: { runId: payload.runId, model: payload.model },
    signal,
    headers: afterSeq == null ? undefined : { 'Last-Event-ID': String(afterSeq) },
    suppressGlobalError: true,
  })
  if (!response.body) throw new ConversationError('stream_body_missing')
  yield* parseAgUiSseStream(response.body, signal)
}

export interface CancelConversationRunResult {
  cancelled: boolean
}

export const cancelConversationRun = (
  threadId: string,
  runId: string,
  signal?: AbortSignal,
): Promise<CancelConversationRunResult> => requestJson<CancelConversationRunResult>(
  `/api/conversation/${encodeURIComponent(threadId)}/runs/${encodeURIComponent(runId)}/cancel`,
  {
    method: 'POST',
    signal,
    suppressGlobalError: true,
  },
)
