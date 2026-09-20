import type { Conversation, WorkspaceState } from '../types'

export const TRANSIENT_THREAD_ID = ''

const unloadedTaskTrace = (): Conversation['taskTrace'] => ({ phase: 'unloaded' })

const withoutTaskTrace = (conversation: Conversation): Conversation => (
  conversation.taskTrace.phase === 'unloaded'
    ? conversation
    : { ...conversation, taskTrace: unloadedTaskTrace() }
)

const sortConversations = (conversations: Conversation[]) =>
  [...conversations].sort((a, b) => Date.parse(b.updatedAt) - Date.parse(a.updatedAt))

export const buildEmptyConversation = (
  options: { threadId?: string; now: string; model?: string; mode?: Conversation['mode']; accessMode?: Conversation['accessMode'] },
): Conversation => ({
  threadId: options.threadId ?? TRANSIENT_THREAD_ID,
  title: '新会话',
  pinned: false,
  updatedAt: options.now,
  model: options.model ?? 'GPT-5.5',
  mode: options.mode ?? 'default',
  accessMode: options.accessMode ?? 'full',
  messages: [],
  runFailures: [],
  todos: [],
  taskTrace: unloadedTaskTrace(),
  runStatus: 'idle',
  isHydrated: true,
  historySynchronized: false,
})

export const createEmptyWorkspace = (): WorkspaceState => ({
  conversations: [],
  currentThreadId: TRANSIENT_THREAD_ID,
})

export function createNewConversation(
  state: WorkspaceState,
): WorkspaceState {
  return selectCurrentConversation(state, TRANSIENT_THREAD_ID)
}

/** 原子切换当前会话，并保证只有目标会话能重新取得 taskTrace */
export function selectCurrentConversation(
  state: WorkspaceState,
  threadId: string,
): WorkspaceState {
  if (state.currentThreadId === threadId) return state
  return {
    currentThreadId: threadId,
    conversations: state.conversations.map((conversation) => {
      if (conversation.threadId !== threadId) return withoutTaskTrace(conversation)
      return {
        ...conversation,
        taskTrace: conversation.isHydrated
          ? { phase: 'loading' }
          : unloadedTaskTrace(),
      }
    }),
  }
}

export function updateConversation(
  state: WorkspaceState,
  threadId: string,
  updater: (conversation: Conversation) => Conversation,
): WorkspaceState {
  return {
    ...state,
    conversations: state.conversations.map((conversation) => {
      if (conversation.threadId !== threadId) return conversation
      const updated = updater(conversation)
      return threadId === state.currentThreadId ? updated : withoutTaskTrace(updated)
    }),
  }
}

export function upsertConversation(
  state: WorkspaceState,
  nextConversation: Conversation,
): WorkspaceState {
  const hasConversation = state.conversations.some(
    (conversation) => conversation.threadId === nextConversation.threadId,
  )
  const ownedNext = nextConversation.threadId === state.currentThreadId
    ? nextConversation
    : withoutTaskTrace(nextConversation)
  const conversations = hasConversation
    ? state.conversations.map((conversation) =>
        conversation.threadId === nextConversation.threadId
          ? ownedNext
          : conversation)
    : [ownedNext, ...state.conversations]
  return {
    ...state,
    conversations: sortConversations(conversations),
  }
}

export function removeConversation(
  state: WorkspaceState,
  threadId: string,
): WorkspaceState {
  const conversations = state.conversations.filter(
    (conversation) => conversation.threadId !== threadId,
  )
  const currentThreadId = state.currentThreadId === threadId
    ? (conversations[0]?.threadId ?? TRANSIENT_THREAD_ID)
    : state.currentThreadId
  return selectCurrentConversation({
    conversations,
    currentThreadId: state.currentThreadId,
  }, currentThreadId)
}


type TitleFields = Pick<Conversation, 'title' | 'titleSource' | 'titleGenerationStatus' | 'titleSeq'>

/** 只有标题自身的提交序号决定覆盖关系，Trace 与 HTTP 响应先后不参与判断 */
export function mergeConversationTitle(current: TitleFields | undefined, incoming: TitleFields): TitleFields {
  const value = current?.titleSeq !== undefined && (incoming.titleSeq === undefined || incoming.titleSeq < current.titleSeq)
    ? current : incoming
  return { title: value.title, titleSource: value.titleSource, titleGenerationStatus: value.titleGenerationStatus, titleSeq: value.titleSeq }
}

/** 连接中断时，已确认仍在运行的当前任务继续显示加载状态 */
export function isConversationRunning(conversation: Conversation): boolean {
  return conversation.runStatus === 'streaming'
    || (conversation.runStatus === 'detached' && conversation.trace?.status.execution === 'running'
      && conversation.trace.headRunId === conversation.activeRunId)
}
