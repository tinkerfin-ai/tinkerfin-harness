import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, it, vi } from 'vitest'
import { ConversationRunFailure } from './ConversationRunFailure'

it('仅展示会话异常及重试，支持键盘和禁用状态', async () => {
  const user = userEvent.setup()
  const retry = vi.fn()
  const { rerender } = render(<ConversationRunFailure errorCode={null} retryable disabled={false} onRetry={retry} />)
  expect(screen.getByText('会话异常')).toBeInTheDocument()
  expect(screen.getAllByRole('button')).toHaveLength(1)
  await user.tab()
  await user.keyboard('{Enter}')
  expect(retry).toHaveBeenCalledOnce()
  rerender(<ConversationRunFailure errorCode={null} retryable disabled onRetry={retry} />)
  expect(screen.getByRole('button', { name: '重试' })).toBeDisabled()
  rerender(<ConversationRunFailure errorCode={null} retryable={false} disabled={false} onRetry={retry} />)
  expect(screen.queryByRole('button')).not.toBeInTheDocument()
})


it.each([
  ['workspace_busy', '项目中还有任务在执行，请等待结束后再试'],
  ['workspace_file_conflict', '技能文件有本地修改，请先处理文件冲突后再试'],
])('历史失败 %s 保留明确原因', (errorCode, message) => {
  window.localStorage.clear()
  render(<ConversationRunFailure errorCode={errorCode} retryable disabled={false} />)
  expect(screen.getByText(message)).toBeVisible()
})
