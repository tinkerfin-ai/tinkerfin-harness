import { render, screen } from '@testing-library/react'
import { Search } from 'lucide-react'
import { describe, expect, it, vi } from 'vitest'

import { Button } from './Button'
import { IconButton } from './IconButton'

describe('Button', () => {
  it('defaults to a non-submitting button and exposes its selected state', () => {
    render(<Button selected>筛选</Button>)

    const button = screen.getByRole('button', { name: '筛选' })
    expect(button).toHaveAttribute('type', 'button')
    expect(button).toHaveAttribute('aria-pressed', 'true')
    expect(button).toHaveClass('is-selected', 'ui-button--xs')
  })

  it('keeps explicit submit semantics and disables loading actions', () => {
    render(<Button type="submit" loading>保存</Button>)

    const button = screen.getByRole('button', { name: '保存' })
    expect(button).toHaveAttribute('type', 'submit')
    expect(button).toHaveAttribute('aria-busy', 'true')
    expect(button).toBeDisabled()
    expect(button.querySelector('.ui-button__spinner')).not.toBeNull()
  })

  it('distinguishes an explicit unselected toggle from a regular button', () => {
    render(<><Button selected={false}>未选筛选</Button><Button>普通操作</Button></>)

    expect(screen.getByRole('button', { name: '未选筛选' })).toHaveAttribute('aria-pressed', 'false')
    expect(screen.getByRole('button', { name: '普通操作' })).not.toHaveAttribute('aria-pressed')
  })

  it('does not invoke a disabled action', () => {
    const onClick = vi.fn()
    render(<Button disabled onClick={onClick}>不可用</Button>)

    screen.getByRole('button', { name: '不可用' }).click()
    expect(onClick).not.toHaveBeenCalled()
  })

})

describe('IconButton', () => {
  it('保留调用方的补充说明，并在提供工具提示时合并可访问描述', () => {
    const { rerender } = render(<>
      <p id="action-reason">当前模型不支持图片</p>
      <IconButton label="发送消息" aria-describedby="action-reason" disabled icon={<Search size={18} />} />
    </>)
    expect(screen.getByRole('button', { name: '发送消息' })).toHaveAccessibleDescription('当前模型不支持图片')
    rerender(<>
      <p id="action-reason">当前模型不支持图片</p>
      <IconButton label="发送消息" aria-describedby="action-reason" tooltip="发送草稿" disabled icon={<Search size={18} />} />
    </>)
    expect(screen.getByRole('button', { name: '发送消息' })).toHaveAccessibleDescription('当前模型不支持图片 发送草稿')
  })

  it('requires an accessible label and exposes the optional tooltip', () => {
    render(<IconButton label="搜索会话" tooltip="搜索会话" icon={<Search size={18} />} />)

    const button = screen.getByRole('button', { name: '搜索会话' })
    const tooltip = screen.getByRole('tooltip', { name: '搜索会话' })
    expect(button).toHaveAccessibleDescription('搜索会话')
    expect(tooltip).toHaveTextContent('搜索会话')
    expect(button.querySelector('.ui-button__label')).toBeNull()
    expect(button.querySelector('.ui-button__icon .ui-icon-button__icon')).not.toBeNull()
    expect(button).toHaveClass('ui-button--xs', 'ui-button--circle')
  })
})
