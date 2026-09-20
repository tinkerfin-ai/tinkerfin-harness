import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { PlanReviewState } from '../../../types'
import { PlanReviewCard, PlanReviewStatusRow } from './PlanReviewCard'

describe('PlanReviewCard', () => {
  const interaction = (): PlanReviewState => ({
    kind: 'review',
    interruptId: 'plan-review-1',
    revision: 3,
    allowedActions: ['approve', 'reject', 'cancel'],
    submitted: false,
    draft: {
      revision: 3,
      contentSchema: {
        fingerprint: '0'.repeat(64),
        mediaType: 'text/markdown',
      },
      content: {
        description: '切换实现模式并保持父图稳定',
        markdown: '# 实现模式切换\n\n- 保持父图稳定',
      },
    },
  })

  beforeEach(() => {
    window.sessionStorage.clear()
  })

  it('shows approve, reject, and the independent close action', async () => {
    const user = userEvent.setup()
    const submit = vi.fn()
    const cancel = vi.fn()

    render(
      <PlanReviewCard
        interaction={interaction()}
        onChange={vi.fn()}
        onSubmit={submit}
        onClose={cancel}
      />,
    )

    expect(screen.getByRole('heading', { level: 1, name: '实现模式切换' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 2 }))
      .toHaveTextContent('切换实现模式并保持父图稳定')
    const card = screen.getByRole('region', { name: 'Plan 审阅' })
    expect(card.querySelector('.plan-review-composer-heading p')).not.toBeInTheDocument()
    expect(card.querySelector('.plan-interaction-card-title'))
      .toHaveTextContent('切换实现模式并保持父图稳定')
    expect(card.querySelector('.interaction-card-color-bridge.is-warning')).toBeInTheDocument()
    expect(screen.getByText('保持父图稳定')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '编辑' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '反馈' })).not.toBeInTheDocument()

    const footer = card.querySelector('.plan-review-composer-footer')
    expect(footer).not.toBeNull()
    expect(footer?.parentElement).toHaveClass('plan-review-composer-body')
    const actions = within(footer as HTMLElement)
    const reject = actions.getByRole('button', { name: '拒绝' })
    const approve = actions.getByRole('button', { name: '批准' })
    expect(reject).toHaveClass('approval-reject-button')
    expect(approve).toHaveClass('ui-button--solid')

    await user.click(approve)
    expect(submit).toHaveBeenCalledExactlyOnceWith('approve')
    await user.click(screen.getByRole('button', { name: '关闭卡片，继续对话' }))
    expect(cancel).toHaveBeenCalledOnce()
  })

  it('keeps the rejection reason optional and restores focus when editing is cancelled', async () => {
    const user = userEvent.setup()
    const submit = vi.fn()

    function Harness() {
      const [current, setCurrent] = useState(interaction())
      return (
        <PlanReviewCard
          interaction={current}
          onChange={setCurrent}
          onSubmit={submit}
          onClose={vi.fn()}
        />
      )
    }

    render(<Harness />)
    const reject = screen.getByRole('button', { name: '拒绝' })
    await user.click(reject)
    const reason = screen.getByRole('textbox', { name: '拒绝原因（可选）' })
    await waitFor(() => expect(reason).toHaveFocus())
    expect(reason).not.toBeRequired()

    await user.click(screen.getByRole('button', { name: '确认拒绝' }))
    expect(submit).toHaveBeenCalledExactlyOnceWith('reject')

    await user.type(reason, '范围不合适')
    await user.click(screen.getByRole('button', { name: '取消' }))
    await waitFor(() => expect(reject).toHaveFocus())
    expect(screen.queryByRole('textbox', { name: '拒绝原因（可选）' })).not.toBeInTheDocument()
  })

  it('shows only actions declared by the authoritative response schema', () => {
    render(
      <PlanReviewCard
        interaction={{ ...interaction(), allowedActions: ['reject'] }}
        onChange={vi.fn()}
        onSubmit={vi.fn()}
        onClose={vi.fn()}
      />,
    )

    expect(screen.getByRole('button', { name: '拒绝' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '批准' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '关闭卡片，继续对话' })).toBeInTheDocument()
  })

  it('stays expanded while retaining the review header cancel affordance', () => {
    window.sessionStorage.setItem('tinkerfin:plan-review-collapse:thread-a', 'collapsed')
    render(
      <PlanReviewCard
        interaction={interaction()}
        onChange={vi.fn()}
        onSubmit={vi.fn()}
        onClose={vi.fn()}
      />,
    )

    expect(screen.getByRole('separator', { name: '调整交互卡片高度' })).toBeInTheDocument()
    expect(screen.getByRole('region', { name: '计划草稿内容' })).toBeInTheDocument()
    const card = screen.getByRole('region', { name: 'Plan 审阅' })
    expect(card).not.toHaveClass('is-minimized')
    expect(card.querySelector('.plan-review-toggle-surface')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '关闭卡片，继续对话' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /展开计划草稿|收起计划草稿/ })).not.toBeInTheDocument()
  })

  it('shows waiting and submitted conversation statuses', () => {
    const view = render(<PlanReviewStatusRow interaction={interaction()} />)
    expect(screen.getByText('Plan')).toBeInTheDocument()
    expect(screen.getByText('等待审阅')).toBeInTheDocument()
    expect(view.container.querySelector('.activity-dots')).toBeInTheDocument()

    view.rerender(<PlanReviewStatusRow interaction={{ ...interaction(), submitted: true }} />)
    expect(screen.getByRole('status')).toHaveTextContent('正在处理决定')
    expect(view.container.querySelector('.activity-dots')).not.toBeInTheDocument()
  })

  it('announces a dynamic submission error', () => {
    render(
      <PlanReviewCard
        interaction={{ ...interaction(), error: '计划版本已经更新' }}
        onChange={vi.fn()}
        onSubmit={vi.fn()}
        onClose={vi.fn()}
      />,
    )

    expect(screen.getByRole('alert')).toHaveTextContent('计划版本已经更新')
  })

})
