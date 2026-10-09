import { fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, it, vi } from 'vitest'
import { Breadcrumbs } from './Breadcrumbs'

it('上级可以导航，最后一项只有当前位置语义', async () => {
  const user = userEvent.setup()
  const navigate = vi.fn()
  render(<Breadcrumbs label="文件路径" current="location" items={[{ label: '工作区', onNavigate: navigate }, { label: 'scripts' }]} />)
  expect(screen.getByRole('navigation', { name: '文件路径' })).toBeVisible()
  expect(screen.getByText('scripts')).toHaveAttribute('aria-current', 'location')
  expect(screen.queryByRole('button', { name: 'scripts' })).toBeNull()
  await user.tab()
  expect(screen.getByRole('button', { name: '工作区' })).toHaveFocus()
  await user.keyboard('{Enter}')
  expect(navigate).toHaveBeenCalledOnce()
})

it('保存中禁用所有上级，完整名称保持可访问，当前页默认使用 page', () => {
  const navigate = vi.fn()
  const name = '很长的模型提供方名称'.repeat(8)
  render(<Breadcrumbs label="模型配置导航" disabled items={[{ label: '模型配置', onNavigate: navigate }, { label: name, onNavigate: navigate }, { label: '添加模型' }]} />)
  for (const button of screen.getAllByRole('button')) {
    expect(button).toBeDisabled()
    fireEvent.click(button)
  }
  expect(screen.getByRole('button', { name })).toBeInTheDocument()
  expect(screen.getByText('添加模型')).toHaveAttribute('aria-current', 'page')
  expect(navigate).not.toHaveBeenCalled()
})
