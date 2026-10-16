import type { WorkspaceState } from '../../types'

const mustKeepOutsideSearch = (
  state: WorkspaceState,
  threadId: string,
  normalThreadIds: ReadonlySet<string>,
) => {
  if (normalThreadIds.has(threadId) || state.currentThreadId === threadId) return true
  const conversation = state.conversations.find((item) => item.threadId === threadId)
  return Boolean(
    conversation?.isHydrated
    || conversation?.runStatus === 'streaming'
    || conversation?.runStatus === 'detached'
    || conversation?.runStatus === 'waiting_approval',
  )
}

/**
 * 清理只服务于旧搜索结果的会话摘要
 *
 * Args:
 *   state: 当前 Workspace 权威状态
 *   searchOnlyThreadIds: 由搜索接口临时引入的会话
 *   normalThreadIds: 普通历史分页已经持有的会话
 *
 * Returns:
 *   清理后的 Workspace 与仍需跨查询保留的搜索会话 ID
 */
export function pruneSearchOnlyConversations(
  state: WorkspaceState,
  searchOnlyThreadIds: ReadonlySet<string>,
  normalThreadIds: ReadonlySet<string>,
) {
  const retainedSearchOnlyThreadIds = new Set(
    [...searchOnlyThreadIds].filter((threadId) => (
      mustKeepOutsideSearch(state, threadId, normalThreadIds)
    )),
  )
  const conversations = state.conversations.filter((conversation) => (
    !searchOnlyThreadIds.has(conversation.threadId)
    || retainedSearchOnlyThreadIds.has(conversation.threadId)
  ))
  return {
    state: conversations.length === state.conversations.length
      ? state
      : { ...state, conversations },
    retainedSearchOnlyThreadIds,
  }
}
