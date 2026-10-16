import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { ViewTabs } from './ViewTabs'

describe('ViewTabs', () => {
  it('supports automatic keyboard activation and skips disabled views', () => {
    const onChange = vi.fn()
    render(
      <ViewTabs
        value="conversation"
        label="会话视图"
        options={[
          { value: 'conversation', label: '对话', controls: 'conversation-panel' },
          { value: 'unavailable', label: '不可用', disabled: true },
          { value: 'trace', label: '链路', controls: 'trace-panel' },
        ]}
        onChange={onChange}
      />,
    )

    const conversation = screen.getByRole('tab', { name: '对话' })
    const trace = screen.getByRole('tab', { name: '链路' })
    fireEvent.keyDown(conversation, { key: 'ArrowRight' })
    expect(onChange).toHaveBeenLastCalledWith('trace')
    expect(trace).toHaveFocus()
    fireEvent.keyDown(trace, { key: 'Home' })
    expect(onChange).toHaveBeenLastCalledWith('conversation')
    expect(conversation).toHaveFocus()
  })

  it('exposes compact density without changing tab semantics', () => {
    render(
      <ViewTabs
        value="timeline"
        label="链路布局"
        density="compact"
        options={[
          { value: 'timeline', label: '时间线' },
          { value: 'tree', label: '树形' },
        ]}
        onChange={vi.fn()}
      />,
    )

    expect(screen.getByRole('tab', { name: '时间线' })).toHaveAttribute('aria-selected', 'true')
  })

  it('provides a medium density for content detail tabs', () => {
    render(
      <ViewTabs
        value="overview"
        label="详情分类"
        density="medium"
        options={[
          { value: 'overview', label: '概述', controls: 'details' },
          { value: 'result', label: '结果', controls: 'details' },
        ]}
        onChange={vi.fn()}
      />,
    )

    expect(screen.getByRole('tab', { name: '概述' })).toHaveAttribute(
      'aria-controls',
      'details',
    )
  })
})
