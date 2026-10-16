import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'

import uiStyles from './ui.css?raw'
import { DatePicker } from './DatePicker'
import { LocaleProvider } from '../../i18n'

describe('DatePicker', () => {
  it.each(['zh-CN', 'en'] as const)('在 %s 中提供完整日月年网格与范围状态', async locale => {
    localStorage.setItem('tinkerfin:language', locale)
    const user = userEvent.setup()
    const onChange = vi.fn()
    const labels = locale === 'zh-CN'
      ? ['选择日期', '日期选择器', '月历', '选择月份和年份', '月份网格', '选择年份', '年份网格', '不可选日期：', '已选日期：']
      : ['Choose a date', 'Date picker', 'Calendar month', 'Choose month and year', 'Calendar year', 'Choose year', 'Calendar decade', 'Unavailable date:', 'Selected date:']
    render(<LocaleProvider><DatePicker value="2026-09-14" label="发布日期" min="2026-09-10" max="2026-09-20" onChange={onChange} /></LocaleProvider>)
    await user.click(screen.getByRole('button', { name: '发布日期' }))
    const calendar = await screen.findByRole('application', { name: labels[0] })
    expect(calendar).toHaveAttribute('aria-roledescription', labels[1])
    const verifyRows = (description: string, columns: number) => {
      const grid = within(calendar).getByRole('grid')
      expect(grid).toHaveAttribute('aria-roledescription', description)
      const rows = within(grid).getAllByRole('row')
      for (const row of rows) {
        const cells = within(row).getAllByRole('gridcell')
        expect(cells.length).toBeGreaterThan(0)
        expect(cells.length).toBeLessThanOrEqual(columns)
      }
      expect(rows.flatMap(row => within(row).getAllByRole('gridcell'))).toHaveLength(within(grid).getAllByRole('gridcell').length)
    }
    verifyRows(labels[2], 7)
    const unavailable = within(calendar).getAllByRole('button', { name: new RegExp(`^${labels[7]}`) })[0]
    expect(unavailable).toHaveAttribute('aria-disabled', 'true')
    expect(within(calendar).getByRole('button', { name: new RegExp(`^${labels[8]}`) })).not.toHaveAttribute('aria-disabled')
    await user.click(unavailable)
    expect(onChange).not.toHaveBeenCalled()
    await user.click(within(calendar).getByRole('button', { name: labels[3] }))
    verifyRows(labels[4], 3)
    await user.click(within(calendar).getByRole('button', { name: labels[5] }))
    verifyRows(labels[6], 3)
  })

  it('禁用已打开的日期选择器时关闭月历，恢复可用后不自动重开', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    const view = render(<DatePicker value="2026-09-14" label="发布日期" onChange={onChange} />)
    const trigger = screen.getByRole('button', { name: '发布日期' })
    await user.click(trigger)
    expect(await screen.findByRole('application', { name: '选择日期' })).toBeInTheDocument()

    await act(async () => {
      view.rerender(<DatePicker value="2026-09-14" label="发布日期" onChange={onChange} disabled />)
    })
    expect(trigger).toBeDisabled()
    expect(screen.queryByRole('application', { name: '选择日期' })).not.toBeInTheDocument()
    expect(onChange).not.toHaveBeenCalled()

    await act(async () => {
      view.rerender(<DatePicker value="2026-09-14" label="发布日期" onChange={onChange} />)
    })
    expect(trigger).toBeEnabled()
    expect(screen.queryByRole('application', { name: '选择日期' })).not.toBeInTheDocument()
  })

  it('使用受控日期值并在选择后关闭月历、恢复触发器焦点', async () => {
    const user = userEvent.setup()
    function Harness() {
      const [value, setValue] = useState('2026-09-14')
      return <DatePicker value={value} label="发布日期" onChange={setValue} controlSize="xs" />
    }
    render(<Harness />)

    const trigger = screen.getByRole('button', { name: '发布日期' })
    await user.click(trigger)
    await screen.findByRole('application', { name: '选择日期' })
    const target = await waitFor(() => {
      const element = document.querySelector<HTMLElement>('[data-date-value="2026-09-15"]')
      expect(element).not.toBeNull()
      return element!
    })
    await user.click(target)
    await waitFor(() => expect(screen.queryByRole('application', { name: '选择日期' })).not.toBeInTheDocument())
    expect(trigger).toHaveTextContent('2026/09/15')
    await waitFor(() => expect(trigger).toHaveFocus())
  })

  it('支持方向键选择和 Escape 关闭', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(<DatePicker value="2026-09-14" label="发布日期" onChange={onChange} />)
    const trigger = screen.getByRole('button', { name: '发布日期' })
    await user.click(trigger)
    const active = await waitFor(() => {
      const element = document.querySelector<HTMLElement>('[data-date-value="2026-09-14"]')
      expect(element).not.toBeNull()
      return element!
    })
    await waitFor(() => expect(active).toHaveFocus())
    await user.keyboard('{ArrowRight}')
    const next = document.querySelector<HTMLElement>('[data-date-value="2026-09-15"]')!
    await waitFor(() => expect(next).toHaveFocus())
    await user.keyboard('{Enter}')
    await waitFor(() => expect(onChange).toHaveBeenCalledWith('2026-09-15'))

    await user.click(trigger)
    await user.keyboard('{Escape}')
    await waitFor(() => expect(trigger).toHaveFocus())
  })

  it('通过框架内置视图直接选择年份和月份', async () => {
    const user = userEvent.setup()
    render(<DatePicker value="2026-09-14" label="发布日期" onChange={vi.fn()} />)
    await user.click(screen.getByRole('button', { name: '发布日期' }))
    await user.click(await screen.findByRole('button', { name: '选择月份和年份' }))
    expect(document.querySelectorAll('[data-month-value]')).toHaveLength(12)
    await user.click(screen.getByRole('button', { name: '选择年份' }))
    await user.click(document.querySelector<HTMLElement>('[data-year-value="2027"]')!)
    await user.click(document.querySelector<HTMLElement>('[data-month-value="7"]')!)
    expect(screen.getByRole('button', { name: '选择月份和年份' })).toHaveTextContent('2027年7月')
  })

  it('遵循全局控件、触控、高对比和 reduced-motion 契约', () => {
    expect(uiStyles).toMatch(/\.ui-temporal-picker__trigger--xs\s*\{[^}]*height:\s*var\(--control-xs\);[^}]*min-height:\s*var\(--control-xs\);/s)
    expect(uiStyles).toMatch(/\.ui-temporal-picker__trigger:hover:not\(:disabled\)\s*\{[^}]*cursor:\s*pointer;/s)
    expect(uiStyles).toMatch(/@media \(any-hover: none\), \(any-pointer: coarse\)[\s\S]*\.ui-temporal-picker__trigger,\s*\.ui-date-picker__nav,\s*\.ui-date-picker__day,\s*\.ui-date-picker__period,[\s\S]*min-height:\s*var\(--control-lg\);/s)
    expect(uiStyles).toMatch(/@media \(forced-colors: active\)[\s\S]*\.ui-temporal-picker__trigger:focus-visible,[\s\S]*outline:\s*2px solid Highlight;/s)
    expect(uiStyles).toMatch(/@media \(prefers-reduced-motion: reduce\)[\s\S]*\.ui-temporal-picker__popover,[\s\S]*transition:\s*none;/s)
  })
})
