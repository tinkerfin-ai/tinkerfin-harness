import { render, screen } from '@testing-library/react'
import { expect, it } from 'vitest'

import { MarkdownContent } from './MarkdownContent'

it('文档可关闭远端图片加载，聊天默认仍可展示 Markdown 图片', () => {
  const content = '![营收趋势](https://example.com/chart.png)'
  const { rerender } = render(<MarkdownContent content={content} allowRemoteImages={false} />)
  expect(screen.queryByRole('img')).not.toBeInTheDocument()
  expect(screen.getByText('营收趋势')).toBeVisible()
  rerender(<MarkdownContent content={content} />)
  expect(screen.getByRole('img', { name: '营收趋势' })).toHaveAttribute('src', 'https://example.com/chart.png')
})
