import { fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { PlanQuestionComposer } from './PlanQuestionComposer'

describe('PlanQuestionComposer', () => {
  it('确认中可浏览前后问题及原答案，浏览不修改已提交表单', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(<PlanQuestionComposer threadId="confirmation-reading"
      interaction={{ kind: 'questions', interruptId: 'plan-question-1', title: '确认部署约束', description: '这些答案会影响计划范围与验证方式', form: { questions: [] }, submitted: true, activeQuestionIndex: 1, questions: [
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

  it('已有多选答案和自定义文本合计超过上限时禁止提交', () => {
    const submit = vi.fn()
    const change = vi.fn()
    render(<PlanQuestionComposer threadId="over-limit" onClose={vi.fn()} onChange={change} onSubmit={submit}
      interaction={{ kind: 'questions', interruptId: 'plan-question-1', title: '确认范围', description: '',
        activeQuestionIndex: 0, form: { questions: [] }, submitted: false,
        questions: [{ id: 'platforms', answerType: 'multiple_choice', prompt: '覆盖哪些平台？', required: true,
          options: [{ id: 'web', label: 'Web', recommended: true }, { id: 'mobile', label: '移动端', recommended: true }],
          allowFreeText: true, minSelections: 1, maxSelections: 2, selectedOptionIds: ['web', 'mobile'], customAnswer: '其他平台',
        }],
      }} />)
    const button = screen.getByRole('button', { name: '提交' })
    expect(button).toBeDisabled()
    fireEvent.click(button)
    expect(submit).not.toHaveBeenCalled()
    expect(change).not.toHaveBeenCalled()
  })

})
