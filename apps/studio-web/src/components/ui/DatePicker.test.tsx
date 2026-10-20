import { act, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

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
})
