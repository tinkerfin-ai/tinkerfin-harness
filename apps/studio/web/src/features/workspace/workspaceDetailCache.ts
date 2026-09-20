import type { Conversation, WorkspaceState } from '../../types'
import { createEmptyWorkspace } from '../../lib/workspace'

const ENDED_CONVERSATION_LIMIT = 3

export type ComposerPreferences = Pick<Conversation, 'model' | 'mode' | 'accessMode'>

type WorkspaceUpdate = WorkspaceState | ((state: WorkspaceState) => WorkspaceState)

interface WorkspaceCacheState {
  workspace: WorkspaceState
  visits: string[]
  retained: ReadonlyMap<symbol, string>
  preferences: ReadonlyMap<string, Partial<ComposerPreferences>>
}

type WorkspaceCacheAction =
  | { type: 'update'; update: WorkspaceUpdate }
  | { type: 'retain'; token: symbol; threadId: string }
  | { type: 'move'; token: symbol; threadId: string }
  | { type: 'release'; token: symbol }
  | { type: 'preferences'; threadId: string; patch: Partial<ComposerPreferences> }
  | { type: 'accepted'; threadId: string; submitted: ComposerPreferences }

export const createWorkspaceCacheState = (): WorkspaceCacheState => ({
  workspace: createEmptyWorkspace(),
  visits: [],
  retained: new Map(),
  preferences: new Map(),
})

const canReloadEndedDetails = (conversation: Conversation) => (
  conversation.historySynchronized
  && conversation.trace !== undefined
  && ['succeeded', 'failed', 'cancelled', 'abandoned'].includes(conversation.trace.status.execution)
  && (conversation.runStatus === 'idle' || conversation.runStatus === 'error')
  && !conversation.approval
  && !conversation.planInteraction
  && !conversation.pendingInteractionKind
  && !conversation.notice?.recovery
)

/** 只保留列表和再次打开会话所需的字段，避免新详情字段被顺带留在缓存中 */
const unloadConversationDetails = (conversation: Conversation): Conversation => ({
  threadId: conversation.threadId,
  title: conversation.title,
  titleSource: conversation.titleSource,
  titleGenerationStatus: conversation.titleGenerationStatus,
  titleSeq: conversation.titleSeq,
  pinned: conversation.pinned,
  updatedAt: conversation.updatedAt,
  model: conversation.model,
  mode: conversation.mode,
  accessMode: conversation.accessMode,
  runStatus: conversation.runStatus,
  activeRunId: conversation.activeRunId,
  messages: [],
  runFailures: [],
  todos: [],
  taskTrace: { phase: 'unloaded' },
  isHydrated: false,
  historySynchronized: false,
})

const trimDetails = (
  workspace: WorkspaceState,
  visits: readonly string[],
  retained: ReadonlyMap<symbol, string>,
): WorkspaceState => {
  const candidates = workspace.conversations.filter(canReloadEndedDetails)
  if (candidates.length <= ENDED_CONVERSATION_LIMIT) return workspace
  const positions = new Map(visits.map((threadId, index) => [threadId, index]))
  candidates.sort((left, right) => {
    if (left.threadId === workspace.currentThreadId) return -1
    if (right.threadId === workspace.currentThreadId) return 1
    return (positions.get(left.threadId) ?? Number.MAX_SAFE_INTEGER)
      - (positions.get(right.threadId) ?? Number.MAX_SAFE_INTEGER)
      || left.threadId.localeCompare(right.threadId)
  })
  const protectedThreads = new Set(retained.values())
  const evicted = new Set(candidates.slice(ENDED_CONVERSATION_LIMIT)
    .filter(conversation => !protectedThreads.has(conversation.threadId))
    .map(conversation => conversation.threadId))
  if (evicted.size === 0) return workspace
  return {
    ...workspace,
    conversations: workspace.conversations.map(conversation => (
      evicted.has(conversation.threadId) ? unloadConversationDetails(conversation) : conversation
    )),
  }
}

const withPreferences = (
  conversation: Conversation,
  preferences: Partial<ComposerPreferences> | undefined,
): Conversation => {
  if (!preferences) return conversation
  const model = preferences.model ?? conversation.model
  const mode = preferences.mode ?? conversation.mode
  const accessMode = preferences.accessMode ?? conversation.accessMode
  return model === conversation.model && mode === conversation.mode && accessMode === conversation.accessMode
    ? conversation
    : { ...conversation, model, mode, accessMode }
}

const applyPreferences = (
  workspace: WorkspaceState,
  preferences: WorkspaceCacheState['preferences'],
): WorkspaceState => {
  let changed = false
  const conversations = workspace.conversations.map(conversation => {
    const next = withPreferences(conversation, preferences.get(conversation.threadId))
    changed ||= next !== conversation
    return next
  })
  return changed ? { ...workspace, conversations } : workspace
}

/** 工作台写入、访问顺序与详情保护在同一队列内结算，异步收尾不会越过缓存裁剪 */
export function reduceWorkspaceCache(
  state: WorkspaceCacheState,
  action: WorkspaceCacheAction,
): WorkspaceCacheState {
  if (action.type === 'update') {
    const updated = typeof action.update === 'function' ? action.update(state.workspace) : action.update
    if (updated === state.workspace) return state
    const existingIds = new Set(updated.conversations.map(conversation => conversation.threadId))
    const removedIds = new Set(state.workspace.conversations
      .filter(conversation => !existingIds.has(conversation.threadId))
      .map(conversation => conversation.threadId))
    const retained = removedIds.size === 0 ? state.retained : new Map(
      [...state.retained].filter(([, threadId]) => !removedIds.has(threadId)),
    )
    const preferences = removedIds.size === 0 ? state.preferences : new Map(
      [...state.preferences].filter(([threadId]) => !removedIds.has(threadId)),
    )
    let visits = state.visits.filter(threadId => existingIds.has(threadId))
    if (updated.currentThreadId && existingIds.has(updated.currentThreadId)
      && (updated.currentThreadId !== state.workspace.currentThreadId || !visits.includes(updated.currentThreadId))) {
      visits = [updated.currentThreadId, ...visits.filter(threadId => threadId !== updated.currentThreadId)]
    }
    const workspace = trimDetails(applyPreferences(updated, preferences), visits, retained)
    return { workspace, visits, retained, preferences }
  }

  if (action.type === 'retain' || action.type === 'move' || action.type === 'release') {
    if (action.type !== 'retain' && !state.retained.has(action.token)) return state
    const retained = new Map(state.retained)
    if (action.type === 'release') retained.delete(action.token)
    else retained.set(action.token, action.threadId)
    return { ...state, retained, workspace: trimDetails(state.workspace, state.visits, retained) }
  }

  if (action.type === 'preferences') {
    if (!state.workspace.conversations.some(conversation => conversation.threadId === action.threadId)) return state
    const patch: Partial<ComposerPreferences> = {
      ...(action.patch.model !== undefined ? { model: action.patch.model } : {}),
      ...(action.patch.mode !== undefined ? { mode: action.patch.mode } : {}),
      ...(action.patch.accessMode !== undefined ? { accessMode: action.patch.accessMode } : {}),
    }
    if (Object.keys(patch).length === 0) return state
    const preferences = new Map(state.preferences)
    preferences.set(action.threadId, { ...preferences.get(action.threadId), ...patch })
    return { ...state, preferences, workspace: applyPreferences(state.workspace, preferences) }
  }

  const current = state.preferences.get(action.threadId)
  if (!current) return state
  const remaining = { ...current }
  for (const key of ['model', 'mode', 'accessMode'] as const) {
    if (remaining[key] === action.submitted[key]) delete remaining[key]
  }
  const preferences = new Map(state.preferences)
  if (Object.keys(remaining).length) preferences.set(action.threadId, remaining)
  else preferences.delete(action.threadId)
  return { ...state, preferences }
}
