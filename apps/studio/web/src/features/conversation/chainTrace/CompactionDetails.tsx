import { ChevronDown } from 'lucide-react'
import type { TraceGraphNode } from '../../../api/conversation/traceGraph'
import { CodeText } from '../../../components/ui/CodeText'
import { useI18n } from '../../../i18n'
import type { JsonValue } from '../../../types'
import { MarkdownContent } from '../components/MarkdownContent'
import { traceContentText } from './tracePresentation'

const record = (value: JsonValue | undefined | null) => value != null && typeof value === 'object' && !Array.isArray(value) ? value : undefined

/** 压缩输入和摘要分别归入对应页签，模型用量在模型详情查看 */
export function CompactionDetails({ operation, models, section }: {
  operation: TraceGraphNode
  models: TraceGraphNode[]
  section: 'request' | 'result'
}) {
  const { t } = useI18n()
  const result = record(operation.result)
  const summary = result?.generated_summary ?? result?.summary
  const input = record(operation.request)
  const messages = Array.isArray(input?.messages) ? input.messages : []
  if (section === 'result') return (
    <section className="chain-trace-compaction-details">
      {typeof summary === 'string' && summary
        ? <MarkdownContent content={summary} variant="compact" />
        : <p className="chain-trace-compaction-note">{operation.resultOmitted
          ? t('结果内容未保留')
          : result?.status === 'not_reduced' || result?.status === 'compacted'
            ? t('摘要内容未保留') : t('暂无摘要内容')}</p>}
      {operation.failure && <pre><CodeText language="json">{JSON.stringify(operation.failure, null, 2)}</CodeText></pre>}
    </section>
  )
  const firstModel = models[0]
  return (
    <div className="chain-trace-compaction-details">
      <section>
        {operation.requestOmitted ? <p className="chain-trace-compaction-note">{t('本次压缩的原始内容未保留')}</p> : messages.length === 0 ? <p className="chain-trace-compaction-note">{t('暂无可压缩的历史')}</p> : messages.map((message, index) => {
          const item = record(message)
          return <section className="chain-trace-compaction-history" key={typeof item?.id === 'string' ? item.id : index}>
            <MarkdownContent content={traceContentText(item?.content)} variant="compact" />
          </section>
        })}
      </section>
      {firstModel && (firstModel.request != null || firstModel.requestOmitted) && <details>
        <summary><ChevronDown size={14} aria-hidden="true" />{t('摘要模型输入')}</summary>
        {firstModel.requestOmitted
          ? <p className="chain-trace-compaction-note">{t('请求内容未保留')}</p>
          : <pre><CodeText language="json">{JSON.stringify(firstModel.request ?? null, null, 2)}</CodeText></pre>}
      </details>}
    </div>
  )
}
