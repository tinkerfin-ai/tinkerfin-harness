import { mergeConversationTitle } from "../../../lib/workspace"
import { translateCurrent } from '../../../i18n'
import { compactionsFromTrace } from '../compaction/state'
import { messageAttachments, messageText, type Attachment } from '../attachments/content'
import type {
  ConversationHistoryCoreDetail,
  ConversationHistoryDetail,
  ConversationTraceUpdate,
  InteractionAvailability,
  TraceInteraction,
} from '../../../api/conversation/history'
import { parseInteractionAvailability, parseRunFailures, traceObservationTime } from '../../../api/conversation/history'
import {
  parseConversationGraph,
  type ConversationGraph,
  type ConversationGraphDelta,
  type ConversationGraphNode,
} from '../../../api/conversation/historyGraph'
import type { TaskTraceSnapshot } from '../../../api/conversation/taskTrace'
import { ConversationError } from '../../../api/conversation/errors'
import type {
  ApprovalState,
  Conversation,
  DeepReadonly,
  JsonObject,
  JsonValue,
  Message,
  PendingInteractionKind,
  TodoItem,
  WebTaskTraceViewState,
} from '../../../types'
import { approvalItemsFromInterrupts, planInteractionFromInterrupts } from '../agui'

type TraceSnapshot = DeepReadonly<ConversationHistoryCoreDetail>
type TraceNode = DeepReadonly<ConversationGraphNode>

const isObject = (value: unknown): value is DeepReadonly<JsonObject> => (
  value != null && typeof value === 'object' && !Array.isArray(value)
)

const text = (value: DeepReadonly<JsonValue> | null | undefined): string => {
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
  status: ConversationGraphNode['status'],
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

const todosFromState = (root: DeepReadonly<JsonObject>): TodoItem[] => {
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

const modeFromState = (root: DeepReadonly<JsonObject>): Conversation['mode'] => {
  const plan = root.tinkerfin_plan
  return isObject(plan) && plan.effectiveMode === 'plan' ? 'plan' : 'default'
}

const applyEntityDelta = <T extends { id: string }>(
  current: readonly T[],
  upserts: readonly T[],
  removes: readonly string[],
): readonly T[] => {
  if (!upserts.length && !removes.length) return current
  const values = new Map(current.map((item) => [item.id, item]))
  for (const id of removes) values.delete(id)
  for (const item of upserts) values.set(item.id, structuredClone(item))
  return [...values.values()]
}

const applyTraceGraphDelta = (
  current: DeepReadonly<ConversationGraph>,
  delta: ConversationGraphDelta,
): DeepReadonly<ConversationGraph> => {
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
  const turns = [...applyEntityDelta(
    current.turns,
    delta.turnUpserts,
    delta.turnRemoves,
  )].sort((left, right) => left.ordinal - right.ordinal || left.id.localeCompare(right.id))
  return parseConversationGraph({
    turns,
    nodes: delta.orderedNodeIds.map((id) => nodesById.get(id)),
    orderedNodeIds: [...delta.orderedNodeIds],
    matchedNodeIds: [...delta.matchedNodeIds],
    asOfSeq: delta.asOfSeq,
    completeness: { ...delta.completeness },
  })
}

const scopedSourceKey = (graphNamespace: readonly string[], sourceId: string): string => (
  JSON.stringify([graphNamespace, sourceId])
)

const interactionState = (
  interactions: readonly DeepReadonly<TraceInteraction>[],
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

const actionableInteraction = (
  trace: TraceSnapshot,
  previous?: Conversation,
): ReturnType<typeof interactionState> => {
  const projected = interactionState(trace.interactions)
  const permissions = new Map(parseInteractionAvailability(trace.interactionAvailability)
    .map(item => [item.interruptId, item]))
  const groupIds = (value: ReturnType<typeof interactionState>): string[] => value.approval
    ? value.approval.items.map(item => item.interruptId)
    : value.planInteraction ? [value.planInteraction.interruptId] : []
  const previousIds = previous ? groupIds(previous) : []
  const incomingIds = groupIds(projected)
  const previousForm = previous?.approval ?? previous?.planInteraction
  const matchesSubmission = (values: Array<InteractionAvailability | undefined>) => (
    values.length > 0 && values.every(item => item
      && (!previousForm?.submitted || previousForm.submissionRunId == null
        || item.submissionRunId === previousForm.submissionRunId))
  )
  const previousPermissions = previousIds.map(id => permissions.get(id))
  const settled = matchesSubmission(previousPermissions)
    && previousPermissions.every(item => item?.state === 'resolved' || item?.state === 'cancelled')
  const sameGroup = previousIds.length > 0 && previousIds.length === incomingIds.length
    && previousIds.every(id => incomingIds.includes(id))
  const incomplete = trace.interactions.some(item => item.status === 'pending' && item.agui === null)
  const retained = previous && !settled && (sameGroup || previousForm?.submitted || incomplete)
    ? { approval: previous.approval, planInteraction: previous.planInteraction,
        pendingInteractionKind: previous.pendingInteractionKind ?? projected.pendingInteractionKind }
    : projected
  const ids = groupIds(retained)
  if (!ids.length) return retained
  const values = ids.map(id => permissions.get(id))
  const matches = retained === projected && !sameGroup ? values.every(Boolean) : matchesSubmission(values)
  if (matches && values.every(item => item?.state === 'resolved' || item?.state === 'cancelled')) return {}
  const submitted = !(matches && values.every(item => item?.state === 'available'))
  const owners = new Set(values.map(item => item?.submissionRunId).filter((id): id is string => !!id))
  const submissionRunId = submitted
    ? (!settled ? previousForm?.submissionRunId : undefined)
      ?? (owners.size === 1 ? [...owners][0] : undefined)
    : undefined
  return {
    ...retained,
    approval: retained.approval ? { ...retained.approval, submitted, submissionRunId } : undefined,
    planInteraction: retained.planInteraction ? { ...retained.planInteraction, submitted, submissionRunId } : undefined,
  }
}

const submissionNotice = (
  interaction: ReturnType<typeof interactionState>,
  headRunId: string,
  status: Conversation['runStatus'],
): Conversation['notice'] => (
  (interaction.approval?.submitted || interaction.planInteraction?.submitted)
  && status !== 'streaming' && status !== 'detached'
    ? { id: `${headRunId}:interaction-confirmation`, kind: 'info',
        content: translateCurrent('提交状态尚未确认，请重新加载'), recovery: 'history' }
    : undefined
)

const retainConfirmedPlanHistory = (
  messages: Message[],
  trace: TraceSnapshot,
  previous?: Conversation,
): Message[] => {
  if (!previous) return messages
  const visibleRuns = new Set(trace.messages.map(item => item.runId))
  const retained = previous.messages.filter(item => item.meta?.planHistory
    && item.meta.runId && visibleRuns.has(item.meta.runId))
  const plan = previous.planInteraction
  const confirmation = plan?.submitted && plan.submissionRunId
    ? trace.interactionAvailability.find(item => item.interruptId === plan.interruptId
      && item.submissionRunId === plan.submissionRunId
      && (item.state === 'resolved' || item.state === 'cancelled'))
    : undefined
  const origin = plan && previous.trace?.interactions.find(item => item.agui?.some(action => action.id === plan.interruptId))
  if (plan && confirmation && origin) retained.push({
    id: `plan-history:${plan.interruptId}`, role: 'process', content: '', createdAt: origin.openedAt,
    meta: { planHistory: structuredClone(plan), status: confirmation.state === 'cancelled' ? 'cancelled' : 'completed', runId: origin.runId },
  })
  const existing = new Set(messages.map(item => item.id))
  const localHistory = new Map(retained.map(item => [item.id, item]))
  const restored = messages.map(item => {
    const local = localHistory.get(item.id)
    return item.meta?.planHistory && local?.meta?.planHistory
      && item.meta.runId === local.meta.runId
      ? { ...item, meta: { ...item.meta, planHistory: local.meta.planHistory } }
      : item
  })
  const additions = retained.filter(item => {
    if (existing.has(item.id)) return false
    existing.add(item.id)
    return true
  })
  return additions.length ? [...restored, ...additions]
    : restored.some((item, index) => item !== messages[index]) ? restored : messages
}

const traceMessages = (trace: TraceSnapshot): Message[] => {
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
  const owningSubagent = (node: TraceNode): TraceNode | undefined => {
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
        role: item.role === 'user' && item.source?.kind === 'context' ? 'context' : item.role,
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
          : { runId: item.runId, contentOmitted: item.contentOmitted, traceMessageId: item.id, source: item.source ?? undefined },
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
          graphNamespace: [...node.graphNamespace],
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
  for (const interaction of trace.interactions) {
    if (interaction.status === 'pending' || !interaction.agui) continue
    const plan = planInteractionFromInterrupts(interaction.agui)
    if (!plan) continue
    ordered.push({ sequence: interaction.traceSeq, value: {
      id: `plan-history:${plan.interruptId}`, role: 'process', content: '',
      createdAt: interaction.openedAt,
      meta: { planHistory: plan, status: 'completed', runId: interaction.runId },
    } })
  }
  return ordered.sort((left, right) => (
    left.sequence - right.sequence
  )).map((item) => item.value)
}

const runStatus = (trace: TraceSnapshot): Conversation['runStatus'] => {
  switch (trace.status.execution) {
    case 'running': return 'detached'
    case 'waiting': return 'waiting_approval'
    case 'failed':
    case 'unknown': return 'error'
    default: return 'idle'
  }
}

const assertTraceDetail = (trace: TraceSnapshot) => {
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
  current: TraceSnapshot,
  incoming: Pick<TraceSnapshot,
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

const assertMatchingTraceEntities = <T extends { id: string }>(current: readonly T[], incoming: readonly T[]) => {
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

interface TraceViewOptions {
  model: string
  lastDeliveredSeq?: number
  previous?: Conversation
  preserveInputFrom?: Conversation
}

export const restoreConversationFromTrace = (
  detail: ConversationHistoryDetail,
  options: TraceViewOptions & {
    includeTaskTrace: boolean
    taskTrace?: WebTaskTraceViewState
    expandHistory?: boolean
    preserveHistory?: boolean
  },
): Conversation => {
  const current = options.previous?.trace
  const order = current ? compareTraceObservation(current, detail) : 1
  const settled = new Map(current?.interactionAvailability
    .filter(item => item.state === 'resolved' || item.state === 'cancelled')
    .map(item => [item.interruptId, item]))
  const availability = parseInteractionAvailability(detail.interactionAvailability).map(item => {
    const known = settled.get(item.interruptId)
    if (!known) return item
    if ((item.state === 'resolved' || item.state === 'cancelled')
      && (item.state !== known.state || item.submissionRunId !== known.submissionRunId)) {
      throw new ConversationError('stream_event_invalid')
    }
    return { ...known }
  })
  const presentIds = new Set(availability.map(item => item.interruptId))
  const pending = detail.interactions.filter(item => item.status === 'pending')
  const pendingIds = new Set(pending.flatMap(item => item.agui?.map(action => action.id) ?? []))
  const retainedIds = new Set([
    ...(options.previous?.approval?.submitted ? options.previous.approval.items.map(item => item.interruptId) : []),
    ...(options.previous?.planInteraction?.submitted ? [options.previous.planInteraction.interruptId] : []),
  ])
  const completeNewPrefix = current && detail.generation === current.generation && detail.asOfSeq > current.asOfSeq
    && pending.every(item => item.agui !== null)
  for (const known of settled.values()) {
    if (!presentIds.has(known.interruptId)
      && (!completeNewPrefix || pendingIds.has(known.interruptId)
        || retainedIds.has(known.interruptId))) availability.push({ ...known })
  }
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
  if (current && order < 0) {
    if (!options.expandHistory || detail.asOfSeq !== current.asOfSeq
      || detail.headRunId !== current.headRunId) {
      const previous = options.previous!
      const title = mergeConversationTitle(previous, detail)
      if (!previous.approval && !previous.planInteraction) {
        return title.titleSeq === previous.titleSeq ? previous : { ...previous, ...title }
      }
      const trace = { ...current, interactionAvailability: availability }
      const interaction = actionableInteraction(trace, previous)
      return {
        ...previous, ...title, trace,
        approval: interaction.approval,
        planInteraction: interaction.planInteraction,
        pendingInteractionKind: interaction.pendingInteractionKind,
        messages: retainConfirmedPlanHistory(previous.messages, trace, previous),
        notice: submissionNotice(interaction, current.headRunId, previous.runStatus)
          ?? (previous.notice?.recovery === 'history' ? undefined : previous.notice),
      }
    }
  }
  const { taskTrace: wireTaskTrace, ...wireCore } = detail
  // 新输入只复制一次；同前缀刷新仅复制新观测，已展开的只读实体继续共享
  let trace: TraceSnapshot = current && preserveHistory && order >= 0
    ? {
        ...wireCore,
        availableHeads: [...wireCore.availableHeads],
        state: structuredClone(wireCore.state),
        status: { ...wireCore.status },
        completeness: { ...wireCore.completeness },
        messages: current.messages,
        runFailures: current.runFailures,
        reasoning: current.reasoning,
        graph: current.graph,
        interactions: current.interactions,
        historyCursor: current.historyCursor,
      }
    : structuredClone(wireCore)
  if (current && order < 0) {
    // 固定前缀的旧分页补充历史实体，运行状态仍使用更新的存储观测
    trace = {
      ...trace,
      observedAt: current.observedAt,
      status: current.status,
      completeness: current.completeness,
    }
  }
  trace = { ...trace, interactionAvailability: availability }
  if (options.includeTaskTrace && wireTaskTrace == null) {
    throw new ConversationError('stream_event_invalid')
  }
  const taskTrace = options.includeTaskTrace && wireTaskTrace != null
    ? taskTraceView(wireTaskTrace)
    : options.taskTrace ?? { phase: 'unloaded' as const }
  return projectTraceConversation(trace, taskTrace, options)
}

/** 快照已归属会话且只读；消息和交互表单另建可编辑视图 */
const projectTraceConversation = (
  trace: TraceSnapshot,
  taskTrace: WebTaskTraceViewState,
  options: TraceViewOptions,
): Conversation => {
  assertTraceDetail(trace)
  const interaction = actionableInteraction(trace, options.previous ?? options.preserveInputFrom)
  const projectedStatus = runStatus(trace)
  const confirming = Boolean(interaction.approval?.submitted || interaction.planInteraction?.submitted)
  const status = interaction.pendingInteractionKind && projectedStatus !== 'error'
    && !(confirming && projectedStatus === 'detached')
    ? 'waiting_approval'
    : projectedStatus
  // 取消的当前轮直接显示完整正文，其他轮次保留尚未显示完的实时文字进度
  const liveTextById = new Map(options.previous?.messages
    .filter(message => message.role === 'assistant' && message.liveText)
    .map(message => [message.id, message.liveText]))
  const messages = traceMessages(trace).map(message => {
    if (trace.status.execution === 'cancelled' && message.meta?.runId === trace.headRunId) return message
    const liveText = message.role === 'assistant' ? liveTextById.get(message.id) : undefined
    return liveText ? { ...message, liveText } : message
  })

  return {
    projectId: trace.projectId,
    archived: trace.archived,
    threadId: trace.threadId,
    ...mergeConversationTitle(options.previous, trace),
    pinned: trace.pinned,
    updatedAt: trace.updatedAt,
    model: trace.lastModel ?? options.model,
    accessMode: trace.accessMode,
    mode: modeFromState(trace.state.root),
    messages: retainConfirmedPlanHistory(messages, trace, options.previous ?? options.preserveInputFrom),
    compactions: compactionsFromTrace(trace.graph.nodes, messages),
    runFailures: parseRunFailures(trace.runFailures),
    todos: todosFromState(trace.state.root),
    taskTrace,
    approval: interaction.approval,
    planInteraction: interaction.planInteraction,
    pendingInteractionKind: interaction.pendingInteractionKind,
    notice: submissionNotice(interaction, trace.headRunId, status),
    runStatus: status,
    activeRunId: status === 'detached' ? trace.headRunId : undefined,
    serverState: trace.state.root,
    // Trace 序号与 Messaging 投递序号相互独立；实时调用方保留已知游标，纯历史水化保持未知
    lastSeq: options.lastDeliveredSeq,
    trace,
    isHydrated: true,
    historySynchronized: true,
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
    if (update.hasEvents) return conversation
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
  const next: TraceSnapshot = {
    ...previous,
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
    status: { ...update.status },
    completeness: { ...update.completeness },
    messageCount: update.messageCount,
    toolCallCount: update.toolCallCount,
    historyCursor: update.asOfSeq === previous.asOfSeq ? previous.historyCursor : null,
  }
  const taskTrace = includeTaskTrace && taskTraceReplacement != null
    ? taskTraceView(taskTraceReplacement)
    : includeTaskTrace
      ? conversation.taskTrace
      : { phase: 'unloaded' as const }
  return projectTraceConversation(next, taskTrace, {
    model: conversation.model,
    lastDeliveredSeq: conversation.lastSeq,
    previous: conversation,
  })
}
