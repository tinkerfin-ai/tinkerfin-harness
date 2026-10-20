import { fireEvent, render, screen } from '@testing-library/react'
import { Search } from 'lucide-react'
import { describe, expect, it } from 'vitest'

import { Button } from './Button'
import { IconButton } from './IconButton'

describe('Button', () => {

  it('distinguishes an explicit unselected toggle from a regular button', () => {
    render(<><Button selected={false}>未选筛选</Button><Button>普通操作</Button></>)

    expect(screen.getByRole('button', { name: '未选筛选' })).toHaveAttribute('aria-pressed', 'false')
    expect(screen.getByRole('button', { name: '普通操作' })).not.toHaveAttribute('aria-pressed')
  })
})

describe('IconButton', () => {
  it('保留调用方的补充说明，并在提供工具提示时合并可访问描述', () => {
    const { rerender } = render(<>
      <p id="action-reason">附件仍在上传</p>
      <IconButton label="发送消息" aria-describedby="action-reason" disabled icon={<Search size={18} />} />
    </>)
    expect(screen.getByRole('button', { name: '发送消息' })).toHaveAccessibleDescription('附件仍在上传')
    rerender(<>
      <p id="action-reason">附件仍在上传</p>
      <IconButton label="发送消息" aria-describedby="action-reason" tooltip="发送草稿" disabled icon={<Search size={18} />} />
    </>)
    fireEvent.pointerMove(screen.getByRole('button', { name: '发送消息' }))
    expect(screen.getByRole('button', { name: '发送消息' })).toHaveAccessibleDescription('附件仍在上传 发送草稿')
  })
})
