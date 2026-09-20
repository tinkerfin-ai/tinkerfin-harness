import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { AccessModePicker } from './AccessModePicker'

describe('文件审批选择', () => {
  it('显示两个权限并支持键盘选择和焦点恢复', () => {
    const change = vi.fn()
    render(<AccessModePicker value="write_approval" onChange={change} />)
    const trigger = screen.getByRole('button', { name: '选择访问权限' })
    expect(trigger).toHaveAccessibleDescription('写入需审批')
    expect(screen.getByRole('tooltip')).toHaveTextContent('写入需审批')
    expect(trigger.querySelector('.ui-compact-picker-chevron')).not.toBeNull()
    fireEvent.click(trigger)
    const menu = screen.getByRole('listbox', { name: '访问权限选项' })
    expect(trigger.parentElement).toHaveClass('ui-compact-picker--access-mode')
    expect(menu.parentElement).toBe(trigger.parentElement)
    expect(screen.getAllByRole('option')).toHaveLength(2)
    fireEvent.keyDown(menu, { key: 'ArrowDown' })
    fireEvent.keyDown(menu, { key: 'Enter' })
    expect(change).toHaveBeenCalledWith('full')
    expect(trigger).toHaveFocus()
  })
  it('运行或审批期间不能改变权限', () => {
    render(<AccessModePicker value="write_approval" onChange={vi.fn()} disabled />)
    expect(screen.getByRole('button', { name: '选择访问权限' })).toBeDisabled()
  })
})
