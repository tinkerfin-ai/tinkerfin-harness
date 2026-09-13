import type {
  TraceGraphNode,
  TraceGraphNodeKind,
} from '../../../api/conversation/traceGraph'
import type { useI18n } from '../../../i18n'
import type { JsonValue } from '../../../types'
import { messageAttachments } from '../attachments/content'

type Translate = ReturnType<typeof useI18n>['t']
export type TracePublicCategory =
  | 'user'
  | 'context'
  | 'model'
  | 'tool'
  | 'subagent'
  | 'assistant'

export const traceKindLabel = (kind: TraceGraphNodeKind, t: Translate) => ({
  human_message: t('用户消息'),
  assistant_message: t('助手消息'),
  context: t('上下文'),
  model: t('模型调用'),
  tool: t('工具'),
  subagent: t('子智能体'),
  memory: t('记忆'),
  guardrail: t('护栏'),
  retrieval: t('检索'),
  custom: t('上下文'),
  plan: t('计划'),
  interaction: t('人工交互'),
}[kind])

export const traceVisualCategory = (
  kind: TraceGraphNodeKind,
): TracePublicCategory => {
  switch (kind) {
    case 'human_message': return 'user'
    case 'assistant_message': return 'assistant'
    case 'model': return 'model'
    case 'tool': return 'tool'
    case 'subagent': return 'subagent'
    default: return 'context'
  }
}

export const traceVisualCategoryLabel = (
  category: TracePublicCategory,
  t: Translate,
) => ({
  user: t('用户'),
  context: t('上下文'),
  model: t('模型'),
  tool: t('工具'),
  subagent: t('子智能体'),
  assistant: t('助手'),
}[category])

export const traceKindCompactLabel = (
  kind: TraceGraphNodeKind,
  t: Translate,
) => traceVisualCategoryLabel(traceVisualCategory(kind), t)

export const traceNodeName = (node: TraceGraphNode) => node.name

export const traceContentText = (value: JsonValue | null | undefined) => {
  if (value == null) return ''
  if (typeof value === 'string') return value
  if (Array.isArray(value)) {
    return value.map((block) => (
      block !== null
      && typeof block === 'object'
      && !Array.isArray(block)
      && block.type === 'text'
      && typeof block.text === 'string'
        ? block.text
        : JSON.stringify(block)
    )).filter(Boolean).join('\n')
  }
  return JSON.stringify(value)
}

const compactContent = (value: JsonValue | null | undefined) => (
  traceContentText(value).replaceAll(/\s+/g, ' ').trim()
)

const messagePreview = (value: JsonValue | null | undefined) => {
  if (typeof value === 'string') return value.replaceAll(/\s+/g, ' ').trim()
  if (!Array.isArray(value)) return ''
  const visibleText = value.flatMap((block) => {
    if (typeof block === 'string') return [block]
    return block !== null && typeof block === 'object' && !Array.isArray(block)
      && block.type === 'text' && typeof block.text === 'string' ? [block.text] : []
  }).join(' ').replaceAll(/\s+/g, ' ').trim()
  return visibleText || messageAttachments(value).map(attachment => attachment.name).join(', ')
}

export const traceNodePreview = (node: TraceGraphNode) => {
  if (node.failure) return node.failure.message ?? node.failure.errorType
  if (node.kind.endsWith('_message')) return node.contentOmitted ? '' : messagePreview(node.content)
  if (node.kind === 'context') return compactContent(node.content)
  if (node.kind === 'model') return ''
  if (node.kind === 'tool' || node.kind === 'subagent') {
    return compactContent(node.request)
  }
  return compactContent(node.result) || compactContent(node.request)
}

export const traceNodePreviewFallback = (node: TraceGraphNode, t: Translate) => {
  if (node.contentOmitted) return ''
  if (node.kind === 'assistant_message' && node.toolCallOnly) return t('（仅工具调用）')
  if (node.kind.endsWith('_message') && Array.isArray(node.content)
    && node.content.some(block => block !== null && typeof block === 'object' && !Array.isArray(block)
      && typeof block.type === 'string' && ['file', 'image', 'document', 'audio', 'video'].includes(block.type))) {
    return t('附件')
  }
  return ''
}

export const traceNodeAccessibleLabel = (
  node: TraceGraphNode,
  t: Translate,
) => {
  const category = traceVisualCategoryLabel(traceVisualCategory(node.kind), t)
  const value = node.kind.endsWith('_message') || node.kind === 'context'
    ? traceNodePreview(node) || traceNodePreviewFallback(node, t)
    : node.kind === 'tool' ? node.name : traceNodeName(node)
  const summary = value.length > 96 ? `${value.slice(0, 95)}…` : value
  return `${category}，${summary || t('不可用')}`
}

export const traceStatusLabel = (
  status: TraceGraphNode['status'],
  t: Translate,
) => ({
  running: t('运行中'),
  waiting: t('等待中'),
  succeeded: t('已完成'),
  failed: t('失败'),
  cancelled: t('已取消'),
  abandoned: t('已放弃'),
  unknown: t('未知'),
}[status])

export const elapsedMilliseconds = (entry: TraceGraphNode) => {
  if (!entry.completedAt) return null
  return Math.max(0, Date.parse(entry.completedAt) - Date.parse(entry.startedAt))
}

export const durationLabel = (
  milliseconds: number | null,
  t: Translate,
) => {
  if (milliseconds == null) return t('进行中')
  if (milliseconds < 1000) {
    return t('{count} 毫秒', { count: Math.round(milliseconds) })
  }
  return t('{count} 秒', { count: (milliseconds / 1000).toFixed(2) })
}
