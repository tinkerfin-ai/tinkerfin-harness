import { fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'

import { ListboxPicker } from './ListboxPicker'

function PortalPicker() {
  const [open, setOpen] = useState(false)
  const [value, setValue] = useState<'all' | 'model'>('all')
  return (
    <ListboxPicker
      value={value}
      options={['all', 'model']}
      open={open}
      onOpenChange={setOpen}
      onChange={setValue}
      triggerLabel="类型筛选"
      listboxLabel="类型"
      rootClassName="picker-root"
      triggerClassName="picker-trigger"
      listboxClassName="picker-listbox"
      optionClassName="picker-option"
      listboxPortalTarget={document.body}
      listboxStyle={{ position: 'fixed', top: 40, left: 12 }}
      renderTrigger={(option) => option}
      renderOption={(option, selected) => `${option}${selected ? ' selected' : ''}`}
    />
  )
}

describe('ListboxPicker', () => {
  it('keeps portal options inside the shared keyboard and focus contract', () => {
    render(<PortalPicker />)

    const trigger = screen.getByRole('button', { name: '类型筛选' })
    fireEvent.click(trigger)
    const listbox = screen.getByRole('listbox', { name: '类型' })
    expect(listbox.parentElement).toBe(document.body)
    expect(listbox).toHaveFocus()
    expect(listbox).toHaveStyle({ position: 'fixed', top: '40px', left: '12px' })
    vi.spyOn(listbox, 'getBoundingClientRect').mockReturnValue(new DOMRect(0, 0, 100, 40))
    vi.spyOn(screen.getByRole('option', { name: 'model' }), 'getBoundingClientRect').mockReturnValue(new DOMRect(0, 60, 100, 20))
    fireEvent.keyDown(listbox, { key: 'End' })
    expect(listbox.scrollTop).toBe(40)

    const model = screen.getByRole('option', { name: 'model' })
    fireEvent.pointerDown(model)
    fireEvent.click(model)
    expect(trigger).toHaveTextContent('model')
    expect(trigger).toHaveFocus()
    expect(screen.queryByRole('listbox', { name: '类型' })).not.toBeInTheDocument()

    fireEvent.click(trigger)
    fireEvent.keyDown(screen.getByRole('listbox', { name: '类型' }), { key: 'Home' })
    fireEvent.keyDown(screen.getByRole('listbox', { name: '类型' }), { key: 'Enter' })
    expect(trigger).toHaveTextContent('all')
    expect(trigger).toHaveFocus()
  })
})

function MultiplePicker({ disabled = false }: { disabled?: boolean }) {
  const [open, setOpen] = useState(false)
  const [value, setValue] = useState<string[]>([])
  return <ListboxPicker multiple disabled={disabled} value={value} options={['PNG', 'JPEG', 'WebP']} onChange={setValue} open={open} onOpenChange={setOpen} triggerLabel="输出格式" listboxLabel="输出格式" rootClassName="picker" triggerClassName="trigger" listboxClassName="options" renderTrigger={formats => formats.join(' / ') || '默认'} renderOption={format => format} />
}

it('多选逐项切换、保留焦点并在 Escape 后返回触发器', () => {
  render(<MultiplePicker />)
  const trigger = screen.getByRole('button', { name: '输出格式' })
  fireEvent.click(trigger)
  const list = screen.getByRole('listbox')
  expect(list).toHaveAttribute('aria-multiselectable', 'true')
  fireEvent.click(screen.getByRole('option', { name: 'PNG' }))
  fireEvent.keyDown(list, { key: 'ArrowDown' })
  fireEvent.keyDown(list, { key: ' ' })
  expect(screen.getAllByRole('option', { selected: true })).toHaveLength(2)
  expect(list).toHaveFocus()
  fireEvent.keyDown(list, { key: 'Escape' })
  expect(trigger).toHaveFocus()
  expect(trigger).toHaveTextContent('PNG / JPEG')
  fireEvent.click(trigger)
  fireEvent.click(screen.getByRole('option', { name: 'PNG' }))
  fireEvent.click(screen.getByRole('option', { name: 'JPEG' }))
  expect(screen.getAllByRole('option', { selected: false })).toHaveLength(3)
})

it.each(['鼠标', '键盘'])('允许用%s选回空字符串表示的服务默认值', input => {
  const change = vi.fn()
  const toggle = vi.fn()
  render(<ListboxPicker value="high" options={['', 'high']} onChange={change} open onOpenChange={toggle} triggerLabel="推理强度" listboxLabel="推理强度" rootClassName="picker" triggerClassName="trigger" listboxClassName="options" renderTrigger={value => value || '模型默认'} renderOption={value => value || '模型默认'} />)
  if (input === '鼠标') fireEvent.click(screen.getByRole('option', { name: '模型默认' }))
  else {
    fireEvent.keyDown(screen.getByRole('listbox'), { key: 'Home' })
    fireEvent.keyDown(screen.getByRole('listbox'), { key: 'Enter' })
  }
  expect(change).toHaveBeenCalledWith('')
  expect(toggle).toHaveBeenCalledWith(false)
})

it.each(['内联多选', '浮层单选'])('%s通过 Tab 关闭并将后续导航交给浏览器', mode => {
  render(mode === '内联多选' ? <MultiplePicker /> : <PortalPicker />)
  const trigger = screen.getByRole('button', { name: mode === '内联多选' ? '输出格式' : '类型筛选' })
  for (const shiftKey of [false, true]) {
    fireEvent.click(trigger)
    expect(fireEvent.keyDown(screen.getByRole('listbox'), { key: 'Tab', shiftKey })).toBe(true)
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
    expect(trigger).toHaveFocus()
  }
})

it('多选在禁用时关闭并在重新启用后保留选择', async () => {
  const user = userEvent.setup()
  const view = render(<MultiplePicker />)
  const trigger = screen.getByRole('button', { name: '输出格式' })
  await user.click(trigger)
  await user.click(screen.getByRole('option', { name: 'PNG' }))
  view.rerender(<MultiplePicker disabled />)
  expect(trigger).toBeDisabled()
  expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
  await user.click(trigger)
  expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
  view.rerender(<MultiplePicker />)
  await user.click(trigger)
  expect(screen.getByRole('option', { name: 'PNG' })).toHaveAttribute('aria-selected', 'true')
})
