import { describe, expect, it } from 'vitest'

import type { TodoGroup } from '../../../api/conversation/taskTrace'
import { calculateTodoGroupWindow } from './useTodoGroupWindow'

const groups = Array.from({ length: 24 }, (_, index): TodoGroup => ({
  id: `todo-group:run-${index}`,
  userMessageId: `message-${index}`,
  userMessagePreview: `任务组 ${index}`,
  groupToolCallId: `tool-${index}`,
  createdAt: new Date(Date.UTC(2026, 7, 30, 12, 0, index)).toISOString(),
  status: 'running',
  todos: Array.from({ length: 10 }, (__, todoIndex) => ({
    id: `todo-${index}-${todoIndex}`,
    content: `任务 ${todoIndex}`,
    status: 'pending',
  })),
}))

describe('calculateTodoGroupWindow', () => {
  it('uses measured expanded height without changing group count or order', () => {
    const measured = new Map([[groups[10]!.id, 900]])
    const result = calculateTodoGroupWindow({
      groups,
      expandedIds: new Set([groups[10]!.id]),
      scrollTop: 500,
      viewportHeight: 768,
      measuredHeights: measured,
    })

    expect(result.offsets[11]! - result.offsets[10]!).toBe(900)
    expect(result.items.map((item) => item.group.id))
      .toEqual([...result.items].sort((a, b) => a.index - b.index).map((item) => item.group.id))
  })

  it('does not retain an offscreen group old expanded height after expansion moves', () => {
    const result = calculateTodoGroupWindow({
      groups,
      expandedIds: new Set([groups[20]!.id]),
      scrollTop: 1_200,
      viewportHeight: 768,
      measuredHeights: new Map([[groups[10]!.id, 900]]),
    })

    expect(result.offsets[11]! - result.offsets[10]!).toBe(72)
    expect(result.offsets[21]! - result.offsets[20]!).toBeGreaterThan(72)
  })

  it('reserves independent dynamic height for every expanded group', () => {
    const result = calculateTodoGroupWindow({
      groups,
      expandedIds: new Set([groups[10]!.id, groups[11]!.id]),
      scrollTop: 600,
      viewportHeight: 768,
    })

    expect(result.offsets[11]! - result.offsets[10]!).toBeGreaterThan(72)
    expect(result.offsets[12]! - result.offsets[11]!).toBeGreaterThan(72)
  })
})
