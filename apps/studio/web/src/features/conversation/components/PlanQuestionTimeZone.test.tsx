import { render, screen } from '@testing-library/react'
import { expect, it } from 'vitest'
import { LocaleProvider } from '../../../i18n'
import { PlanQuestionComposer } from './PlanQuestionComposer'

it.each(['zh-CN', 'en'])('时间与日期时间回答展示并关联权威时区（%s）', locale => {
  localStorage.setItem('tinkerfin:language', locale)
  const base = { id: 'question', prompt: '何时开始', required: true, timeZone: 'America/New_York' }
  const interaction = {
    kind: 'questions' as const, interruptId: 'time-zone-question', title: '安排时间', description: '',
    activeQuestionIndex: 0, form: {}, submitted: false,
  }
  const actions = { onChange: () => {}, onSubmit: () => {}, onClose: () => {} }
  const { rerender } = render(<LocaleProvider><PlanQuestionComposer threadId="time-zone-thread" {...actions}
    interaction={{ ...interaction, questions: [{ ...base, answerType: 'time', time: '09:00' }] }} /></LocaleProvider>)
  const description = `${locale === 'en' ? 'Time zone: ' : '时区：'}America/New_York`
  expect(screen.getByText(description)).toBeVisible()
  expect(screen.getByRole('button', { name: /^(时间回答|Time answer)/ })).toHaveAccessibleDescription(description)
  rerender(<LocaleProvider><PlanQuestionComposer threadId="time-zone-thread" {...actions}
    interaction={{ ...interaction, questions: [{ ...base, answerType: 'datetime', dateTime: '2026-10-09T09:00' }] }} /></LocaleProvider>)
  expect(screen.getByRole('button', { name: /^(时间回答|Time answer)/ })).toHaveAccessibleDescription(description)
  expect(screen.getByRole('button', { name: /^(日期回答|Date answer)/ })).toHaveAccessibleDescription(description)
})
