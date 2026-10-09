import { render, screen } from '@testing-library/react'
import { Mail } from 'lucide-react'
import { describe, expect, it } from 'vitest'

import { TextField } from './TextField'

describe('TextField', () => {
  it('associates its visible label, helper and inline error with the input', () => {
    render(
      <TextField
        label="邮箱地址"
        helperText="用于接收通知"
        error="邮箱格式无效"
        leadingContent={<Mail size={16} />}
      />,
    )

    const input = screen.getByLabelText('邮箱地址')
    expect(input).toHaveAttribute('aria-invalid', 'true')
    expect(input).toHaveAccessibleDescription('邮箱格式无效 用于接收通知')
    expect(screen.getByRole('alert')).toHaveTextContent('邮箱格式无效')
  })

  it('为没有显式标识的字段关联独立标签', () => {
    render(<><TextField label="搜索" /><TextField label="名称" /></>)
    const input = screen.getByLabelText('搜索')
    expect(input.id).not.toBe('')
    expect(screen.getByLabelText('名称').id).not.toBe(input.id)

  })

  it('forwards disabled and read-only semantics and exposes loading state', () => {
    const { rerender } = render(<TextField label="用户名" disabled />)
    expect(screen.getByLabelText('用户名')).toBeDisabled()

    rerender(<TextField label="用户名" readOnly />)
    expect(screen.getByLabelText('用户名')).toHaveAttribute('readonly')
    expect(screen.getByLabelText('用户名')).not.toBeDisabled()

    rerender(<TextField label="用户名" loading />)
    expect(screen.getByLabelText('用户名')).toHaveAttribute('aria-busy', 'true')
    expect(screen.getByRole('status', { name: '加载中' })).toBeInTheDocument()
  })
})
