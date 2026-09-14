import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { PlanQuestionState } from '../../../types'
import { PlanQuestionComposer, PlanQuestionStatusRow } from './PlanQuestionComposer'
import conversationStyles from '../conversation.css?raw'

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

  it.each(['single_choice', 'multiple_choice'] as const)('长英文标题和说明完整保留，%s 选择语义不变', (answerType) => {
    const options = [
      { id: 'greeter', label: 'Fixed entrance greeter on peak lunch shift: menu guidance and pickup-line flow control', description: 'Existing staff only; directly targets conversion.', recommended: true },
      { id: 'upsell', label: 'Standard one-line upsell at order (large / add topping?)', description: 'Targets average ticket; keep base pricing unchanged.', recommended: false },
      { id: 'unchanged', label: 'Keep current service', recommended: false },
    ]
    const base = { id: 'actions', prompt: 'Which practical actions should the pilot run?', required: true, options, allowFreeText: true }
    let current: PlanQuestionState = { ...interaction(), questions: [answerType === 'single_choice'
      ? { ...base, answerType }
      : { ...base, answerType, minSelections: 1, maxSelections: 3, selectedOptionIds: [] }] }
    const change = (updater: (value: PlanQuestionState) => PlanQuestionState) => { current = updater(current) }
    const view = render(<PlanQuestionComposer threadId="business-actions" interaction={current} onChange={change} onSubmit={vi.fn()} />)
    const role = answerType === 'single_choice' ? 'radio' : 'checkbox'
    expect(screen.queryAllByText('推荐', { exact: true })).toHaveLength(answerType === 'single_choice' ? 1 : 0)
    for (const option of options) {
      expect(screen.getByRole(role, { name: new RegExp(option.label.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')) })).toBeVisible()
      if ('description' in option) expect(screen.getByText(option.description!)).toBeVisible()
    }
    fireEvent.click(screen.getByRole(role, { name: /^Fixed entrance greeter/ }))
    view.rerender(<PlanQuestionComposer threadId="business-actions" interaction={current} onChange={change} onSubmit={vi.fn()} />)
    expect(current.questions[0]).toMatchObject(answerType === 'single_choice'
      ? { selectedOptionId: 'greeter' } : { selectedOptionIds: ['greeter'] })
    expect(screen.getByRole(role, { name: /^Fixed entrance greeter/ })).toBeChecked()
  })

  it('takes one question at a time and auto-advances after a selection', () => {
    let current = interaction()
    const change = (updater: (value: PlanQuestionState) => PlanQuestionState) => {
      current = updater(current)
    }
    const view = render(
      <PlanQuestionComposer threadId="thread-a" interaction={current} onChange={change} onSubmit={vi.fn()} />,
    )

    expect(screen.getByRole('heading', {
      name: '确认部署约束 这些答案会影响计划范围与验证方式',
    })).toBeInTheDocument()
    const card = screen.getByRole('region', { name: 'Plan 澄清问题' })
    expect(card.querySelector('.plan-question-composer-head'))
      .toHaveTextContent('这些答案会影响计划范围与验证方式')
    expect(card.querySelector('.plan-question-composer-heading p')).not.toBeInTheDocument()
    expect(card.querySelector('.plan-interaction-card-description'))
      .toHaveTextContent('这些答案会影响计划范围与验证方式')
    expect(screen.getByRole('region', { name: '部署到哪个环境？' }))
      .not.toHaveTextContent('这些答案会影响计划范围与验证方式')
    expect(card.querySelector('.interaction-card-color-bridge.is-plan')).toBeInTheDocument()
    expect(screen.queryByText('规划前需要确认')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '放弃本次 Plan 澄清' })).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '部署到哪个环境？' })).toBeInTheDocument()
    expect(screen.queryByText('交付时间有什么偏好？')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '浏览上一题' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '浏览下一题' })).toBeDisabled()
    const next = screen.getByRole('button', { name: /^下一题$/ })
    expect(next).toBeDisabled()
    expect(next).toHaveClass('ui-button--sm', 'ui-button--capsule', 'ui-button--primary')
    expect(next).not.toHaveClass('ui-button--solid')
    expect(document.querySelector('.plan-question-composer-pager .ui-tooltip')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('radio', { name: /预发布/ }))
    expect(current.questions[0]).toMatchObject({ selectedOptionId: 'staging', skipped: false })
    expect(current.activeQuestionIndex).toBe(1)

    view.rerender(
      <PlanQuestionComposer threadId="thread-a" interaction={current} onChange={change} onSubmit={vi.fn()} />,
    )
    expect(screen.getByRole('heading', { name: /交付时间有什么偏好/ })).toBeInTheDocument()
    expect(screen.getByText('可选')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '跳过本题' }))
      .toHaveClass('ui-button--sm', 'ui-button--capsule', 'ui-button--secondary')
    expect(screen.getByRole('button', { name: /^下一题$/ }))
      .toHaveClass('ui-button--sm', 'ui-button--capsule', 'ui-button--primary')
    expect(screen.queryByRole('navigation', { name: '问题进度' })).not.toBeInTheDocument()
  })

  it('keeps both next-question controls bound to the same answer state and action', () => {
    let current = interaction()
    const change = (updater: (value: PlanQuestionState) => PlanQuestionState) => {
      current = updater(current)
    }
    const view = render(
      <PlanQuestionComposer threadId="thread-a" interaction={current} onChange={change} onSubmit={vi.fn()} />,
    )
    const browseNext = () => screen.getByRole('button', { name: '浏览下一题' })
    const next = () => screen.getByRole('button', { name: /^下一题$/ })

    expect(browseNext()).toBeDisabled()
    expect(next()).toBeDisabled()
    fireEvent.change(screen.getByRole('textbox', { name: /自定义回答/ }), {
      target: { value: '仅部署生产环境' },
    })
    view.rerender(
      <PlanQuestionComposer threadId="thread-a" interaction={current} onChange={change} onSubmit={vi.fn()} />,
    )
    expect(browseNext()).toBeEnabled()
    expect(next()).toBeEnabled()

    fireEvent.click(browseNext())
    expect(current.activeQuestionIndex).toBe(1)
    view.rerender(
      <PlanQuestionComposer threadId="thread-a" interaction={current} onChange={change} onSubmit={vi.fn()} />,
    )
    fireEvent.click(screen.getByRole('button', { name: '浏览上一题' }))
    expect(current.activeQuestionIndex).toBe(0)
    view.rerender(
      <PlanQuestionComposer threadId="thread-a" interaction={current} onChange={change} onSubmit={vi.fn()} />,
    )
    fireEvent.click(next())
    expect(current.activeQuestionIndex).toBe(1)
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
      <PlanQuestionComposer
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
    expect(selected.querySelector('.plan-question-option-index svg')).toBeInTheDocument()
  })

  it('returns focus to the invalid answer when current-question submission fails', async () => {
    const user = userEvent.setup()
    const unanswered = {
      ...interaction(),
      questions: [interaction().questions[0]],
    }
    let current = unanswered
    const change = (updater: (value: PlanQuestionState) => PlanQuestionState) => {
      current = updater(current)
    }
    const view = render(
      <PlanQuestionComposer
        threadId="thread-a"
        interaction={current}
        onChange={change}
        onSubmit={vi.fn()}
      />,
    )

    const firstAnswer = screen.getByRole('radio', { name: /预发布/ })
    expect(firstAnswer).not.toHaveFocus()
    const submit = screen.getByRole('button', { name: '提交' })
    await user.click(submit)
    await waitFor(() => expect(firstAnswer).toHaveFocus())
    view.rerender(
      <PlanQuestionComposer
        threadId="thread-a"
        interaction={current}
        onChange={change}
        onSubmit={vi.fn()}
      />,
    )

    expect(current.error).toBe('请回答所有必填的 Plan 澄清问题')
    await waitFor(() => expect(firstAnswer).toHaveFocus())
  })

  it('uses keyboard confirmation, supports optional skip and reports status', () => {
    let current = interaction()
    const change = (updater: (value: PlanQuestionState) => PlanQuestionState) => {
      current = updater(current)
    }
    const view = render(
      <PlanQuestionComposer threadId="thread-a" interaction={current} onChange={change} onSubmit={vi.fn()} />,
    )

    const first = screen.getByRole('radio', { name: /预发布/ })
    expect(screen.getByRole('radiogroup')).toHaveAttribute('aria-required', 'true')
    fireEvent.keyDown(first, { key: 'ArrowDown' })
    expect(screen.getByRole('radio', { name: '生产' })).toHaveFocus()
    expect(current.questions[0]).toMatchObject({ selectedOptionId: 'production' })
    fireEvent.keyDown(screen.getByRole('radio', { name: '生产' }), { key: 'Enter' })
    view.rerender(
      <PlanQuestionComposer threadId="thread-a" interaction={current} onChange={change} onSubmit={vi.fn()} />,
    )
    fireEvent.click(screen.getByRole('button', { name: '跳过本题' }))
    expect(current.questions[1]).toMatchObject({ skipped: true })

    const statusView = render(<PlanQuestionStatusRow interaction={current} />)
    expect(screen.getByText('等待回答')).toBeInTheDocument()
    expect(screen.queryByText('3 / 3')).not.toBeInTheDocument()
    expect(statusView.container.querySelectorAll('.activity-dots i')).toHaveLength(3)

    statusView.rerender(<PlanQuestionStatusRow interaction={{ ...current, submitted: true }} />)
    expect(statusView.container.querySelector('.activity-dots')).not.toBeInTheDocument()
  })

  it('collapses to the title, current prompt and progress without losing drafts', () => {
    let current = {
      ...interaction(),
      questions: interaction().questions.map((question, index) => index === 0
        ? { ...question, customAnswer: '保留这个草稿' }
        : question),
    }
    const change = (updater: (value: PlanQuestionState) => PlanQuestionState) => {
      current = updater(current)
    }
    const view = render(
      <PlanQuestionComposer threadId="thread-a" interaction={current} onChange={change} onSubmit={vi.fn()} />,
    )

    expect(screen.getByRole('separator', { name: '调整交互卡片高度' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '点击标题区域收起问题卡片' }))
    expect(screen.queryByRole('separator', { name: '调整交互卡片高度' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '展开问题卡片' })).toHaveAttribute('aria-expanded', 'false')
    expect(screen.getByRole('button', { name: '点击标题区域展开问题卡片' })).toBeInTheDocument()
    expect(screen.getByText('部署到哪个环境？')).toBeInTheDocument()
    expect(screen.queryByRole('radiogroup')).not.toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /查看第/ })).toHaveLength(3)
    fireEvent.click(screen.getByRole('button', { name: /查看第 3 题/ }))
    expect(current.activeQuestionIndex).toBe(2)
    expect(current.questions[0]).toMatchObject({ customAnswer: '保留这个草稿' })
    view.rerender(
      <PlanQuestionComposer threadId="thread-a" interaction={current} onChange={change} onSubmit={vi.fn()} />,
    )
    fireEvent.click(screen.getByRole('button', { name: '点击标题区域展开问题卡片' }))
    expect(screen.getByRole('separator', { name: '调整交互卡片高度' })).toBeInTheDocument()
    expect(screen.queryByRole('radiogroup')).not.toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: '自定义回答：还有其他补充吗？' })).toBeInTheDocument()
  })

  it('restores collapse state after remount and isolates it by conversation', async () => {
    const props = {
      interaction: interaction(),
      onChange: vi.fn(),
      onSubmit: vi.fn(),
    }
    const firstView = render(<PlanQuestionComposer threadId="thread-a" {...props} />)
    fireEvent.click(screen.getByRole('button', { name: '点击标题区域收起问题卡片' }))
    expect(window.sessionStorage.getItem('tinkerfin:plan-question-collapse:thread-a'))
      .toBe('collapsed')
    firstView.unmount()

    const restoredView = render(<PlanQuestionComposer threadId="thread-a" {...props} />)
    expect(screen.queryByRole('radiogroup')).not.toBeInTheDocument()

    restoredView.rerender(<PlanQuestionComposer threadId="thread-b" {...props} />)
    await waitFor(() => expect(screen.getByRole('radiogroup')).toBeInTheDocument())

    restoredView.rerender(<PlanQuestionComposer threadId="thread-a" {...props} />)
    await waitFor(() => expect(screen.queryByRole('radiogroup')).not.toBeInTheDocument())
  })

  it('blocks wheel propagation while the natural-height body does not overflow', () => {
    const outerWheel = vi.fn()
    const { container } = render(
      <div onWheel={outerWheel}>
        <PlanQuestionComposer
          threadId="thread-a"
          interaction={interaction()}
          onChange={vi.fn()}
          onSubmit={vi.fn()}
        />
      </div>,
    )
    const body = container.querySelector<HTMLElement>('.plan-question-composer-body')!
    expect(container.querySelector('.ui-overlay-scrollbar')).toHaveAttribute('data-visibility', 'transient')
    Object.defineProperties(body, {
      clientHeight: { configurable: true, value: 220 },
      scrollHeight: { configurable: true, value: 220 },
    })

    fireEvent.wheel(body, { deltaY: 48 })

    expect(outerWheel).not.toHaveBeenCalled()
  })

  it('submits all-optional batches and returns to the first missing required question', () => {
    let current = { ...interaction(), activeQuestionIndex: 2 }
    const submit = vi.fn()
    const change = (updater: (value: PlanQuestionState) => PlanQuestionState) => {
      current = updater(current)
    }
    const view = render(
      <PlanQuestionComposer threadId="thread-a" interaction={current} onChange={change} onSubmit={submit} />,
    )
    expect(screen.getByRole('button', { name: '提交' }))
      .toHaveClass('ui-button--sm', 'ui-button--capsule', 'ui-button--primary')
    fireEvent.click(screen.getByRole('button', { name: '提交' }))
    expect(submit).not.toHaveBeenCalled()
    expect(current.activeQuestionIndex).toBe(0)
    expect(current.error).toBe('请回答所有必填的 Plan 澄清问题')

    current = {
      ...interaction(),
      activeQuestionIndex: 2,
      questions: interaction().questions.map((question) => ({ ...question, required: false })),
    }
    view.rerender(
      <PlanQuestionComposer threadId="thread-a" interaction={current} onChange={change} onSubmit={submit} />,
    )
    fireEvent.click(screen.getByRole('button', { name: '提交' }))
    expect(submit).toHaveBeenCalledOnce()
  })

  it('keeps multiple selections and custom text together without auto-advancing', () => {
    let current: PlanQuestionState = {
      ...interaction(),
      questions: [{
        id: 'browsers',
        answerType: 'multiple_choice',
        prompt: '需要覆盖哪些浏览器？',
        required: true,
        options: [
          { id: 'chrome', label: 'Chrome', recommended: true },
          { id: 'safari', label: 'Safari', recommended: true },
        ],
        allowFreeText: true,
        minSelections: 2,
        maxSelections: 3,
        selectedOptionIds: [],
      }],
      activeQuestionIndex: 0,
    }
    const change = (updater: (value: PlanQuestionState) => PlanQuestionState) => {
      current = updater(current)
    }
    const view = render(
      <PlanQuestionComposer threadId="thread-a" interaction={current} onChange={change} onSubmit={vi.fn()} />,
    )

    expect(screen.queryByText('推荐', { exact: true })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('checkbox', { name: /Chrome/ }))
    view.rerender(
      <PlanQuestionComposer threadId="thread-a" interaction={current} onChange={change} onSubmit={vi.fn()} />,
    )
    fireEvent.change(screen.getByRole('textbox', { name: /自定义回答/ }), {
      target: { value: 'Firefox' },
    })

    expect(current.activeQuestionIndex).toBe(0)
    expect(current.questions[0]).toMatchObject({
      selectedOptionIds: ['chrome'],
      customAnswer: 'Firefox',
      skipped: false,
    })
  })

  it('restores an answered multiple-choice question at the first Tab stop', async () => {
    const user = userEvent.setup()
    const current: PlanQuestionState = {
      ...interaction(),
      questions: [{
        id: 'browsers-tab-order',
        answerType: 'multiple_choice',
        prompt: '需要覆盖哪些浏览器？',
        required: true,
        options: ['Chrome', 'Safari', 'Firefox', 'Edge'].map((label, index) => ({
          id: `browser-${index + 1}`,
          label,
          recommended: index === 0,
        })),
        allowFreeText: true,
        minSelections: 1,
        maxSelections: 4,
        selectedOptionIds: ['browser-4'],
      }],
      activeQuestionIndex: 0,
    }

    render(
      <PlanQuestionComposer
        threadId="thread-a"
        interaction={current}
        onChange={vi.fn()}
        onSubmit={vi.fn()}
      />,
    )

    const options = screen.getAllByRole('checkbox')
    await waitFor(() => expect(options[0]).toHaveFocus())
    await user.tab()
    expect(options[1]).toHaveFocus()
    await user.tab({ shift: true })
    expect(options[0]).toHaveFocus()
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
      <PlanQuestionComposer threadId="thread-a" interaction={current} onChange={change} onSubmit={submit} />,
    )

    expect(screen.getByText('选择 1 至 2 项，自定义回答计作一项')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('checkbox', { name: /Web/ }))
    view.rerender(
      <PlanQuestionComposer threadId="thread-a" interaction={current} onChange={change} onSubmit={submit} />,
    )
    fireEvent.click(screen.getByRole('checkbox', { name: /移动端/ }))
    view.rerender(
      <PlanQuestionComposer threadId="thread-a" interaction={current} onChange={change} onSubmit={submit} />,
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
      <PlanQuestionComposer threadId="thread-a" interaction={current} onChange={change} onSubmit={submit} />,
    )
    fireEvent.click(screen.getByRole('button', { name: '提交' }))
    expect(submit).not.toHaveBeenCalled()
    expect(current.error).toBe('Plan 澄清答案超出允许的选择数量')
  })

  it('uses the shared controlled date picker', async () => {
    const user = userEvent.setup()
    const initial = interaction()
    const dateQuestion = initial.questions[1]
    if (!dateQuestion || dateQuestion.answerType !== 'date') throw new Error('测试夹具第二题必须是日期题')
    let current = {
      ...initial,
      activeQuestionIndex: 1,
      questions: initial.questions.map((question, index) => index === 1
        ? { ...dateQuestion, date: '2026-09-14' }
        : question),
    }
    const change = (updater: (value: PlanQuestionState) => PlanQuestionState) => {
      current = updater(current)
    }
    render(
      <PlanQuestionComposer threadId="thread-a" interaction={current} onChange={change} onSubmit={vi.fn()} />,
    )

    const trigger = screen.getByRole('button', { name: '日期回答：交付时间有什么偏好？' })
    expect(trigger).toHaveAttribute('aria-haspopup', 'grid')
    await user.click(trigger)
    const target = await waitFor(() => {
      const element = document.querySelector<HTMLElement>('[data-date-value="2026-09-15"]')
      expect(element).not.toBeNull()
      return element!
    })
    await user.click(target)
    expect(current.questions[1]).toMatchObject({
      answerType: 'date',
      date: '2026-09-15',
      skipped: false,
    })
  })

  it('uses the shared bounded time picker without exposing the configured time zone', async () => {
    const user = userEvent.setup()
    let current: PlanQuestionState = {
      ...interaction(),
      activeQuestionIndex: 0,
      questions: [{
        id: 'deployment-time',
        answerType: 'time',
        prompt: '何时执行？',
        required: true,
        timeZone: 'Asia/Shanghai',
        minimum: '09:00',
        maximum: '18:00',
      }],
    }
    const submit = vi.fn()
    const change = (updater: (value: PlanQuestionState) => PlanQuestionState) => {
      current = updater(current)
    }
    const view = render(
      <PlanQuestionComposer threadId="thread-a" interaction={current} onChange={change} onSubmit={submit} />,
    )

    expect(screen.queryByText('时区：Asia/Shanghai')).not.toBeInTheDocument()
    expect(screen.getByText('允许范围：09:00–18:00')).toBeInTheDocument()
    const trigger = screen.getByRole('button', { name: '时间回答：何时执行？' })
    expect(trigger).toBeEnabled()
    expect(document.querySelector('input[type="time"]')).not.toBeInTheDocument()
    expect(document.querySelector('select')).not.toBeInTheDocument()

    await user.click(trigger)
    const hours = screen.getByRole('listbox', { name: '小时' })
    const minutes = screen.getByRole('listbox', { name: '分钟' })
    expect(within(hours).getByRole('option', { name: '08' })).toBeDisabled()
    expect(within(hours).getByRole('option', { name: '09' })).toBeEnabled()
    expect(within(hours).getByRole('option', { name: '19' })).toBeDisabled()
    await user.click(within(hours).getByRole('option', { name: '09' }))
    await user.click(within(minutes).getByRole('option', { name: '30' }))
    expect(current.questions[0]).toMatchObject({
      answerType: 'time',
      time: '09:30',
      skipped: false,
    })

    current = {
      ...current,
      questions: current.questions.map((question) => question.answerType === 'time'
        ? { ...question, time: '08:59' }
        : question),
    }
    view.rerender(
      <PlanQuestionComposer threadId="thread-a" interaction={current} onChange={change} onSubmit={submit} />,
    )
    fireEvent.click(screen.getByRole('button', { name: '提交' }))
    expect(submit).not.toHaveBeenCalled()
    expect(current.error).toBe('Plan 时间答案超出允许范围')
  })

  it('composes bounded date and time selectors without exposing the configured time zone', async () => {
    // 只固定日历日期，保留交互与焦点恢复使用的真实计时器
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date('2026-08-28T00:00:00Z'))
    const user = userEvent.setup()
    let current: PlanQuestionState = {
      ...interaction(),
      activeQuestionIndex: 0,
      questions: [{
        id: 'deployment-at',
        answerType: 'datetime',
        prompt: '何时执行？',
        required: true,
        timeZone: 'Asia/Shanghai',
        minimum: '2026-08-30T09:00',
        maximum: '2026-09-30T18:00',
      }],
    }
    const submit = vi.fn()
    const change = (updater: (value: PlanQuestionState) => PlanQuestionState) => {
      current = updater(current)
    }
    const view = render(
      <PlanQuestionComposer threadId="thread-a" interaction={current} onChange={change} onSubmit={submit} />,
    )

    expect(screen.queryByText('时区：Asia/Shanghai')).not.toBeInTheDocument()
    expect(screen.queryByText('允许范围：2026-08-30T09:00–2026-09-30T18:00')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '时间回答：何时执行？' })).toBeDisabled()
    expect(document.querySelector('input[type="datetime-local"]')).not.toBeInTheDocument()
    expect(document.querySelector('select')).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '日期回答：何时执行？' }))
    const date = await waitFor(() => {
      const element = document.querySelector<HTMLElement>('[data-date-value="2026-08-30"]')
      expect(element).not.toBeNull()
      return element!
    })
    await user.click(date)
    expect(current.questions[0]).toMatchObject({ dateTime: '2026-08-30T09:00' })
    view.rerender(
      <PlanQuestionComposer threadId="thread-a" interaction={current} onChange={change} onSubmit={submit} />,
    )
    await user.click(screen.getByRole('button', { name: '时间回答：何时执行？' }))
    await user.click(within(screen.getByRole('listbox', { name: '小时' }))
      .getByRole('option', { name: '09' }))
    await user.click(within(screen.getByRole('listbox', { name: '分钟' }))
      .getByRole('option', { name: '30' }))
    expect(current.questions[0]).toMatchObject({
      answerType: 'datetime',
      dateTime: '2026-08-30T09:30',
      skipped: false,
    })

    current = {
      ...current,
      questions: current.questions.map((question) => question.answerType === 'datetime'
        ? { ...question, dateTime: '2026-08-30T08:59' }
        : question),
    }
    view.rerender(
      <PlanQuestionComposer threadId="thread-a" interaction={current} onChange={change} onSubmit={submit} />,
    )
    fireEvent.click(screen.getByRole('button', { name: '提交' }))
    expect(submit).not.toHaveBeenCalled()
    expect(current.error).toBe('Plan 日期时间答案超出允许范围')
  })

  it('keeps answer controls accessible in touch, forced-colors and reduced-motion modes', () => {
    expect(conversationStyles).toMatch(/@media \(forced-colors: active\)[\s\S]*\.plan-question-composer-footer::before,\s*\.plan-review-composer-footer::before\s*\{[^}]*background:\s*Canvas;/s)
    expect(conversationStyles).toMatch(/@media \(any-hover: none\), \(any-pointer: coarse\)[\s\S]*\.plan-question-composer\.is-minimized \.plan-question-progress,[\s\S]*\.plan-question-composer\.is-minimized \.plan-question-progress-step\s*\{[^}]*height:\s*var\(--control-lg\);/s)
    expect(conversationStyles).toMatch(/@media \(any-hover: none\), \(any-pointer: coarse\)[\s\S]*\.plan-question-date\s*\{[^}]*height:\s*var\(--control-lg\);/s)
    expect(conversationStyles).toMatch(/@media \(forced-colors: active\)[\s\S]*\.plan-question-option:focus-visible,[\s\S]*outline:\s*2px solid Highlight;/s)
    expect(conversationStyles).toMatch(/@media \(any-hover: none\), \(any-pointer: coarse\)[\s\S]*\.plan-question-pager-button\s*{[^}]*min-width:\s*var\(--control-lg\);[^}]*min-height:\s*var\(--control-lg\);/s)
    expect(conversationStyles).toMatch(/@media \(prefers-reduced-motion: reduce\)[\s\S]*\.plan-question-pager-button \.ui-icon-button__icon\s*{\s*transition:\s*none;/s)
  })

  it('grows the custom answer from one line to three lines before scrolling', () => {
    const { container } = render(
      <PlanQuestionComposer
        threadId="thread-a"
        interaction={interaction()}
        onChange={vi.fn()}
        onSubmit={vi.fn()}
      />,
    )
    const textarea = container.querySelector<HTMLTextAreaElement>('.plan-question-custom textarea')!
    textarea.style.maxHeight = '72px'
    Object.defineProperty(textarea, 'scrollHeight', { configurable: true, value: 48 })

    fireEvent.change(textarea, { target: { value: '第一行\n第二行' } })
    expect(textarea.style.height).toBe('48px')
    expect(textarea.style.overflowY).toBe('hidden')

    Object.defineProperty(textarea, 'scrollHeight', { configurable: true, value: 96 })
    fireEvent.change(textarea, { target: { value: '第一行\n第二行\n第三行\n第四行' } })
    expect(textarea.style.height).toBe('72px')
    expect(textarea.style.overflowY).toBe('auto')
  })

  it('does not auto-focus untouched answer controls on entry', async () => {
    const firstView = render(
      <PlanQuestionComposer
        threadId="thread-a"
        interaction={interaction()}
        onChange={vi.fn()}
        onSubmit={vi.fn()}
      />,
    )
    const firstOption = screen.getByRole('radio', { name: /预发布/ })
    await new Promise<void>((resolve) => window.requestAnimationFrame(() => resolve()))
    expect(firstOption).not.toHaveFocus()
    firstView.unmount()

    render(
      <PlanQuestionComposer
        threadId="thread-a"
        interaction={{ ...interaction(), activeQuestionIndex: 2 }}
        onChange={vi.fn()}
        onSubmit={vi.fn()}
      />,
    )

    const answer = screen.getByRole('textbox', { name: '自定义回答：还有其他补充吗？' })
    await new Promise<void>((resolve) => window.requestAnimationFrame(() => resolve()))
    expect(answer).not.toHaveFocus()
    expect(screen.queryByRole('radiogroup')).not.toBeInTheDocument()
  })

  it('restores focus to an existing free-text draft', async () => {
    const answered = interaction()
    answered.activeQuestionIndex = 2
    const question = answered.questions[2]
    if (!question || question.answerType !== 'text') throw new Error('测试夹具第三题必须是文本题')
    answered.questions[2] = { ...question, answer: '已有补充' }

    render(
      <PlanQuestionComposer
        threadId="thread-a"
        interaction={answered}
        onChange={vi.fn()}
        onSubmit={vi.fn()}
      />,
    )

    await waitFor(() => expect(
      screen.getByRole('textbox', { name: '自定义回答：还有其他补充吗？' }),
    ).toHaveFocus())
  })
})
