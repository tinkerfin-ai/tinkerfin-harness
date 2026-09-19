import { mergeConversationTitle } from "../../../lib/workspace"
import { messageAttachments, messageText, type Attachment } from '../attachments/content'
import type {
  ConversationHistoryCoreDetail,
  ConversationHistoryDetail,
  ConversationTraceUpdate,
  TraceInteraction,
} from '../../../api/conversation/history'
import { parseRunFailures, traceObservationTime } from '../../../api/conversation/history'
import {
  parseTraceGraph,
  type TraceGraph,
  type TraceGraphDelta,
  type TraceGraphNode,
} from '../../../api/conversation/traceGraph'
import type { TaskTraceSnapshot } from '../../../api/conversation/taskTrace'
import { ConversationError } from '../../../api/conversation/errors'
import type {
  ApprovalState,
  Conversation,
  JsonObject,
  JsonValue,
  Message,
  PendingInteractionKind,
  TodoItem,
  WebTaskTraceViewState,
} from '../../../types'
import { approvalItemsFromInterrupts, planInteractionFromInterrupts } from '../agui'

const isObject = (value: unknown): value is JsonObject => (
  value != null && typeof value === 'object' && !Array.isArray(value)
)

const text = (value: JsonValue | null | undefined): string => {
  if (typeof value === 'string') return value
  if (value == null) return ''
  return JSON.stringify(value, null, 2)
}

const elapsedMs = (startedAt: string, completedAt?: string | null) => {
  if (!completedAt) return undefined
  const duration = Date.parse(completedAt) - Date.parse(startedAt)
  return Number.isFinite(duration) && duration >= 0 ? duration : undefined
}

const nodeMessageStatus = (
  status: TraceGraphNode['status'],
): NonNullable<Message['meta']>['status'] => {
  switch (status) {
    case 'running': return 'running'
    case 'waiting': return 'paused'
    case 'succeeded': return 'completed'
    case 'cancelled':
    case 'abandoned': return 'cancelled'
    default: return 'failed'
  }
}

const todosFromState = (root: JsonObject): TodoItem[] => {
  if (!Array.isArray(root.todos)) return []
  return root.todos.flatMap((value, index) => {
    if (!isObject(value) || typeof value.content !== 'string') return []
    const rawStatus = value.status
    const status: TodoItem['status'] = rawStatus === 'completed'
      ? 'completed'
      : rawStatus === 'in_progress'
        ? 'running'
        : rawStatus === 'failed'
          ? 'failed'
          : rawStatus === 'cancelled'
            ? 'cancelled'
            : 'pending'
    return [{
      id: typeof value.id === 'string' ? value.id : 'trace-todo-' + index,
      content: value.content,
      status,
    }]
  })
}

const modeFromState = (root: JsonObject): Conversation['mode'] => {
  const plan = root.tinkerfin_plan
  return isObject(plan) && plan.effectiveMode === 'plan' ? 'plan' : 'default'
}

const applyEntityDelta = <T extends { id: string }>(
  current: T[],
  upserts: T[],
  removes: string[],
): T[] => {
  const values = new Map(current.map((item) => [item.id, item]))
  for (const id of removes) values.delete(id)
  for (const item of upserts) values.set(item.id, structuredClone(item))
  return [...values.values()]
}

const applyTraceGraphDelta = (
  current: TraceGraph,
  delta: TraceGraphDelta,
): TraceGraph => {
  if (delta.asOfSeq < current.asOfSeq) throw new ConversationError('stream_event_invalid')
  if (delta.asOfSeq === current.asOfSeq && (
    delta.turnUpserts.length || delta.turnRemoves.length
    || delta.nodeUpserts.length || delta.nodeRemoves.length
    || JSON.stringify(delta.orderedNodeIds) !== JSON.stringify(current.orderedNodeIds)
    || JSON.stringify(delta.matchedNodeIds) !== JSON.stringify(current.matchedNodeIds)
    || JSON.stringify(delta.completeness) !== JSON.stringify(current.completeness)
  )) throw new ConversationError('stream_event_invalid')
  const currentTurns = new Map(current.turns.map((turn) => [turn.id, turn]))
  delta.turnUpserts.forEach((turn) => {
    const previous = currentTurns.get(turn.id)
    if (previous && (
      previous.ordinal !== turn.ordinal
      || previous.startedAt !== turn.startedAt
    )) throw new ConversationError('stream_event_invalid')
  })
  const currentNodes = new Map(current.nodes.map((node) => [node.id, node]))
  delta.nodeUpserts.forEach((node) => {
    const previous = currentNodes.get(node.id)
    if (previous && (
      node.updatedSeq < previous.updatedSeq
      || node.turnId !== previous.turnId
      || node.kind !== previous.kind
      || node.name !== previous.name
      || JSON.stringify(node.graphNamespace) !== JSON.stringify(previous.graphNamespace)
    )) throw new ConversationError('stream_event_invalid')
  })
  const nodeValues = applyEntityDelta(
    current.nodes,
    delta.nodeUpserts,
    delta.nodeRemoves,
  )
  const nodesById = new Map(nodeValues.map((node) => [node.id, node]))
  if (
    delta.orderedNodeIds.length !== nodeValues.length
    || new Set(delta.orderedNodeIds).size !== nodeValues.length
    || delta.orderedNodeIds.some((id) => !nodesById.has(id))
    || delta.matchedNodeIds.some((id) => !nodesById.has(id))
  ) throw new ConversationError('stream_event_invalid')
  const turns = applyEntityDelta(
    current.turns,
    delta.turnUpserts,
    delta.turnRemoves,
  ).sort((left, right) => left.ordinal - right.ordinal || left.id.localeCompare(right.id))
  return parseTraceGraph({
    turns,
    nodes: delta.orderedNodeIds.map((id) => nodesById.get(id) as TraceGraphNode),
    orderedNodeIds: [...delta.orderedNodeIds],
    matchedNodeIds: [...delta.matchedNodeIds],
    asOfSeq: delta.asOfSeq,
    completeness: structuredClone(delta.completeness),
  })
}

const scopedSourceKey = (graphNamespace: string[], sourceId: string): string => (
  JSON.stringify([graphNamespace, sourceId])
)

const interactionState = (
  interactions: TraceInteraction[],
): {
  approval?: ApprovalState
  planInteraction?: Conversation['planInteraction']
  pendingInteractionKind?: PendingInteractionKind
} => {
  const pending = interactions
    .filter((interaction) => interaction.status === 'pending')
    .sort((left, right) => left.traceSeq - right.traceSeq || left.id.localeCompare(right.id))
  if (pending.length === 0) return {}
  // 缺失的审批内容只能提示补全，不能推测参数或生成可提交的动作
  if (pending.some((interaction) => interaction.agui === null)) {
    return { pendingInteractionKind: 'input_required' }
  }
  const interrupts = pending.flatMap((interaction) => interaction.agui ?? [])
  const plan = planInteractionFromInterrupts(interrupts)
  if (plan) {
    return {
      planInteraction: plan,
      pendingInteractionKind: plan.kind === 'questions' ? 'plan_clarification' : 'plan_review',
    }
  }
  if (interrupts.length && interrupts.every((interrupt) => interrupt.reason === 'tool_call')) {
    const items = approvalItemsFromInterrupts(interrupts)
    if (new Set(items.map((item) => item.interruptId)).size !== items.length) {
      throw new ConversationError('stream_event_invalid')
    }
    return {
      approval: { items, activeIndex: 0, submitted: false, mode: 'options' },
      pendingInteractionKind: 'tool_approval',
    }
  }
  if (pending.length > 1) throw new ConversationError('stream_event_invalid')
  return { pendingInteractionKind: 'input_required' }
}

const traceMessages = (trace: ConversationHistoryCoreDetail): Message[] => {
  const reasoning = new Map(
    trace.reasoning
      .filter((item) => !item.contentOmitted && item.content != null)
      .map((item) => [item.messageId, text(item.content)]),
  )
  const toolResults = new Map(
    trace.messages
      .filter((item) => item.role === 'tool' && item.toolCallId)
      .map((item) => [
        scopedSourceKey(item.graphNamespace, item.toolCallId as string),
        item,
      ]),
  )
  const nodesById = new Map(trace.graph.nodes.map((node) => [node.id, node]))
  const verifiedSubagents = trace.graph.nodes.filter((node) => (
    node.kind === 'subagent' && node.sourceId
  ))
  const subagentPartialOutput = new Map<string, Array<{ sequence: number; content: string; attachments: Attachment[] }>>()
  trace.messages.forEach((message) => {
    if (message.role !== 'assistant' || message.graphNamespace.length === 0) return
    const owner = verifiedSubagents
      .filter((node) => (
        node.graphNamespace.length <= message.graphNamespace.length
        && node.graphNamespace.every((value, position) => value === message.graphNamespace[position])
      ))
      .sort((left, right) => right.graphNamespace.length - left.graphNamespace.length)[0]
    if (!owner) return
    const content = messageText(message.content)
    const attachments = messageAttachments(message.content)
    if (!content && !attachments.length) return
    const existing = subagentPartialOutput.get(owner.id) ?? []
    existing.push({ sequence: message.traceSeq, content, attachments })
    subagentPartialOutput.set(owner.id, existing)
  })
  const owningSubagent = (node: TraceGraphNode): TraceGraphNode | undefined => {
    if (!node.parentSubagentId) return undefined
    const parent = nodesById.get(node.parentSubagentId)
    return parent?.kind === 'subagent' ? parent : undefined
  }
  const ordered: Array<{ value: Message; sequence: number }> = trace.messages.flatMap((item) => {
    if (item.graphNamespace.length > 0) return []
    if (item.role !== 'user' && item.role !== 'assistant') return []
    return [{
      sequence: item.traceSeq,
      value: {
        id: item.agui?.messageId ?? item.id,
        role: item.role,
        content: messageText(item.content),
        attachments: messageAttachments(item.content),
        createdAt: item.createdAt,
        meta: item.role === 'assistant'
          ? {
              status: item.status === 'completed' ? 'completed' : 'running',
              runId: item.runId,
              reasoning: reasoning.get(item.id),
              completedAt: item.completedAt ?? undefined,
              durationMs: elapsedMs(item.createdAt, item.completedAt),
            }
          : { runId: item.runId, contentOmitted: item.contentOmitted, traceMessageId: item.id },
      },
    }]
  })
  trace.graph.nodes.forEach((node) => {
    if (
      node.kind !== 'tool'
      && node.kind !== 'subagent'
      && node.kind !== 'plan'
    ) return
    if (node.kind === 'subagent' && !node.sourceId) return
    const resultNamespace = node.kind === 'subagent'
      ? node.graphNamespace.slice(0, -1)
      : node.graphNamespace
    const result = node.sourceId
      ? toolResults.get(scopedSourceKey(resultNamespace, node.sourceId))
      : undefined
    const delegatedChild = node.agui?.kind === 'tool'
      ? trace.graph.nodes.find(child => child.agui?.kind === 'subagent'
        && node.agui?.kind === 'tool' && child.agui.parentToolCallId === node.agui.toolCallId)
      : undefined
    const retainedInput = node.requestOmitted ? undefined : node.request
    const retainedResult = node.resultOmitted ? undefined : node.result
    const subagent = node.kind === 'tool' ? owningSubagent(node) : undefined
    const subagentInput = isObject(retainedInput)
      && typeof retainedInput.description === 'string'
      ? retainedInput.description
      : text(retainedInput)
    const role: Message['role'] = node.kind === 'tool'
      ? 'tool'
      : node.kind === 'subagent'
        ? 'subagent'
        : 'process'
    ordered.push({
      sequence: node.startedSeq,
      value: {
        id: node.id,
        role,
        content: node.name,
        attachments: [...new Map([
          ...messageAttachments(result?.content ?? retainedResult),
          ...(subagentPartialOutput.get(node.id) ?? []).flatMap(item => item.attachments),
        ].map(attachment => [attachment.id, attachment])).values()],
        createdAt: node.startedAt,
        meta: {
          title: node.name,
          toolName: node.kind === 'tool' ? node.name : undefined,
          graphNamespace: node.graphNamespace,
          agentName: node.kind === 'subagent' ? node.name : undefined,
          sourceAgentName: subagent?.name,
          params: node.kind === 'tool' ? text(retainedInput) : undefined,
          input: node.kind === 'subagent' && !node.requestOmitted ? subagentInput : undefined,
          result: messageText(
            node.kind === 'subagent'
              ? retainedResult
                ?? result?.content
                ?? subagentPartialOutput.get(node.id)
                  ?.sort((left, right) => left.sequence - right.sequence)
                  .map((item) => item.content)
                  .join('\n\n')
              : result?.content ?? retainedResult,
          ),
          status: nodeMessageStatus(node.status),
          toolCallId: node.agui?.kind === 'tool'
            ? node.agui.toolCallId
            : node.agui?.kind === 'subagent' ? node.agui.parentToolCallId : undefined,
          batchId: node.kind === 'tool' && !subagent
            ? node.modelCallId ?? undefined
            : undefined,
          subRunId: node.agui?.kind === 'subagent'
            ? node.agui.subagentInvocationId
            : delegatedChild?.agui?.kind === 'subagent' ? delegatedChild.agui.subagentInvocationId : undefined,
          runId: subagent?.agui?.kind === 'subagent'
            ? subagent.agui.subagentInvocationId
            : node.agui?.kind === 'subagent' ? node.agui.subagentInvocationId : node.runId,
          originMainRunId: node.kind === 'subagent' ? node.runId : undefined,
          lastMainRunId: node.kind === 'subagent' || delegatedChild ? trace.headRunId : undefined,
          completedAt: node.completedAt ?? undefined,
          durationMs: elapsedMs(node.startedAt, node.completedAt),
        },
      },
    })
  })
  return ordered.sort((left, right) => (
    left.sequence - right.sequence
  )).map((item) => item.value)
}

const runStatus = (trace: ConversationHistoryCoreDetail): Conversation['runStatus'] => {
  switch (trace.status.execution) {
    case 'running': return 'detached'
    case 'waiting': return 'waiting_approval'
    case 'failed':
    case 'unknown': return 'error'
    default: return 'idle'
  }
}

const assertTraceDetail = (trace: ConversationHistoryCoreDetail) => {
  if (
    !trace.threadId
    || !trace.generation
    || !trace.headRunId
    || !Number.isSafeInteger(trace.asOfSeq)
    || trace.asOfSeq < 1
    || !Array.isArray(trace.messages)
    || !Array.isArray(trace.graph?.nodes)
    || trace.graph.asOfSeq !== trace.asOfSeq
    || !Array.isArray(trace.interactions)
    || !isObject(trace.state?.root)
  ) throw new ConversationError('stream_event_invalid')
  traceObservationTime(trace.observedAt)
}

const compareTraceObservation = (
  current: ConversationHistoryCoreDetail,
  incoming: Pick<ConversationHistoryCoreDetail,
    'generation' | 'asOfSeq' | 'observedAt' | 'status' | 'completeness' | 'messageCount' | 'toolCallCount' | 'state'>,
): number => {
  if (incoming.generation !== current.generation) throw new ConversationError('stream_event_invalid')
  if (incoming.asOfSeq !== current.asOfSeq) return incoming.asOfSeq - current.asOfSeq
  const incomingTime = traceObservationTime(incoming.observedAt)
  const currentTime = traceObservationTime(current.observedAt)
  if (incomingTime !== currentTime) return incomingTime > currentTime ? 1 : -1
  if (!sameTraceValue(incoming.status, current.status)
    || !sameTraceValue(incoming.completeness, current.completeness)
    || incoming.messageCount !== current.messageCount
    || incoming.toolCallCount !== current.toolCallCount
    || !sameTraceValue(incoming.state, current.state)) {
    throw new ConversationError('stream_event_invalid')
  }
  return 0
}

const sameTraceValue = (left: unknown, right: unknown): boolean => {
  if (left === right) return true
  if (Array.isArray(left) && Array.isArray(right)) {
    return left.length === right.length && left.every((value, index) => sameTraceValue(value, right[index]))
  }
  if (!isObject(left) || !isObject(right)) return false
  const keys = Object.keys(left)
  return keys.length === Object.keys(right).length
    && keys.every((key) => Object.hasOwn(right, key) && sameTraceValue(left[key], right[key]))
}

const assertMatchingTraceEntities = <T extends { id: string }>(current: T[], incoming: T[]) => {
  const previous = new Map(current.map((item) => [item.id, item]))
  for (const item of incoming) {
    const known = previous.get(item.id)
    if (known && !sameTraceValue(known, item)) throw new ConversationError('stream_event_invalid')
  }
}

const taskTraceView = (snapshot: TaskTraceSnapshot): WebTaskTraceViewState => (
  snapshot.status === 'ready'
    ? { phase: 'ready', snapshot }
    : { phase: 'unavailable', snapshot }
)

export const restoreConversationFromTrace = (
  detail: ConversationHistoryDetail,
  options: {
    model: string
    lastDeliveredSeq?: number
    includeTaskTrace: boolean
    taskTrace?: WebTaskTraceViewState
    previous?: Conversation
    expandHistory?: boolean
    preserveHistory?: boolean
  },
): Conversation => {
  const current = options.previous?.trace
  const order = current ? compareTraceObservation(current, detail) : 1
  const preserveHistory = options.preserveHistory && current?.asOfSeq === detail.asOfSeq
    && current?.headRunId === detail.headRunId
  if (current && (order === 0 || preserveHistory) && current.headRunId === detail.headRunId) {
    // 分页可补入同一前缀的历史实体；已存在的实体不能在相同观测内变成不同内容
    assertMatchingTraceEntities(current.messages, detail.messages)
    assertMatchingTraceEntities(current.runFailures.map(item => ({ ...item, id: item.runId })), detail.runFailures.map(item => ({ ...item, id: item.runId })))
    assertMatchingTraceEntities(current.reasoning, detail.reasoning)
    assertMatchingTraceEntities(current.interactions, detail.interactions)
    assertMatchingTraceEntities(current.graph.turns, detail.graph.turns)
    assertMatchingTraceEntities(current.graph.nodes, detail.graph.nodes)
  }
  if (current && preserveHistory && order >= 0) {
    // 同一持久化前缀只重验运行观测，保留用户已经展开的历史窗口
    detail = {
      ...detail,
      messages: current.messages,
      runFailures: current.runFailures,
      reasoning: current.reasoning,
      graph: current.graph,
      interactions: current.interactions,
      historyCursor: current.historyCursor,
    }
  }
  if (current && order < 0) {
    if (!options.expandHistory || detail.asOfSeq !== current.asOfSeq
      || detail.headRunId !== current.headRunId) {
      const previous = options.previous!
      const title = mergeConversationTitle(previous, detail)
      return title.titleSeq === previous.titleSeq ? previous : { ...previous, ...title }
    }
    // 固定前缀的旧分页补充历史实体，运行状态仍使用更新的存储观测
    detail = {
      ...detail,
      observedAt: current.observedAt,
      status: current.status,
      completeness: current.completeness,
    }
  }
  const { taskTrace: wireTaskTrace, ...wireCore } = detail
  const trace = structuredClone(wireCore)
  assertTraceDetail(trace)
  if (options.includeTaskTrace && wireTaskTrace == null) {
    throw new ConversationError('stream_event_invalid')
  }
  const taskTrace = options.includeTaskTrace && wireTaskTrace != null
    ? taskTraceView(wireTaskTrace)
    : options.taskTrace ?? { phase: 'unloaded' as const }
  const interaction = interactionState(trace.interactions)
  const projectedStatus = runStatus(trace)
  const status = interaction.pendingInteractionKind && projectedStatus !== 'error'
    ? 'waiting_approval'
    : projectedStatus
  // 权威历史更新正文和终态时，保留当前页面尚未显示完的实时文字进度
  const liveTextById = new Map(options.previous?.messages
    .filter(message => message.role === 'assistant' && message.liveText)
    .map(message => [message.id, message.liveText]))
  const messages = traceMessages(trace).map(message => {
    const liveText = message.role === 'assistant' ? liveTextById.get(message.id) : undefined
    return liveText ? { ...message, liveText } : message
  })

  return {
    threadId: trace.threadId,
    ...mergeConversationTitle(options.previous, trace),
    pinned: trace.pinned,
    updatedAt: trace.updatedAt,
    model: trace.lastModel ?? options.model,
    accessMode: trace.accessMode,
    mode: modeFromState(trace.state.root),
    messages,
    runFailures: parseRunFailures(trace.runFailures),
    todos: todosFromState(trace.state.root),
    taskTrace,
    approval: interaction.approval,
    planInteraction: interaction.planInteraction,
    pendingInteractionKind: interaction.pendingInteractionKind,
    runStatus: status,
    activeRunId: status === 'detached' ? trace.headRunId : undefined,
    serverState: structuredClone(trace.state.root),
    // Trace 序号与 Messaging 投递序号相互独立；实时调用方保留已知游标，纯历史水化保持未知
    lastSeq: options.lastDeliveredSeq,
    trace,
    isHydrated: true,
  }
}

export const applyConversationTraceUpdate = (
  conversation: Conversation,
  update: ConversationTraceUpdate,
  taskTraceReplacement: TaskTraceSnapshot | null,
  includeTaskTrace: boolean,
): Conversation => {
  const previous = conversation.trace
  if (!previous) throw new ConversationError('stream_event_invalid')
  if (compareTraceObservation(previous, update) < 0) return conversation
  if (update.asOfSeq === previous.asOfSeq) {
    if (update.events.length || update.facts.length) return conversation
    if (!sameTraceValue(update.runFailures, previous.runFailures)
      || update.messages.upserts.length || update.messages.removes.length
      || update.reasoning.upserts.length || update.reasoning.removes.length
      || update.interactions.upserts.length || update.interactions.removes.length
      || update.status.headRunId !== previous.headRunId
      || update.messageCount !== previous.messageCount
      || update.toolCallCount !== previous.toolCallCount
      || JSON.stringify(update.state) !== JSON.stringify(previous.state)) {
      throw new ConversationError('stream_event_invalid')
    }
  }
  if (update.graph.asOfSeq !== update.asOfSeq) {
    throw new ConversationError('stream_event_invalid')
  }
  const next: ConversationHistoryCoreDetail = {
    ...structuredClone(previous),
    asOfSeq: update.asOfSeq,
    generation: update.generation,
    observedAt: update.observedAt,
    headRunId: update.status.headRunId,
    messages: applyEntityDelta(previous.messages, update.messages.upserts, update.messages.removes),
    runFailures: parseRunFailures(update.runFailures),
    reasoning: applyEntityDelta(previous.reasoning, update.reasoning.upserts, update.reasoning.removes),
    graph: applyTraceGraphDelta(previous.graph, update.graph),
    interactions: applyEntityDelta(
      previous.interactions,
      update.interactions.upserts,
      update.interactions.removes,
    ),
    state: structuredClone(update.state),
    status: structuredClone(update.status),
    completeness: structuredClone(update.completeness),
    messageCount: update.messageCount,
    toolCallCount: update.toolCallCount,
    historyCursor: update.asOfSeq === previous.asOfSeq ? previous.historyCursor : null,
  }
  const taskTrace = includeTaskTrace && taskTraceReplacement != null
    ? taskTraceView(taskTraceReplacement)
    : includeTaskTrace
      ? conversation.taskTrace
      : { phase: 'unloaded' as const }
  return restoreConversationFromTrace({ ...next, taskTrace: null }, {
    model: conversation.model,
    lastDeliveredSeq: conversation.lastSeq,
    includeTaskTrace: false,
    taskTrace,
  })
}
