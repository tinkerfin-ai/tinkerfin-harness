import type { TraceGraphNode } from '../../../api/conversation/traceGraph'
import { useI18n } from '../../../i18n'
import {
  durationLabel,
  elapsedMilliseconds,
  traceKindCompactLabel,
  traceNodePreview,
  traceNodePreviewFallback,
  traceNodeName,
  traceVisualCategory,
} from './tracePresentation'

export function TraceNodeCopy({
  node,
  showKind = true,
  showPreview = true,
}: {
  node: TraceGraphNode
  showKind?: boolean
  showPreview?: boolean
}) {
  const { t } = useI18n()
  const nodePreview = node.kind === 'context' && node.contextKind === 'compaction' ? t('压缩') : traceNodePreview(node, t)
  const previewFallback = traceNodePreviewFallback(node, t)
  const preview = nodePreview || previewFallback || ''
  const fallbackPreview = !nodePreview && Boolean(previewFallback)
  const title = node.kind.endsWith('_message') || node.kind === 'context'
    || (node.kind === 'custom' && node.contextKind === 'compaction')
    ? ''
    : node.kind === 'tool' ? node.name : traceNodeName(node, t)
  return (
    <span className="chain-trace-node-copy">
      {(title || showKind) && (
        <span className="chain-trace-node-title">
          {title && <strong>{title}</strong>}
          {showKind && <small>{traceKindCompactLabel(node, t)}</small>}
        </span>
      )}
      {showPreview && preview && (
        <span className={[
          'chain-trace-node-content',
          node.failure ? 'is-error' : '',
          fallbackPreview ? 'is-muted' : '',
          !title ? 'is-primary' : '',
        ].filter(Boolean).join(' ')}>
          {preview}
        </span>
      )}
    </span>
  )
}

export function TraceNodeType({ node }: { node: TraceGraphNode }) {
  const { t } = useI18n()
  const category = traceVisualCategory(node.kind)
  return (
    <span className={`chain-trace-type-pill is-category-${category}`}>
      {traceKindCompactLabel(node, t)}
    </span>
  )
}

export function TraceNodeMeta({ node }: { node: TraceGraphNode }) {
  const { t } = useI18n()
  const elapsed = elapsedMilliseconds(node)
  return (
    <span className="chain-trace-node-meta">
      {node.failure && <span className="chain-trace-error-badge">{t('错误')}</span>}
      {!node.failure && node.status === 'waiting' && (
        <span className="chain-trace-waiting-badge">{t('等待中')}</span>
      )}
      {!node.failure && node.status === 'running' && (
        <span className="chain-trace-running-badge">{t('运行中')}</span>
      )}
      {elapsed != null && (
        <span className="chain-trace-duration">{durationLabel(elapsed, t)}</span>
      )}
    </span>
  )
}
