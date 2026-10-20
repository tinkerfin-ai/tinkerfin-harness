import { ChevronDown, CircleHelp, Route } from 'lucide-react'
import { useId, useState } from 'react'
import type { PlanResult } from '../../../api/conversation/planResults'
import type { PlanClarificationAnswer } from '../../../api/conversation/types'
import type { DeepReadonly, PlanInteraction, PlanQuestionItem } from '../../../types'
import { useI18n } from '../../../i18n'
import { MarkdownContent } from './MarkdownContent'

function answerText(question: DeepReadonly<PlanQuestionItem>, answer: DeepReadonly<PlanClarificationAnswer> | undefined, unavailable: string, skipped: string): string {
  if (!answer) return unavailable
  if (answer.status === 'skipped') return skipped
  if (answer.answerType !== question.answerType) return unavailable
  switch (answer.answerType) {
    case 'single_choice': return question.answerType === 'single_choice'
      ? answer.customAnswer ?? question.options.find(option => option.id === answer.optionId)?.label ?? unavailable : unavailable
    case 'multiple_choice': return question.answerType === 'multiple_choice'
      ? [...answer.optionIds.map(id => question.options.find(option => option.id === id)?.label ?? unavailable), ...(answer.customAnswer ? [answer.customAnswer] : [])].join('、') : unavailable
    case 'text': return answer.answer
    case 'date': return answer.date
    case 'time': return `${answer.time.slice(0, 5)}${'timeZone' in question ? ` (${question.timeZone})` : ''}`
    case 'datetime': return `${answer.dateTime.replace('T', ' ').slice(0, 16)}${'timeZone' in question ? ` (${question.timeZone})` : ''}`
  }
}

/** 按已保存结果展示交互历史，原计划和问题不再提供执行入口 */
export function PlanHistoryCard({ interaction, result, id }: {
  interaction: DeepReadonly<PlanInteraction>
  result?: DeepReadonly<PlanResult>
  id?: string
}) {
  const { t } = useI18n()
  const titleId = useId()
  const [open, setOpen] = useState(false)
  const labels = { answered: '已回答', approved: '已批准', rejected: '已拒绝', dismissed: '已关闭', cancelled: '已取消', discussed: '已反馈' } as const
  const skippedAll = result?.outcome === 'answered' && result.answers && Object.values(result.answers).length > 0
    && Object.values(result.answers).every(answer => answer.status === 'skipped')
  const status = skippedAll ? t('已跳过') : result ? t(labels[result.outcome]) : t('结果不可用')
  const title = interaction.kind === 'questions' ? interaction.title : interaction.draft.content.description
  const answers = interaction.kind === 'questions' ? interaction.questions.map(question => ({
    id: question.id, prompt: question.prompt,
    value: answerText(question, result?.answers?.[question.id], t('结果不可用'), t('已跳过')),
  })) : []
  const summary = result?.reason ?? (interaction.kind === 'questions' && result?.outcome === 'answered'
    ? answers.map(answer => answer.value).join(' · ') : '')
  return <details id={id} className={`plan-history-card is-${interaction.kind}`} open={open} aria-label={`${title} · ${status}`}
    onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>
      <span className="plan-history-card-heading">
        <span className="plan-history-card-icon" aria-hidden="true">{interaction.kind === 'questions' ? <CircleHelp size={14} strokeWidth={2} /> : <Route size={14} strokeWidth={2} />}</span>
        <span className="plan-history-card-title" id={titleId}>{title}</span>
        <span className="plan-history-card-status">{status}</span>
        <ChevronDown className="plan-history-card-chevron" size={14} strokeWidth={2} aria-hidden="true" />
      </span>
      {!open && summary && <span className="plan-history-card-preview">{summary}</span>}
    </summary>
    <div className="plan-history-card-body" role="region" aria-labelledby={titleId}>
      {result?.reason && <p className="plan-history-card-decision">{result.reason}</p>}
      {interaction.kind === 'review'
        ? <MarkdownContent content={interaction.draft.content.markdown} />
        : result && result.outcome !== 'answered'
          ? <ul className="plan-history-card-questions">{answers.map(answer => <li key={answer.id}>{answer.prompt}</li>)}</ul>
          : <dl>{answers.map(answer => <div key={answer.id}><dt>{answer.prompt}</dt><dd>{answer.value}</dd></div>)}</dl>}
    </div>
  </details>
}
