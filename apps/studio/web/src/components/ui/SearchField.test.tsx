import { createRef, useState } from 'react'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, it, vi } from 'vitest'

import { SearchField } from './SearchField'

it('保留输入、外部焦点与 Escape 关闭语义', async () => {
  const close = vi.fn()
  const ref = createRef<HTMLInputElement>()
  function Example() {
    const [value, setValue] = useState('')
    return <SearchField ref={ref} value={value} onChange={setValue} onClose={close}
      label="搜索记录" closeLabel="关闭搜索" placeholder="输入关键词" />
  }
  render(<Example />)
  const user = userEvent.setup()
  const input = screen.getByRole('searchbox', { name: '搜索记录' })
  expect(ref.current).toBe(input)
  await user.type(input, '新闻')
  expect(input).toHaveValue('新闻')
  await user.keyboard('{Escape}')
  expect(close).toHaveBeenCalledOnce()
  await user.click(screen.getByRole('button', { name: '关闭搜索' }))
  expect(close).toHaveBeenCalledTimes(2)
})
