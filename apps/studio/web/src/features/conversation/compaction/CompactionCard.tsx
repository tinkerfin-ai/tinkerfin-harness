import { ChevronDown, LoaderCircle, RotateCw, SquareTerminal } from 'lucide-react'
import { useI18n, type TranslationKey } from '../../../i18n'
import { Button } from '../../../components/ui'
import { MarkdownContent } from '../components/MarkdownContent'
import type { ContextCompaction } from './state'
import './compaction.css'

const labels: Record<ContextCompaction['status'], TranslationKey> = {
  generating: '正在整理上下文',
  saving: '正在保存压缩结果',
  compacted: '上下文已压缩',
  nothing_to_compact: '暂无可压缩的历史',
  not_reduced: '上下文未缩短，已保留原内容',
  cancelled: '压缩已停止',
  failed: '压缩未完成',
  unconfirmed: '压缩结果尚未确认',
}

/** 操作记录不占用聊天消息角色；成功摘要默认折叠并以只读方式展开 */
export function CompactionCard({ operation, onReload }: { operation: ContextCompaction; onReload?: () => void }) {
  const { t } = useI18n()
  const busy = operation.status === 'generating' || operation.status === 'saving'
  const label = operation.status === 'compacted' && operation.compactedMessages != null
    ? t('已压缩 {count} 条历史消息', { count: operation.compactedMessages })
    : t(labels[operation.status])
  const title = <>
    {busy
      ? <LoaderCircle className="compaction-card__icon compaction-card__spinner" size={16} aria-hidden="true" />
      : <SquareTerminal className="compaction-card__icon" size={16} aria-hidden="true" />}
    <span className="compaction-card__heading">
      <span className="compaction-card__command">compact_conversation</span>
      <span className="compaction-card__result">
        <span aria-hidden="true">·</span>
        <span>{label}</span>
      </span>
    </span>
  </>
  if (operation.status === 'compacted' && operation.summary) return (
    <details id={`compaction:${operation.runId}`} className="compaction-card">
      <summary className="compaction-card__row">{title}<ChevronDown className="compaction-card__chevron" size={14} aria-hidden="true" /></summary>
      <div className="compaction-card__summary"><MarkdownContent content={operation.summary} /></div>
    </details>
  )
  return <section id={`compaction:${operation.runId}`} className="compaction-card" aria-label={label}>
    <div className="compaction-card__row" role="status">{title}</div>
    {(operation.status === 'failed' || operation.status === 'unconfirmed') && <div className="compaction-card__hint">
      <p>{t('请重新加载会话确认结果，原聊天记录仍可查看')}</p>
      {onReload && <Button type="button" variant="ghost" size="lg" leadingIcon={<RotateCw size={14} />} onClick={onReload}>{t('重新加载')}</Button>}
    </div>}
  </section>
}
