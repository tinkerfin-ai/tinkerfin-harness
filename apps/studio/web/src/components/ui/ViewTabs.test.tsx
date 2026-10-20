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
})
