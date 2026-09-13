import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'

import uiStyles from './ui.css?raw'
import { TimePicker } from './TimePicker'
import { Dialog } from './Dialog'

describe('TimePicker', () => {
  it('通过自定义小时和分钟浮层写入时间并恢复触发器焦点', async () => {
    const user = userEvent.setup()
    function Harness() {
      const [value, setValue] = useState('')
      return <TimePicker value={value} label="上线时刻" onChange={setValue} controlSize="xs" />
    }
    const { container } = render(<Harness />)

    const trigger = screen.getByRole('button', { name: '上线时刻' })
    expect(trigger).toHaveTextContent('--:--')
    expect(container.querySelector('select')).not.toBeInTheDocument()
    await user.click(trigger)
    expect(await screen.findByRole('dialog', { name: '选择时间' })).toBeInTheDocument()

    const hours = screen.getByRole('listbox', { name: '小时' })
    const minutes = screen.getByRole('listbox', { name: '分钟' })
    await user.click(within(hours).getByRole('option', { name: '09' }))
    await user.click(within(minutes).getByRole('option', { name: '30' }))

    expect(trigger).toHaveTextContent('09:30')
    expect(screen.queryByRole('dialog', { name: '选择时间' })).not.toBeInTheDocument()
    await waitFor(() => expect(trigger).toHaveFocus())
  })

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

  it('支持方向键选择、Enter 确认和 Escape 关闭', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(<TimePicker value="00:00" label="上线时刻" onChange={onChange} />)
    const trigger = screen.getByRole('button', { name: '上线时刻' })

    await user.click(trigger)
    const hour = within(screen.getByRole('listbox', { name: '小时' }))
      .getByRole('option', { name: '00', selected: true })
    await waitFor(() => expect(hour).toHaveFocus())
    await user.keyboard('{ArrowDown}')
    const nextHour = within(screen.getByRole('listbox', { name: '小时' }))
      .getByRole('option', { name: '01', selected: true })
    await waitFor(() => expect(nextHour).toHaveFocus())
    await user.keyboard('{Enter}')
    const minute = within(screen.getByRole('listbox', { name: '分钟' }))
      .getByRole('option', { name: '00', selected: true })
    await waitFor(() => expect(minute).toHaveFocus())
    await user.keyboard('{ArrowDown}')
    const nextMinute = within(screen.getByRole('listbox', { name: '分钟' }))
      .getByRole('option', { name: '01', selected: true })
    await waitFor(() => expect(nextMinute).toHaveFocus())
    await user.keyboard('{Enter}')
    expect(onChange).toHaveBeenCalledWith('01:01')
    await waitFor(() => expect(trigger).toHaveFocus())

    await user.click(trigger)
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('dialog', { name: '选择时间' })).not.toBeInTheDocument()
    await waitFor(() => expect(trigger).toHaveFocus())
  })

  it('弹窗内按 Escape 只关闭时间选项，并恢复时间触发器焦点', async () => {
    const user = userEvent.setup()
    const onClose = vi.fn()
    render(<Dialog open title="编辑日程" onClose={onClose}><TimePicker value="09:00" label="执行时间" onChange={vi.fn()} /></Dialog>)
    const trigger = screen.getByRole('button', { name: '执行时间' })
    await user.click(trigger)
    await waitFor(() => expect(within(screen.getByRole('listbox', { name: '小时' })).getByRole('option', { selected: true })).toHaveFocus())
    await user.keyboard('{Escape}')
    expect(onClose).not.toHaveBeenCalled()
    expect(screen.queryByRole('dialog', { name: '选择时间' })).not.toBeInTheDocument()
    await waitFor(() => expect(trigger).toHaveFocus())
  })

  it('与日期选择器共用触发器、浮层、触控和动效规格', () => {
    expect(uiStyles).toMatch(/\.ui-temporal-picker__trigger\s*\{[^}]*border-radius:\s*var\(--radius-md\);[^}]*font-size:\s*var\(--type-ui-size\);/s)
    expect(uiStyles).toMatch(/\.ui-temporal-picker__trigger--xs\s*\{[^}]*height:\s*var\(--control-xs\);[^}]*min-height:\s*var\(--control-xs\);/s)
    expect(uiStyles).toMatch(/\.ui-temporal-picker__popover\s*\{[^}]*box-sizing:\s*border-box;[^}]*border-radius:\s*var\(--radius-3xl\);[^}]*box-shadow:\s*var\(--shadow-3\);/s)
    expect(uiStyles).toMatch(/@media \(max-width: 440px\)[\s\S]*\.ui-temporal-picker__popover\s*\{[^}]*width:\s*calc\(100vw - \(2 \* var\(--space-3\)\)\);/s)
    expect(uiStyles).toMatch(/\.ui-time-picker__option\.is-selected,[\s\S]*background:\s*var\(--color-brand\);/s)
    expect(uiStyles).toMatch(/@media \(any-hover: none\), \(any-pointer: coarse\)[\s\S]*\.ui-temporal-picker__trigger,[\s\S]*\.ui-time-picker__option\s*\{[^}]*min-height:\s*var\(--control-lg\);/s)
    expect(uiStyles).toMatch(/@media \(prefers-reduced-motion: reduce\)[\s\S]*\.ui-temporal-picker__popover,[\s\S]*\.ui-time-picker__option,[\s\S]*animation:\s*none;/s)
  })

  it.each([false, true])('在弹窗归属=%s时，Tab按小时、分钟移动并退出到触发器，不提交草稿', async (inDialog) => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    const onClose = vi.fn()
    const picker = <TimePicker value="09:00" label="执行时间" onChange={onChange} />
    render(inDialog ? <Dialog open title="编辑日程" onClose={onClose}>{picker}<button type="button">后续操作</button></Dialog> : picker)
    const trigger = screen.getByRole('button', { name: '执行时间' })
    await user.click(trigger)
    const hour = within(screen.getByRole('listbox', { name: '小时' })).getByRole('option', { name: '09' })
    const minute = within(screen.getByRole('listbox', { name: '分钟' })).getByRole('option', { name: '00' })
    await waitFor(() => expect(hour).toHaveFocus())
    await user.tab()
    expect(minute).toHaveFocus()
    await user.tab({ shift: true })
    expect(hour).toHaveFocus()
    await user.tab()
    fireEvent.keyDown(minute, { key: 'Tab' })
    expect(trigger).toHaveFocus()
    expect(screen.queryByRole('dialog', { name: '选择时间' })).not.toBeInTheDocument()
    if (inDialog) {
      await user.tab()
      expect(screen.getByRole('button', { name: '后续操作' })).toHaveFocus()
    }
    await user.click(trigger)
    await waitFor(() => expect(within(screen.getByRole('listbox', { name: '小时' })).getByRole('option', { name: '09' })).toHaveFocus())
    await user.tab({ shift: true })
    await waitFor(() => expect(trigger).toHaveFocus())
    expect(onChange).not.toHaveBeenCalled()
    expect(onClose).not.toHaveBeenCalled()
  })
})
