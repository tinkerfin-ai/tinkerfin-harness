import type { ConversationRunFailure } from '../../../api/conversation/history'
import type { TodoGroup } from '../../../api/conversation/taskTrace'
import type { Conversation, Message } from '../../../types'

export type ConversationDisplayEntry =
  | { type: 'run-failure'; message: Message; failure: ConversationRunFailure }
  | { type: 'message'; message: Message }
  | { type: 'tools'; messages: Message[] }
  | { type: 'todo-group'; group: TodoGroup; message: Message }

type Classified =
  | { type: 'message'; message: Message }
  | { type: 'todo-group'; group: TodoGroup; message: Message }
  | null

const originRunId = (group: TodoGroup) => (
  group.id.startsWith('todo-group:') ? group.id.slice('todo-group:'.length) : ''
)

export const buildConversationDisplayEntries = (
  conversation: Conversation,
): ConversationDisplayEntry[] => {
  const groups = conversation.taskTrace.phase === 'ready'
    ? conversation.taskTrace.snapshot.todoGroups
    : []
  const groupsByTool = new Map<string, TodoGroup>()
  const groupedRuns = new Set<string>()
  for (const group of groups) {
    groupsByTool.set(group.groupToolCallId, group)
    const origin = originRunId(group)
    groupedRuns.add(origin)
    // 讨论会增加用户消息，但恢复运行仍属于 Trace 中原来的任务轮次
    const nodes = conversation.trace?.graph.nodes ?? []
    const turns = new Set(nodes.filter(node => node.runId === origin).map(node => node.turnId))
    for (const node of nodes) if (turns.has(node.turnId)) groupedRuns.add(node.runId)
  }
  const pendingApproval = conversation.approval?.submitted === false
    ? conversation.approval
    : undefined
  const activeApprovalIndex = pendingApproval
    ? Math.max(0, Math.min(pendingApproval.activeIndex, pendingApproval.items.length - 1))
    : -1
  const activeApprovalToolCallId = pendingApproval?.items[activeApprovalIndex]?.toolCallId
  const approvalToolCallIds = new Set(
    pendingApproval?.items.flatMap((item) => item.toolCallId ? [item.toolCallId] : []) ?? [],
  )
  const shownUnconfirmedRuns = new Set<string>()
  let turnRunId: string | undefined

  const classify = (message: Message): Classified => {
    if (message.role === 'user') turnRunId = message.meta?.runId
    if (message.role !== 'tool') return { type: 'message', message }
    if (message.meta?.sourceAgentName) return null
    if (
      activeApprovalToolCallId
      && message.meta?.toolCallId === activeApprovalToolCallId
    ) return { type: 'message', message }
    if (
      message.meta?.toolCallId
      && approvalToolCallIds.has(message.meta.toolCallId)
    ) return null
    if (message.meta?.toolName === 'task') {
      return message.meta.status !== 'running' && !message.meta.subRunId
        ? { type: 'message', message }
        : null
    }
    if (message.meta?.toolName !== 'write_todos') return { type: 'message', message }

    const group = groupsByTool.get(message.id)
      ?? (message.meta.toolCallId ? groupsByTool.get(message.meta.toolCallId) : undefined)
    if (group) return { type: 'todo-group', group, message }
    if (
      message.meta.status === 'failed'
      || message.meta.status === 'cancelled'
      || message.meta.status === 'running'
      || message.meta.status === 'paused'
    ) return { type: 'message', message }
    const runId = turnRunId ?? message.meta.runId ?? ''
    if (groupedRuns.has(runId) || shownUnconfirmedRuns.has(runId)) return null
    shownUnconfirmedRuns.add(runId)
    return { type: 'message', message }
  }

  const entries: ConversationDisplayEntry[] = []
  for (let index = 0; index < conversation.messages.length;) {
    const message = conversation.messages[index]
    if (!message) break
    const batchId = message.role === 'tool' ? message.meta?.batchId : undefined
    if (!batchId) {
      const classified = classify(message)
      if (classified?.type === 'message' && classified.message.role === 'tool') {
        entries.push({ type: 'tools', messages: [classified.message] })
      } else if (classified) entries.push(classified)
      index += 1
      continue
    }

    const batch: Array<{ classified: Classified; index: number }> = []
    let cursor = index
    while (
      cursor < conversation.messages.length
      && conversation.messages[cursor]?.role === 'tool'
      && conversation.messages[cursor]?.meta?.batchId === batchId
    ) {
      const current = conversation.messages[cursor]
      if (current) batch.push({ classified: classify(current), index: cursor })
      cursor += 1
    }
    const ordinary = batch.flatMap(({ classified }) => (
      classified?.type === 'message' && classified.message.role === 'tool'
        ? [classified.message]
        : []
    ))
    const firstOrdinaryIndex = batch.find(
      ({ classified }) => classified?.type === 'message' && classified.message.role === 'tool',
    )?.index
    for (const item of batch) {
      if (item.index === firstOrdinaryIndex && ordinary.length > 0) {
        entries.push({ type: 'tools', messages: ordinary })
      }
      if (item.classified?.type === 'todo-group') entries.push(item.classified)
    }
    index = cursor
  }
  const failures = new Map((conversation.runFailures ?? []).map(failure => [failure.runId, failure]))
  let question: Message | undefined
  return entries.flatMap((entry, index): ConversationDisplayEntry[] => {
    if (entry.type === 'message' && entry.message.role === 'user') question = entry.message
    const next = entries[index + 1]
    const turnEnded = !next || (next.type === 'message' && next.message.role === 'user')
    const failure = question?.meta?.runId ? failures.get(question.meta.runId) : undefined
    return turnEnded && question && failure
      ? [entry, { type: 'run-failure', message: question, failure }]
      : [entry]
  })
}
