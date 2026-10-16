import { ChevronDown } from 'lucide-react'
import { useState } from 'react'
import type { TraceGraphNode } from '../../../api/conversation/traceGraph'
import { CodeText } from '../../../components/ui/CodeText'
import { FeedbackState } from '../../../components/ui'
import { useI18n } from '../../../i18n'
import type { JsonValue } from '../../../types'
import { MarkdownContent } from '../components/MarkdownContent'
import { traceContentText } from './tracePresentation'
import { useTraceModelRequest } from './useTraceModelRequest'

const record = (value: JsonValue | undefined | null) => value != null && typeof value === 'object' && !Array.isArray(value) ? value : undefined

/** 压缩输入和摘要分别归入对应页签，模型用量在模型详情查看 */
export function CompactionDetails({ threadId, operation, models, section }: {
  threadId: string
  operation: TraceGraphNode
  models: TraceGraphNode[]
  section: 'request' | 'result'
}) {
  const { t } = useI18n()
  const result = record(operation.result)
  const summary = result?.generated_summary ?? result?.summary
  const input = record(operation.request)
  const messages = Array.isArray(input?.messages) ? input.messages : []
  const firstModel = models[0]
  const [requestOpen, setRequestOpen] = useState(false)
  const modelRequest = useTraceModelRequest({
    threadId, nodeId: firstModel?.id ?? '', reference: firstModel?.requestReference,
    enabled: section === 'request' && requestOpen,
  })
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
      {firstModel && (firstModel.requestReference || firstModel.requestOmitted) && <details onToggle={event => setRequestOpen(event.currentTarget.open)}>
        <summary><ChevronDown size={14} aria-hidden="true" />{t('摘要模型输入')}</summary>
        {firstModel.requestOmitted
          ? <p className="chain-trace-compaction-note">{t('请求内容未保留')}</p>
          : modelRequest.state.phase === 'ready'
            ? <pre><CodeText language="json">{JSON.stringify(modelRequest.state.detail.request, null, 2)}</CodeText></pre>
            : <FeedbackState kind={modelRequest.state.phase === 'error' ? 'error' : 'loading'}
                appearance={modelRequest.state.phase === 'error' ? 'retry' : 'default'}
                title={t(modelRequest.state.phase === 'error' ? '模型请求加载失败' : '正在加载模型请求')}
                retryLabel={t('重新加载')} onRetry={modelRequest.state.phase === 'error' ? modelRequest.retry : undefined} />}
      </details>}
    </div>
  )
}
