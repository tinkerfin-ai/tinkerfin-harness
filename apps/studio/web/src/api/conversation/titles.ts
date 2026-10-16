import { requestJson } from '../shared/http'
import { ConversationError } from './errors'

export interface ConversationTitleSnapshot {
  threadId: string
  title: string
  titleSource: 'default' | 'generated' | 'user' | 'unknown'
  titleGenerationStatus: 'idle' | 'running' | 'succeeded' | 'failed' | 'skipped'
  titleSeq: number
}

export function isConversationTitle(value: unknown): value is ConversationTitleSnapshot {
  if (!value || typeof value !== 'object') return false
  const item = value as Record<string, unknown>
  return typeof item.threadId === 'string' && typeof item.title === 'string'
    && Array.from(item.title).length >= 1 && Array.from(item.title).length <= 32
    && typeof item.titleSource === 'string' && ['default', 'generated', 'user', 'unknown'].includes(item.titleSource)
    && typeof item.titleGenerationStatus === 'string' && ['idle', 'running', 'succeeded', 'failed', 'skipped'].includes(item.titleGenerationStatus)
    && typeof item.titleSeq === 'number' && Number.isSafeInteger(item.titleSeq) && item.titleSeq >= 0
}

export async function fetchConversationTitle(threadId: string, signal: AbortSignal): Promise<ConversationTitleSnapshot> {
  const value = await requestJson<unknown>(`/api/conversation/${encodeURIComponent(threadId)}/title`, { signal, suppressGlobalError: true })
  if (!isConversationTitle(value) || value.threadId !== threadId) throw new ConversationError('stream_event_invalid', '标题快照无效')
  return value
}
