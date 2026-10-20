import { useState } from 'react'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, it } from 'vitest'

import { ExpandableSearch } from './ExpandableSearch'

function Example() {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  return <ExpandableSearch open={open} onOpenChange={setOpen} value={query} onChange={setQuery}
    label="搜索记录" placeholder="输入关键词" closeLabel="关闭搜索" />
}

it.each(['Escape', '关闭按钮'])('展开聚焦输入，%s关闭清空并恢复入口焦点', async method => {
  render(<Example />)
  const user = userEvent.setup()
  const trigger = screen.getByRole('button', { name: '搜索记录' })
  expect(trigger).toHaveAttribute('aria-expanded', 'false')
  expect(screen.queryByRole('searchbox')).not.toBeInTheDocument()
  await user.click(trigger)
  const input = screen.getByRole('searchbox', { name: '搜索记录' })
  expect(input).toHaveFocus()
  expect(trigger).toHaveAttribute('aria-expanded', 'true')
  expect(screen.queryByRole('button', { name: '搜索记录' })).not.toBeInTheDocument()
  await user.type(input, '报告')
  expect(input).toHaveValue('报告')
  if (method === 'Escape') await user.keyboard('{Escape}')
  else await user.click(screen.getByRole('button', { name: '关闭搜索' }))
  expect(trigger).toHaveFocus()
  expect(trigger).toHaveAttribute('aria-expanded', 'false')
  expect(screen.queryByRole('searchbox')).not.toBeInTheDocument()
  await user.click(trigger)
  expect(screen.getByRole('searchbox', { name: '搜索记录' })).toHaveValue('')
})
