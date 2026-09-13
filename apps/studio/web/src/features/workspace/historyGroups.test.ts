import { describe, expect, it } from 'vitest'

import type { Conversation } from '../../types'
import { groupConversationHistory } from './historyGroups'

const conversation = (
  threadId: string,
  updatedAt: string,
  pinned = false,
): Conversation => ({ accessMode: 'write_approval',
  threadId,
  title: threadId,
  pinned,
  updatedAt,
  model: 'GPT-5.5',
  mode: 'default',
  messages: [],
  todos: [],
  taskTrace: { phase: 'unloaded' },
  runStatus: 'idle',
})

describe('groupConversationHistory', () => {
  it('按置顶和浏览器本地自然日生成稳定分组', () => {
    const now = new Date('2026-08-23T00:30:00+08:00')
    const groups = groupConversationHistory([
      conversation('更早', '2026-07-22T15:59:59Z'),
      conversation('30天内', '2026-08-14T16:00:00Z'),
      conversation('7天内-旧', '2026-08-20T16:00:00Z'),
      conversation('7天内-新', '2026-08-21T16:00:00Z'),
      // 服务端数据库时间是 UTC 无时区值；这里应换算为上海时间 8 月 23 日
      conversation('今天', '2026-08-22T17:00:00'),
      conversation('置顶', '2026-07-01T00:00:00Z', true),
    ], [7, 30], now)

    expect(groups.map((group) => group.label)).toEqual([
      '置顶',
      '今天',
      '7天内',
      '30天内',
      '更早',
    ])
    expect(groups[2]?.items.map((item) => item.threadId)).toEqual([
      '7天内-新',
      '7天内-旧',
    ])
    expect(groups[0]?.items.map((item) => item.threadId)).toEqual(['置顶'])
  })
})
