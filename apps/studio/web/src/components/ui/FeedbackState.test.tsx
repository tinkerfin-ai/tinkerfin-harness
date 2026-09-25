import { fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { FeedbackState } from './FeedbackState'

describe('FeedbackState', () => {
  it('只在错误态提供图标式恢复操作', () => {
    const onRetry = vi.fn()
    const { rerender } = render(
      <FeedbackState kind="loading" title="正在加载会话" />,
    )

    expect(screen.getByRole('status')).toHaveAttribute('aria-busy', 'true')
    expect(screen.queryByRole('button')).not.toBeInTheDocument()

    rerender(
      <FeedbackState kind="error" title="会话加载失败" onRetry={onRetry} />,
    )

    const alert = screen.getByRole('alert')
    const retry = screen.getByRole('button', { name: '重试' })
    expect(alert).toHaveTextContent(/^!会话加载失败重试$/)
    expect(alert.querySelector('.ui-feedback-icon__mark')).toHaveTextContent('!')
    expect(retry.querySelector('.ui-button__label')).toBeNull()
    fireEvent.click(retry)
    expect(onRetry).toHaveBeenCalledOnce()

    rerender(<FeedbackState kind="error" title="会话加载失败" onRetry={onRetry} retryDisabled />)
    expect(screen.getByRole('button', { name: '重试' })).toBeDisabled()
  })

  it('允许根页面提供准确的恢复名称', () => {
    render(
      <FeedbackState
        kind="error"
        title="页面暂时无法显示"
        retryLabel="重新加载页面"
        onRetry={vi.fn()}
      />,
    )

    expect(screen.getByRole('button', { name: '重新加载页面' })).toBeInTheDocument()
  })

  it('读取失败使用同一提示与文字操作，并支持键盘和禁用状态', async () => {
    const user = userEvent.setup()
    const onRetry = vi.fn()
    const { rerender } = render(
      <FeedbackState kind="error" appearance="retry" title="链路加载失败" retryLabel="重新加载" onRetry={onRetry} />,
    )

    expect(screen.getByRole('alert')).toHaveTextContent('链路加载失败')
    const retry = screen.getByRole('button', { name: '重新加载' })
    await user.tab()
    expect(retry).toHaveFocus()
    await user.keyboard('{Enter}')
    expect(onRetry).toHaveBeenCalledOnce()

    rerender(<FeedbackState kind="error" appearance="retry" title="链路加载失败" retryLabel="重新加载" onRetry={onRetry} retryDisabled />)
    expect(screen.getByRole('button', { name: '重新加载' })).toBeDisabled()
  })
})
