import { describe, expect, it } from 'vitest'

import {
  buildEmptyConversation,
  createNewConversation,
  selectCurrentConversation,
  updateConversation,
  upsertConversation,
} from './workspace'

const BASE_TIME = '2026-08-03T09:00:00.000Z'

function conversation(
  overrides: Partial<{
    threadId: string
    title: string
    content: string
    pinned: boolean
    updatedAt: string
  }> = {},
) {
  const {
    threadId = 'thread-1',
    title = '已有会话',
    content = '已有消息',
    pinned = false,
    updatedAt = BASE_TIME,
  } = overrides

  return {
    threadId,
    title,
    pinned,
    updatedAt,
    model: 'GPT-5.5',
    mode: 'default' as const,
    accessMode: 'write_approval' as const,
    messages: content
      ? [
          {
            id: `${threadId}-message-1`,
            role: 'user' as const,
            content,
            createdAt: updatedAt,
          },
        ]
      : [],
    todos: [],
    taskTrace: { phase: 'unloaded' as const },
    runStatus: 'idle' as const,
    isHydrated: true,
    historySynchronized: false,
  }
}

function workspace() {
  return {
    conversations: [conversation()],
    currentThreadId: 'thread-1',
  }
}

describe('workspace conversation behavior', () => {
  it('switches to the blank draft slot without inserting a local conversation row', () => {
    const next = createNewConversation(workspace())

    expect(next.conversations).toHaveLength(1)
    expect(next.currentThreadId).toBe('')
  })

  it('uses the fixed draft title until the backend returns the authoritative title', () => {
    const draft = buildEmptyConversation({
      now: '2026-08-03T11:00:00.000Z',
      model: 'GPT-5.5',
    })

    expect(draft.threadId).toBe('')
    expect(draft.title).toBe('新会话')
    expect(draft.messages).toEqual([])
  })

  it('upserts backend conversations and keeps the newest one first', () => {
    const next = upsertConversation(workspace(), conversation({
      threadId: 'thread-2',
      title: '新同步会话',
      updatedAt: '2026-08-03T12:00:00.000Z',
    }))

    expect(next.conversations.map((item) => item.threadId)).toEqual(['thread-2', 'thread-1'])
    expect(next.currentThreadId).toBe('thread-1')
  })

  it('atomically clears every non-current task trace during selection', () => {
    const initial = {
      conversations: [
        {
          ...conversation({ threadId: 'thread-1' }),
          taskTrace: { phase: 'ready' as const, snapshot: { status: 'ready' as const, todoGroups: [] } },
        },
        conversation({ threadId: 'thread-2' }),
      ],
      currentThreadId: 'thread-1',
    }

    const next = selectCurrentConversation(initial, 'thread-2')

    expect(next.conversations.find((item) => item.threadId === 'thread-1')?.taskTrace)
      .toEqual({ phase: 'unloaded' })
    expect(next.conversations.find((item) => item.threadId === 'thread-2')?.taskTrace)
      .toEqual({ phase: 'loading' })
  })

  it('prevents background updates from repopulating task trace', () => {
    const initial = {
      conversations: [
        conversation({ threadId: 'thread-1' }),
        conversation({ threadId: 'thread-2' }),
      ],
      currentThreadId: 'thread-1',
    }

    const next = updateConversation(initial, 'thread-2', (item) => ({
      ...item,
      taskTrace: { phase: 'ready', snapshot: { status: 'ready', todoGroups: [] } },
    }))

    expect(next.conversations.find((item) => item.threadId === 'thread-2')?.taskTrace)
      .toEqual({ phase: 'unloaded' })
  })
})
