import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import type { TodoGroup, TodoTraceItemStatus } from '../../../../api/conversation/taskTrace'
import type { Message } from '../../../../types'
import { TodoGroupRow } from './TodoGroupRow'

const message: Message = {
  id: 'todo-tool', role: 'tool', content: 'write_todos', createdAt: '2026-10-07T00:00:00Z',
  meta: { toolName: 'write_todos', toolCallId: 'todo-tool', status: 'completed' },
}

const group = (statuses: TodoTraceItemStatus[]): TodoGroup => ({
  id: 'todo-group:run', userMessageId: 'question', userMessagePreview: '调研岗位',
  groupToolCallId: message.id, createdAt: message.createdAt, status: 'running',
  todos: statuses.map((status, index) => ({ id: `todo-${index}`, content: `任务 ${index + 1}`, status })),
})

describe('Todos 进度计数', () => {
  it.each<{ statuses: TodoTraceItemStatus[]; expected: string }>([
    { statuses: ['running', 'pending', 'pending', 'pending', 'pending', 'pending'], expected: '1/6' },
    { statuses: ['completed', 'running', 'pending', 'pending', 'pending', 'pending'], expected: '2/6' },
    { statuses: ['running', 'running', 'pending'], expected: '2/3' },
    { statuses: ['completed', 'completed'], expected: '2/2' },
    { statuses: ['pending', 'pending'], expected: '0/2' },
    { statuses: ['completed', 'incomplete', 'failed', 'cancelled', 'pending'], expected: '1/5' },
    { statuses: [], expected: '0/0' },
  ])('状态 $statuses 显示 $expected', ({ statuses, expected }) => {
    render(<TodoGroupRow group={group(statuses)} message={message} />)
    expect(screen.getByText(expected, { exact: true })).toBeVisible()
  })
})
