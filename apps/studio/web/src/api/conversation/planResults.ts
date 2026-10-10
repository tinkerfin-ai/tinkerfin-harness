import type { PlanClarificationAnswer } from './types'
import { ConversationError } from './errors'

const outcomes = ['answered', 'approved', 'rejected', 'dismissed', 'cancelled', 'discussed'] as const

export interface PlanResult {
  interruptId: string
  submissionRunId: string
  outcome: typeof outcomes[number]
  answers: Record<string, PlanClarificationAnswer> | null
  reason: string | null
}

const record = (value: unknown): value is Record<string, unknown> => (
  typeof value === 'object' && value !== null && !Array.isArray(value)
)
const nonempty = (value: unknown): value is string => typeof value === 'string' && value.trim().length > 0
const isOutcome = (value: unknown): value is PlanResult['outcome'] => outcomes.some(outcome => outcome === value)
const invalid = (): never => { throw new ConversationError('stream_event_invalid') }

function parseAnswer(value: unknown): PlanClarificationAnswer {
  if (!record(value)) return invalid()
  if (value.status === 'skipped') return { status: 'skipped' }
  if (value.status !== 'answered') return invalid()
  const status = 'answered'
  switch (value.answerType) {
    case 'single_choice':
      if (nonempty(value.optionId) && value.customAnswer == null) return { status, answerType: value.answerType, optionId: value.optionId }
      if (nonempty(value.customAnswer) && value.optionId == null) return { status, answerType: value.answerType, customAnswer: value.customAnswer }
      break
    case 'multiple_choice':
      if (Array.isArray(value.optionIds) && value.optionIds.every(nonempty)
        && new Set(value.optionIds).size === value.optionIds.length
        && (value.customAnswer == null || nonempty(value.customAnswer))
        && (value.optionIds.length > 0 || nonempty(value.customAnswer))) {
        return { status, answerType: value.answerType, optionIds: value.optionIds,
          ...(nonempty(value.customAnswer) ? { customAnswer: value.customAnswer } : {}) }
      }
      break
    case 'text': if (nonempty(value.answer)) return { status, answerType: value.answerType, answer: value.answer }; break
    case 'date': if (typeof value.date === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value.date)) return { status, answerType: value.answerType, date: value.date }; break
    case 'time': if (typeof value.time === 'string' && /^\d{2}:\d{2}:\d{2}$/.test(value.time)) return { status, answerType: value.answerType, time: value.time }; break
    case 'datetime': if (typeof value.dateTime === 'string' && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$/.test(value.dateTime)) return { status, answerType: value.answerType, dateTime: value.dateTime }; break
  }
  return invalid()
}

export function parsePlanResults(value: unknown): PlanResult[] {
  if (!Array.isArray(value)) return invalid()
  const ids = new Set<string>()
  return value.map(item => {
    if (!record(item) || !nonempty(item.interruptId) || !nonempty(item.submissionRunId)
      || !isOutcome(item.outcome) || !(item.reason === null || typeof item.reason === 'string')
      || !(item.answers === null || record(item.answers)) || ids.has(item.interruptId)) return invalid()
    ids.add(item.interruptId)
    const answers = item.answers === null ? null : Object.fromEntries(Object.entries(item.answers).map(([id, answer]) => [id, parseAnswer(answer)]))
    if (item.outcome === 'answered' && answers === null) return invalid()
    return { interruptId: item.interruptId, submissionRunId: item.submissionRunId, outcome: item.outcome, answers, reason: item.reason }
  })
}
