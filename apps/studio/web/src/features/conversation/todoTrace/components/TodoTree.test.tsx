import { isInaccessible, render, screen, within } from '@testing-library/react'
import { expect, it } from 'vitest'
import type { TodoGroup, TodoTraceItemStatus } from '../../../../api/conversation/taskTrace'
import { TodoTree } from './TodoTree'

it('每个任务状态都保留可访问文本，任务正文相同时仍可区分', () => {
  const states: [TodoTraceItemStatus, string][] = [
    ['pending', '待执行'], ['running', '执行中'], ['completed', '已完成'],
    ['incomplete', '未确认完成'], ['failed', '失败'], ['cancelled', '已取消'],
  ]
  const group: TodoGroup = {
    id: 'group', userMessageId: 'message', userMessagePreview: '工作清单', groupToolCallId: 'tool',
    createdAt: '2026-10-08T00:00:00Z', status: 'running',
    todos: states.map(([status]) => ({ id: status, content: '同一任务', status })),
  }
  render(<TodoTree group={group} />)
  const rows = within(screen.getByRole('list', { name: '任务列表' })).getAllByRole('listitem')
  states.forEach(([, label], index) => {
    expect(rows[index]).toHaveTextContent('同一任务')
    expect(isInaccessible(within(rows[index]).getByText(label))).toBe(false)
  })
})
