import { ConversationError } from '../../../api/conversation/errors'
import type {
  ConversationHistoryCoreDetail,
  ConversationHistoryDetail,
  TraceInteraction,
} from '../../../api/conversation/history'
import { parseInteractionAvailability, parseRunFailures, parseSubmissionResult, traceObservationTime } from '../../../api/conversation/history'
import {
  type ConversationGraphNode,
} from '../../../api/conversation/historyGraph'
import { parsePlanResults } from '../../../api/conversation/planResults'
import type { TaskTraceSnapshot } from '../../../api/conversation/taskTrace'
import { translateCurrent } from '../../../i18n'
import { mergeConversationTitle } from "../../../lib/workspace"
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
import { messageAttachments, messageText, type Attachment } from '../attachments/content'
import { compactionsFromTrace } from '../compaction/state'
import { interactionInterruptIds, resolveInteractionSubmission, upsertConfirmedPlanHistory } from '../interactionConfirmation'

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
  const permissions = parseInteractionAvailability(trace.interactionAvailability)
  const previousIds = previous ? interactionInterruptIds(previous) : []
  const incomingIds = interactionInterruptIds(projected)
  const previousForm = previous?.approval ?? previous?.planInteraction
  const previousResolution = previous
    ? resolveInteractionSubmission(previous, permissions, trace.submissionResult) : undefined
  const settled = previousResolution?.state === 'settled'
  const sameGroup = previousIds.length > 0 && previousIds.length === incomingIds.length
    && previousIds.every(id => incomingIds.includes(id))
  const incomplete = trace.interactions.some(item => item.status === 'pending' && item.agui === null)
  const retained = previous && !settled && (sameGroup || (previousForm?.submitted && !previousResolution?.notSaved) || incomplete)
    ? { approval: previous.approval, planInteraction: previous.planInteraction,
        pendingInteractionKind: previous.pendingInteractionKind ?? projected.pendingInteractionKind }
    : projected
  return resolveInteractionSubmission(retained, permissions, trace.submissionResult).view
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
  const results = new Map(parsePlanResults(trace.planResults).map(result => [result.interruptId, result]))
  const visibleRuns = new Set(trace.messages.map(item => item.runId))
  const retained = previous.messages.filter(item => item.meta?.planHistory
    && item.meta.runId && visibleRuns.has(item.meta.runId))
  const plan = previous.planInteraction
  const settlement = resolveInteractionSubmission(previous, trace.interactionAvailability, trace.submissionResult)
  const confirmation = plan?.submitted && settlement.state === 'settled'
    ? trace.interactionAvailability.find(item => item.interruptId === plan.interruptId
      && item.submissionRunId === settlement.submissionRunId
      && (item.state === 'resolved' || item.state === 'cancelled'))
    : undefined
  const origin = plan && previous.trace?.interactions.find(item => item.agui?.some(action => action.id === plan.interruptId))
  if (plan && confirmation && origin) retained.push({
    id: `plan-history:${plan.interruptId}`, role: 'process', content: '', createdAt: origin.openedAt,
    meta: { planHistory: structuredClone(plan), planResult: results.get(plan.interruptId), status: confirmation.state === 'cancelled' ? 'cancelled' : 'completed', runId: origin.runId },
  })
  const existing = new Set(messages.map(item => item.id))
  const localHistory = new Map(retained.map(item => [item.id, item]))
  const restored = messages.map(item => {
    const local = localHistory.get(item.id)
    return item.meta?.planHistory && local?.meta?.planHistory
      && item.meta.runId === local.meta.runId
      ? { ...item, meta: { ...item.meta, planHistory: local.meta.planHistory, planResult: item.meta.planResult ?? local.meta.planResult } }
      : item
  })
  const additions = retained.filter(item => {
    if (existing.has(item.id)) return false
    existing.add(item.id)
    return true
  })
  return additions.length ? additions.reduce((entries, item) => upsertConfirmedPlanHistory(
    entries, item, item.meta?.planResult?.submissionRunId ?? item.meta?.planHistory?.submissionRunId,
  ), restored)
    : restored.some((item, index) => item !== messages[index]) ? restored : messages
}

const traceMessages = (trace: TraceSnapshot): Message[] => {
  const planResults = new Map(parsePlanResults(trace.planResults).map(result => [result.interruptId, result]))
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
    if (!interaction.agui) continue
    const plan = planInteractionFromInterrupts(interaction.agui)
    if (!plan) continue
    const result = planResults.get(plan.interruptId)
    if (interaction.status === 'pending' && !result) continue
    ordered.push({ sequence: interaction.traceSeq, value: {
      id: `plan-history:${plan.interruptId}`, role: 'process', content: '',
      createdAt: interaction.openedAt,
      meta: { planHistory: plan, planResult: result, status: interaction.status === 'cancelled' || result?.outcome === 'cancelled' ? 'cancelled' : 'completed', runId: interaction.runId },
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
  if ((options.previous && options.previous.threadId !== detail.threadId)
    || (options.preserveInputFrom && options.preserveInputFrom.threadId !== detail.threadId)) {
    throw new ConversationError('stream_event_invalid')
  }
  const submissionResult = parseSubmissionResult(detail.submissionResult)
  const current = options.previous?.trace
  const inputOwner = options.previous ?? options.preserveInputFrom
  const order = current ? compareTraceObservation(current, detail) : 1
  const settled = new Map(inputOwner?.trace?.interactionAvailability
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
    ...(inputOwner?.approval?.submitted ? inputOwner.approval.items.map(item => item.interruptId) : []),
    ...(inputOwner?.planInteraction?.submitted ? [inputOwner.planInteraction.interruptId] : []),
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
      // 认领与答案来自独立业务结算，即使 Trace 前缀较旧也必须一起收束
      const results = new Map(current.planResults.map(item => [item.interruptId, item]))
      for (const result of parsePlanResults(detail.planResults)) results.set(result.interruptId, result)
      const trace = { ...current, interactionAvailability: availability, submissionResult, planResults: [...results.values()] }
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
  trace = { ...trace, interactionAvailability: availability, submissionResult }
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
