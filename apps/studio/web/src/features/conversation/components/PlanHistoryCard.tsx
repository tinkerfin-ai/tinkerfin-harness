import type { PlanInteraction } from '../../../types'
import { useI18n } from '../../../i18n'
import { MarkdownContent } from './MarkdownContent'

/** 展示已结束的计划交互，历史内容不再提供提交或执行入口 */
export function PlanHistoryCard({ interaction }: { interaction: PlanInteraction }) {
  const { t } = useI18n()
  return <details className="plan-history-card">
    <summary>{interaction.kind === 'questions' ? interaction.title : interaction.draft.content.description} · {t('已结束')}</summary>
    <div className="plan-history-card__body">
      {interaction.kind === 'review'
        ? <MarkdownContent content={interaction.draft.content.markdown} />
        : <ol>{interaction.questions.map(question => <li key={question.id}>{question.prompt}{!question.required && `（${t('可选')}）`}
          {(question.answerType === 'single_choice' || question.answerType === 'multiple_choice') && <ul>{question.options.map(option => <li key={option.id}>{option.label}{option.description && `：${option.description}`}</li>)}</ul>}
        </li>)}</ol>}
    </div>
  </details>
}
