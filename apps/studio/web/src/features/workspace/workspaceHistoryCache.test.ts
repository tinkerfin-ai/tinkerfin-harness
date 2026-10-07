import { describe, expect, it } from 'vitest'

import { buildEmptyConversation } from '../../lib/workspace'
import type { Conversation, WorkspaceState } from '../../types'
import { pruneSearchOnlyConversations } from './workspaceHistoryCache'

const conversation = (
  threadId: string,
  overrides: Partial<Conversation> = {},
): Conversation => ({
  ...buildEmptyConversation({projectId: 'project-1',
    threadId,
    now: '2026-08-25T00:00:00.000Z',
    model: 'GPT-5.5',
  }),
  isHydrated: false,
  ...overrides,
})

describe('workspace search cache', () => {
  it('drops stale summaries while preserving user and runtime-owned conversations', () => {
    const state: WorkspaceState = {
      currentThreadId: 'selected-search',
      conversations: [
        conversation('normal-history'),
        conversation('stale-search'),
        conversation('selected-search'),
        conversation('hydrated-search', { isHydrated: true }),
        conversation('running-search', { runStatus: 'detached', activeRunId: 'run-1' }),
      ],
    }

    const result = pruneSearchOnlyConversations(
      state,
      new Set(['stale-search', 'selected-search', 'hydrated-search', 'running-search']),
      new Set(['normal-history']),
    )

    expect(result.state.conversations.map((item) => item.threadId)).toEqual([
      'normal-history',
      'selected-search',
      'hydrated-search',
      'running-search',
    ])
    expect([...result.retainedSearchOnlyThreadIds]).toEqual([
      'selected-search',
      'hydrated-search',
      'running-search',
    ])
  })
})
