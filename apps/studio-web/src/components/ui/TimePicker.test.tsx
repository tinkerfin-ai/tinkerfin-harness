import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { TimePicker } from './TimePicker'

describe('TimePicker', () => {

  it('在最小和最大时间之外禁用选项', async () => {
    const user = userEvent.setup()
    render(
      <TimePicker
        value="09:15"
        label="上线时刻"
        min="09:15"
        max="10:30"
        onChange={vi.fn()}
      />,
    )

    await user.click(screen.getByRole('button', { name: '上线时刻' }))
    const hours = screen.getByRole('listbox', { name: '小时' })
    const minutes = screen.getByRole('listbox', { name: '分钟' })
    expect(within(hours).getByRole('option', { name: '08' })).toBeDisabled()
    expect(within(hours).getByRole('option', { name: '09' })).toBeEnabled()
    expect(within(hours).getByRole('option', { name: '11' })).toBeDisabled()
    expect(within(minutes).getByRole('option', { name: '14' })).toBeDisabled()
    expect(within(minutes).getByRole('option', { name: '15' })).toBeEnabled()

    await user.click(within(hours).getByRole('option', { name: '10' }))
    expect(within(minutes).getByRole('option', { name: '30' })).toBeEnabled()
    expect(within(minutes).getByRole('option', { name: '31' })).toBeDisabled()
  })
})
