import { fireEvent, render, screen, within } from '@testing-library/react'
import { expect, it, vi } from 'vitest'
import { InteractionSubmission } from './InteractionSubmission'

it('停止与重新检查归属确认区，提交内容按需展开', () => {
  const retry = vi.fn()
  const stop = vi.fn()
  const view = render(<InteractionSubmission state={{ failed: false, checking: false, retry }} stopControl={<button type="button" onClick={stop}>停止任务</button>}><p>已提交的原输入</p></InteractionSubmission>)
  const region = screen.getByRole('region', { name: '提交确认' })
  fireEvent.click(within(region).getByRole('button', { name: '停止任务' }))
  expect(stop).toHaveBeenCalledOnce()
  expect(screen.getByText('已提交的原输入')).not.toBeVisible()
  fireEvent.click(screen.getByText('查看提交内容'))
  expect(screen.getByText('已提交的原输入')).toBeVisible()
  view.rerender(<InteractionSubmission state={{ failed: true, checking: false, retry }}><p>已提交的原输入</p></InteractionSubmission>)
  expect(screen.getByRole('status')).toHaveTextContent('暂时无法确认提交结果')
  fireEvent.click(screen.getByRole('button', { name: '重新检查状态' }))
  expect(retry).toHaveBeenCalledOnce()
  view.rerender(<InteractionSubmission state={{ failed: false, checking: true, retry }}><p>已提交的原输入</p></InteractionSubmission>)
  expect(screen.getByRole('button', { name: '重新检查状态' })).toBeDisabled()
})
