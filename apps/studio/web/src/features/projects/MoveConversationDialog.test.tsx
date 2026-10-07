import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { buildEmptyConversation } from '../../lib/workspace'
import { MoveConversationDialog } from './MoveConversationDialog'

const conversation = buildEmptyConversation({ projectId: 'first', threadId: 'thread', now: '2030-01-01' })
const projects = ['first', 'second', 'third'].map(id => ({ id, name: id, createdAt: '2030-01-01', updatedAt: '2030-01-01' }))
afterEach(cleanup)

it('目标项目支持键盘选择，Escape关闭列表后恢复触发器焦点', () => {
  const onClose = vi.fn(), onConfirm = vi.fn()
  render(<MoveConversationDialog conversation={conversation} projects={projects} trigger={document.createElement('button')} pending={false} error="" onClose={onClose} onConfirm={onConfirm} />)
  const trigger = screen.getByRole('button', { name: '目标项目' })
  fireEvent.click(trigger)
  const list = screen.getByRole('listbox', { name: '目标项目' })
  expect(list).toHaveFocus()
  fireEvent.keyDown(list, { key: 'ArrowDown' })
  fireEvent.keyDown(list, { key: 'Enter' })
  expect(trigger).toHaveTextContent('third')
  expect(trigger).toHaveFocus()
  fireEvent.click(trigger)
  fireEvent.keyDown(screen.getByRole('listbox'), { key: 'Escape' })
  expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
  expect(trigger).toHaveFocus()
  expect(onClose).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: '移动' }))
  expect(onConfirm).toHaveBeenCalledExactlyOnceWith('third')
})

it('等待期间禁用目标选择与关闭，结束后恢复原入口焦点', async () => {
  const trigger = document.createElement('button')
  document.body.append(trigger)
  const props = { conversation, projects, trigger, onClose: vi.fn(), onConfirm: vi.fn(), error: '' }
  const view = render(<MoveConversationDialog {...props} pending />)
  expect(screen.getByRole('button', { name: '目标项目' })).toBeDisabled()
  expect(screen.getByRole('button', { name: '取消' })).toBeDisabled()
  fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' })
  expect(props.onClose).not.toHaveBeenCalled()
  view.rerender(<MoveConversationDialog {...props} pending={false} error="移动失败，请重试" />)
  expect(screen.getByRole('alert')).toHaveTextContent('移动失败，请重试')
  view.unmount()
  await act(async () => {})
  expect(trigger).toHaveFocus()
  trigger.remove()
})
