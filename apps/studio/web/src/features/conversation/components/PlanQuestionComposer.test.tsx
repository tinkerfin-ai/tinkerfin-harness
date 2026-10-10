import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { PlanQuestionState } from '../../../types'
import conversationStyles from '../conversation.css?raw'
import { PlanQuestionComposer } from './PlanQuestionComposer'

const interaction = (): PlanQuestionState => ({
  kind: 'questions',
  interruptId: 'plan-question-1',
  title: '确认部署约束',
  description: '这些答案会影响计划范围与验证方式',
  activeQuestionIndex: 0,
  form: { questions: [] },
  submitted: false,
  questions: [
    {
      id: 'environment',
      answerType: 'single_choice',
      prompt: '部署到哪个环境？',
      required: true,
      options: [
        { id: 'staging', label: '预发布', description: '先验证再上线', recommended: true },
        { id: 'production', label: '生产', recommended: false },
      ],
      allowFreeText: true,
    },
    {
      id: 'deadline',
      answerType: 'date',
      prompt: '交付时间有什么偏好？',
      required: false,
    },
    {
      id: 'notes',
      answerType: 'text',
      prompt: '还有其他补充吗？',
      required: false,
    },
  ],
})

describe('PlanQuestionComposer', () => {
  beforeEach(() => window.sessionStorage.clear())
  afterEach(() => vi.useRealTimers())


  it('确认中可浏览前后问题及原答案，浏览不修改已提交表单', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(<PlanQuestionComposer threadId="confirmation-reading"
      interaction={{ ...interaction(), submitted: true, activeQuestionIndex: 1, questions: [
        { id: 'first', answerType: 'text', prompt: '第一题？', required: true, answer: '第一题原答案' },
        { id: 'second', answerType: 'text', prompt: '第二题？', required: false, answer: '第二题原答案' },
      ] }} onChange={onChange} onSubmit={vi.fn()} onClose={vi.fn()}
    />)
    expect(screen.getByRole('textbox')).toHaveValue('第二题原答案')
    await user.click(screen.getByRole('button', { name: '浏览上一题' }))
    expect(screen.getByRole('textbox')).toHaveValue('第一题原答案')
    await user.click(screen.getByRole('button', { name: '浏览下一题' }))
    expect(screen.getByRole('textbox')).toHaveValue('第二题原答案')
    expect(onChange).not.toHaveBeenCalled()
  })

  it('restores focus to the selected radio when revisiting an answered question', async () => {
    const answered = interaction()
    const firstQuestion = answered.questions[0]
    if (!firstQuestion || firstQuestion.answerType !== 'single_choice') {
      throw new Error('测试夹具首题必须是单选题')
    }
    answered.questions[0] = {
      ...firstQuestion,
      selectedOptionId: 'production',
    }

    render(
      <PlanQuestionComposer onClose={vi.fn()}
        threadId="thread-a"
        interaction={answered}
        onChange={vi.fn()}
        onSubmit={vi.fn()}
      />,
    )

    const selected = screen.getByRole('radio', { name: '生产' })
    await waitFor(() => expect(selected).toHaveFocus())
    expect(selected).toHaveAttribute('tabindex', '0')
    expect(screen.getByRole('radio', { name: /预发布/ })).toHaveAttribute('tabindex', '-1')

  })

  it('最后一道必选题完成前禁用提交，选择答案后才允许提交', async () => {
    const user = userEvent.setup()
    let current = { ...interaction(), questions: [interaction().questions[0]!] }
    const submit = vi.fn()
    const change = (updater: (value: PlanQuestionState) => PlanQuestionState) => { current = updater(current) }
    const view = render(<PlanQuestionComposer onClose={vi.fn()} threadId="thread-a" interaction={current} onChange={change} onSubmit={submit} />)
    expect(screen.queryByRole('button', { name: '跳过本题' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '提交' })).toBeDisabled()
    await user.click(screen.getByRole('button', { name: '提交' }))
    expect(submit).not.toHaveBeenCalled()
    expect(current.error).toBeUndefined()
    await user.click(screen.getByRole('radio', { name: /预发布/ }))
    view.rerender(<PlanQuestionComposer onClose={vi.fn()} threadId="thread-a" interaction={current} onChange={change} onSubmit={submit} />)
    expect(screen.getByRole('button', { name: '提交' })).toBeEnabled()
    await user.click(screen.getByRole('button', { name: '提交' }))
    expect(submit).toHaveBeenCalledOnce()
  })

  it('enforces multiple-choice limits across options and custom text', () => {
    let current: PlanQuestionState = {
      ...interaction(),
      questions: [{
        id: 'platforms',
        answerType: 'multiple_choice',
        prompt: '覆盖哪些平台？',
        required: true,
        options: [
          { id: 'web', label: 'Web', recommended: true },
          { id: 'mobile', label: '移动端', recommended: true },
          { id: 'desktop', label: '桌面端', recommended: false },
        ],
        allowFreeText: true,
        minSelections: 1,
        maxSelections: 2,
        selectedOptionIds: [],
      }],
      activeQuestionIndex: 0,
    }
    const change = (updater: (value: PlanQuestionState) => PlanQuestionState) => {
      current = updater(current)
    }
    const submit = vi.fn()
    const view = render(
      <PlanQuestionComposer onClose={vi.fn()} threadId="thread-a" interaction={current} onChange={change} onSubmit={submit} />,
    )

    expect(screen.getByText('选择 1 至 2 项，自定义回答计作一项')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('checkbox', { name: /Web/ }))
    view.rerender(
      <PlanQuestionComposer onClose={vi.fn()} threadId="thread-a" interaction={current} onChange={change} onSubmit={submit} />,
    )
    fireEvent.click(screen.getByRole('checkbox', { name: /移动端/ }))
    view.rerender(
      <PlanQuestionComposer onClose={vi.fn()} threadId="thread-a" interaction={current} onChange={change} onSubmit={submit} />,
    )
    expect(screen.getByRole('checkbox', { name: /桌面端/ })).toBeDisabled()
    expect(screen.getByRole('textbox', { name: /自定义回答/ })).toBeDisabled()

    current = {
      ...current,
      questions: current.questions.map((question) => question.answerType === 'multiple_choice'
        ? { ...question, customAnswer: '其他平台' }
        : question),
    }
    view.rerender(
      <PlanQuestionComposer onClose={vi.fn()} threadId="thread-a" interaction={current} onChange={change} onSubmit={submit} />,
    )
    fireEvent.click(screen.getByRole('button', { name: '提交' }))
    expect(submit).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: '提交' })).toBeDisabled()
    expect(current.error).toBeUndefined()
  })

  it('keeps answer controls accessible in touch, forced-colors and reduced-motion modes', () => {
    expect(conversationStyles).toMatch(/@media \(forced-colors: active\)[\s\S]*\.plan-question-composer-footer::before,\s*\.plan-review-composer-footer::before\s*\{[^}]*background:\s*Canvas;/s)
    expect(conversationStyles).toMatch(/@media \(any-hover: none\), \(any-pointer: coarse\)[\s\S]*\.plan-question-composer\.is-minimized \.plan-question-progress,[\s\S]*\.plan-question-composer\.is-minimized \.plan-question-progress-step\s*\{[^}]*height:\s*var\(--control-lg\);/s)
    expect(conversationStyles).toMatch(/@media \(any-hover: none\), \(any-pointer: coarse\)[\s\S]*\.plan-question-date\s*\{[^}]*height:\s*var\(--control-lg\);/s)
    expect(conversationStyles).toMatch(/@media \(forced-colors: active\)[\s\S]*\.plan-question-option:focus-visible,[\s\S]*outline:\s*2px solid Highlight;/s)
    expect(conversationStyles).toMatch(/@media \(any-hover: none\), \(any-pointer: coarse\)[\s\S]*\.plan-question-pager-button\s*{[^}]*min-width:\s*var\(--control-lg\);[^}]*min-height:\s*var\(--control-lg\);/s)
    expect(conversationStyles).toMatch(/@media \(prefers-reduced-motion: reduce\)[\s\S]*\.plan-question-pager-button \.ui-icon-button__icon\s*{\s*transition:\s*none;/s)
  })
})
