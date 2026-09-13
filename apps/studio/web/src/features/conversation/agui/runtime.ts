import { isConversationTitle } from '../../../api/conversation/titles'
import { mergeConversationTitle } from "../../../lib/workspace"
import { attachmentInput, isAttachment, messageText, messageAttachments, type Attachment } from '../attachments/content'
import type {
  AgentMode,
  ChatMessageInput,
  ChatRequestPayload,
  ChatResumeEntry,
  ConversationAgUiEvent,
  InterruptEvent,
  PlanClarificationAnswer,
  RawEventContext,
} from "../../../api/conversation/types"
import {
  ConversationError,
  conversationErrorMessage,
} from "../../../api/conversation/errors"
import type {
  ApprovalAllowedDecision,
  ApprovalItem,
  ApprovalState,
  Conversation,
  ConversationNotice,
  JsonObject,
  JsonValue,
  MarkdownPlanDraft,
  Message,
  PlanQuestionItem,
  PlanReviewState,
  TodoItem,
  TodoStatus,
} from "../../../types"
import { translateCurrent } from "../../../i18n"
import { parseToolReviewInterrupt } from "./toolReviewContract"
import { applyStateDelta } from "./jsonPatch"
import {
  parseSubagentProvenance,
  type SubagentProvenance,
} from "./subagentProvenanceContract"

export const createRunId = () =>
  `run-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`

const nowIso = () => new Date().toISOString()

const elapsedMs = (startedAt: string, completedAt: string) => {
  const elapsed = Date.parse(completedAt) - Date.parse(startedAt)
  return Number.isFinite(elapsed) ? Math.max(0, elapsed) : 0
}

const parseJsonObject = (value: string) => {
  try {
    const parsed = JSON.parse(value) as JsonValue
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? parsed as JsonObject
      : null
  } catch {
    return null
  }
}

const isStateTodo = (value: JsonValue): value is { content: string; status: "pending" | "in_progress" | "completed" } =>
  Boolean(
    value
    && typeof value === "object"
    && !Array.isArray(value)
    && typeof (value as { content?: unknown }).content === "string"
    && (
      (value as { status?: unknown }).status === "pending"
      || (value as { status?: unknown }).status === "in_progress"
      || (value as { status?: unknown }).status === "completed"
    ),
  )

const toTodoStatus = (status: "pending" | "in_progress" | "completed"): TodoStatus =>
  status === "in_progress" ? "running" : status

const findMessageIndex = (
  conversation: Conversation,
  predicate: (message: Message) => boolean,
) => conversation.messages.findIndex(predicate)

const updateMessage = (
  conversation: Conversation,
  predicate: (message: Message) => boolean,
  updater: (message: Message) => Message,
): Conversation => ({
  ...conversation,
  messages: conversation.messages.map((message) => (predicate(message) ? updater(message) : message)),
})

const setConversationNotice = (
  conversation: Conversation,
  content: string,
  kind: ConversationNotice["kind"],
  id?: string,
): Conversation => ({
  ...conversation,
  notice: { kind, content, id },
})

const markInterruptedToolCards = (
  conversation: Conversation,
  interruptedRunId: string,
  interrupts: InterruptEvent[],
): Conversation => ({
  ...conversation,
  messages: conversation.messages.map((message) => {
    if (
      message.role !== "tool"
      || message.meta?.status !== "running"
      || message.meta.runId !== interruptedRunId
    ) return message
    const interrupt = interrupts.find((item) => item.toolCallId === message.meta?.toolCallId)
    return {
      ...message,
      meta: {
        ...message.meta,
        status: "paused",
        interruptId: interrupt?.id,
      },
    }
  }),
})

const approvalInputFromArgs = (args: JsonObject, fallback: string | undefined) => {
  const filePath = args.file_path
  if (typeof filePath === "string" && filePath) return filePath
  return fallback ?? ""
}

export const approvalItemsFromInterrupts = (interrupts: InterruptEvent[]): ApprovalItem[] =>
  interrupts.map((interrupt) => {
    const review = parseToolReviewInterrupt(interrupt)
    const originalArgs = review.originalArgs
    const allowedDecisions: ApprovalAllowedDecision[] = [...review.allowedDecisions]
    const toolName = review.toolName

    return {
      id: interrupt.id,
      interruptId: interrupt.id,
      toolCallId: interrupt.toolCallId ?? undefined,
      toolName,
      params: JSON.stringify(originalArgs, null, 2),
      input: approvalInputFromArgs(originalArgs, interrupt.message ?? undefined),
      description: interrupt.message ?? "",
      originalArgs,
      allowedDecisions,
    }
  })

interface PlanInterruptLike {
  id: string
  reason: string
  message?: string | null
  responseSchema?: JsonObject | null
  metadata?: JsonObject | null
}

const jsonValuesEqual = (left: unknown, right: unknown): boolean => {
  if (left === null || right === null || typeof left !== 'object' || typeof right !== 'object') {
    return Object.is(left, right)
  }
  if (Array.isArray(left) || Array.isArray(right)) {
    return Array.isArray(left)
      && Array.isArray(right)
      && left.length === right.length
      && left.every((item, index) => jsonValuesEqual(item, right[index]))
  }
  const leftObject = left as Record<string, unknown>
  const rightObject = right as Record<string, unknown>
  const leftKeys = Object.keys(leftObject)
  const rightKeys = Object.keys(rightObject)
  return leftKeys.length === rightKeys.length
    && leftKeys.every((key) => Object.hasOwn(rightObject, key)
      && jsonValuesEqual(leftObject[key], rightObject[key]))
}

const runtimeEnvelopeMetadata = (interrupt: PlanInterruptLike): JsonObject | null => {
  const runtimeInterrupt = interrupt.metadata?.runtimeInterrupt
  if (!isJsonObject(runtimeInterrupt)) return null
  if (
    runtimeInterrupt.schema !== 'tinkerfin.runtime-interrupt'
    || runtimeInterrupt.nativeInterruptId !== interrupt.id
  ) return null
  const envelope = runtimeInterrupt.envelope
  if (!isJsonObject(envelope)) return null
  if (
    envelope.schema !== 'tinkerfin.runtime-interrupt'
    || envelope.kind !== interrupt.reason
    || !isJsonObject(envelope.responseSchema)
    || !isJsonObject(interrupt.responseSchema)
    || !jsonValuesEqual(envelope.responseSchema, interrupt.responseSchema)
  ) return null
  const metadata = envelope.metadata
  return isJsonObject(metadata) ? metadata : null
}

const isStringWithinLength = (value: unknown, maximum: number): value is string => {
  if (typeof value !== 'string') return false
  const length = Array.from(value).length
  return length >= 1 && length <= maximum
}

const invalidPlanInteraction = (): never => {
  throw new Error('Plan interrupt 载荷不符合 Studio 契约')
}

const isJsonObject = (value: unknown): value is JsonObject => (
  Boolean(value) && typeof value === 'object' && !Array.isArray(value)
)

const hasOnlyKeys = (value: JsonObject, keys: readonly string[]) => {
  const actual = Object.keys(value).sort()
  const expected = [...keys].sort()
  return actual.length === expected.length
    && actual.every((key, index) => key === expected[index])
}

const CLARIFICATION_ID = /^[A-Za-z0-9][A-Za-z0-9._-]*$/
const isClarificationId = (value: unknown): value is string => (
  typeof value === 'string' && CLARIFICATION_ID.test(value)
)
const isNonBlankString = (value: unknown): value is string => (
  typeof value === 'string' && value.trim().length > 0
)

const parsePlanQuestionOptions = (value: unknown) => {
  if (!Array.isArray(value)) return null
  const options = value.flatMap((rawOption) => {
    if (!isJsonObject(rawOption)) return []
    if (!isClarificationId(rawOption.id) || !isNonBlankString(rawOption.label)) return []
    if (
      rawOption.description !== null
      && rawOption.description !== undefined
      && !isNonBlankString(rawOption.description)
    ) return []
    const attributes = rawOption.attributes
    if (!isJsonObject(attributes) || typeof attributes.recommended !== 'boolean') return []
    return [{
      id: rawOption.id,
      label: rawOption.label,
      description: typeof rawOption.description === 'string' ? rawOption.description : null,
      recommended: attributes.recommended,
      attributes,
    }]
  })
  return options.length === value.length
    && new Set(options.map((option) => option.id)).size === options.length
    ? options
    : null
}

const normalizeMinuteTime = (value: unknown): string | null => {
  if (typeof value !== 'string') return null
  const match = /^([01]\d|2[0-3]):([0-5]\d)(?::00)?$/.exec(value)
  return match ? `${match[1]}:${match[2]}` : null
}

const normalizeMinuteDateTime = (value: unknown): string | null => {
  if (typeof value !== 'string') return null
  const match = /^(\d{4}-\d{2}-\d{2})T([01]\d|2[0-3]):([0-5]\d)(?::00)?$/.exec(value)
  return match && isIsoCalendarDate(match[1])
    ? `${match[1]}T${match[2]}:${match[3]}`
    : null
}

const parsePlanQuestions = (value: unknown): PlanQuestionItem[] | null => {
  if (!Array.isArray(value)) return null
  const questions = value.flatMap<PlanQuestionItem>((rawQuestion) => {
    if (!isJsonObject(rawQuestion)) return []
    if (
      !isClarificationId(rawQuestion.id)
      || !isNonBlankString(rawQuestion.prompt)
      || typeof rawQuestion.required !== 'boolean'
      || typeof rawQuestion.answerType !== 'string'
    ) return []
    const attributes = rawQuestion.attributes
    if (attributes !== undefined && attributes !== null && !isJsonObject(attributes)) return []
    const common = {
      id: rawQuestion.id,
      prompt: rawQuestion.prompt,
      required: rawQuestion.required,
      attributes: attributes as JsonObject | null | undefined,
    }
    if (rawQuestion.answerType === 'single_choice') {
      const options = parsePlanQuestionOptions(rawQuestion.options)
      if (
        !options
        || options.length === 0
        || typeof rawQuestion.allowFreeText !== 'boolean'
        || !options[0]?.recommended
        || options.slice(1).some((option) => option.recommended)
      ) return []
      return [{
        ...common,
        answerType: 'single_choice',
        options,
        allowFreeText: rawQuestion.allowFreeText,
      }]
    }
    if (rawQuestion.answerType === 'multiple_choice') {
      const options = parsePlanQuestionOptions(rawQuestion.options)
      const minimum = rawQuestion.minSelections
      const maximum = rawQuestion.maxSelections
      if (
        !options
        || options.length < 2
        || typeof rawQuestion.allowFreeText !== 'boolean'
        || !Number.isInteger(minimum)
        || (minimum as number) < 1
        || (maximum !== null && maximum !== undefined && !Number.isInteger(maximum))
      ) return []
      const capacity = options.length + Number(rawQuestion.allowFreeText)
      if (
        (minimum as number) > capacity
        || (maximum !== null && maximum !== undefined && (
          (maximum as number) < (minimum as number) || (maximum as number) > capacity
        ))
      ) return []
      return [{
        ...common,
        answerType: 'multiple_choice',
        options,
        allowFreeText: rawQuestion.allowFreeText,
        minSelections: minimum as number,
        maxSelections: maximum as number | null | undefined,
        selectedOptionIds: [],
      }]
    }
    if (rawQuestion.answerType === 'text') {
      return [{ ...common, answerType: 'text' }]
    }
    if (rawQuestion.answerType === 'date') {
      return [{ ...common, answerType: 'date' }]
    }
    if (rawQuestion.answerType === 'time') {
      if (!isNonBlankString(rawQuestion.timeZone)) return []
      const minimum = rawQuestion.minimum == null
        ? null
        : normalizeMinuteTime(rawQuestion.minimum)
      const maximum = rawQuestion.maximum == null
        ? null
        : normalizeMinuteTime(rawQuestion.maximum)
      if (
        (rawQuestion.minimum != null && minimum == null)
        || (rawQuestion.maximum != null && maximum == null)
        || (minimum != null && maximum != null && minimum > maximum)
      ) return []
      return [{
        ...common,
        answerType: 'time',
        timeZone: rawQuestion.timeZone,
        minimum,
        maximum,
      }]
    }
    if (rawQuestion.answerType === 'datetime') {
      if (!isNonBlankString(rawQuestion.timeZone)) return []
      const minimum = rawQuestion.minimum == null
        ? null
        : normalizeMinuteDateTime(rawQuestion.minimum)
      const maximum = rawQuestion.maximum == null
        ? null
        : normalizeMinuteDateTime(rawQuestion.maximum)
      if (
        (rawQuestion.minimum != null && minimum == null)
        || (rawQuestion.maximum != null && maximum == null)
        || (minimum != null && maximum != null && minimum > maximum)
      ) return []
      return [{
        ...common,
        answerType: 'datetime',
        timeZone: rawQuestion.timeZone,
        minimum,
        maximum,
      }]
    }
    return []
  })
  return questions.length === value.length
    && questions.length > 0
    && new Set(questions.map((question) => question.id)).size === questions.length
    ? questions
    : null
}

const isIsoCalendarDate = (value: string) => {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value)
  if (!match) return false
  const year = Number(match[1])
  const month = Number(match[2])
  const day = Number(match[3])
  if (year < 1 || month < 1 || month > 12) return false
  const leapYear = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0)
  const daysInMonth = [31, leapYear ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
  return day >= 1 && day <= (daysInMonth[month - 1] ?? 0)
}

const PLAN_REVIEW_ACTIONS = ['approve', 'reject', 'cancel'] as const

const parsePlanReviewActions = (
  responseSchema: JsonValue | undefined,
): PlanReviewState['allowedActions'] | null => {
  if (!isJsonObject(responseSchema)) return null
  const discriminator = responseSchema.discriminator
  if (
    !isJsonObject(discriminator)
    || !hasOnlyKeys(discriminator, ['mapping', 'propertyName'])
    || discriminator.propertyName !== 'type'
    || !isJsonObject(discriminator.mapping)
  ) return null
  const mapping = discriminator.mapping
  const actions = Object.keys(mapping)
  if (
    actions.length === 0
    || actions.some((action) => (
      !PLAN_REVIEW_ACTIONS.includes(action as typeof PLAN_REVIEW_ACTIONS[number])
      || typeof mapping[action] !== 'string'
    ))
  ) return null
  return actions as PlanReviewState['allowedActions']
}

export const planInteractionFromInterrupts = (
  interrupts: readonly PlanInterruptLike[],
): Conversation['planInteraction'] => {
  const planInterrupts = interrupts.filter((interrupt) => (
    interrupt.reason === 'tinkerfin:plan_clarification' || interrupt.reason === 'tinkerfin:plan_review'
  ))
  if (planInterrupts.length === 0) return undefined
  if (interrupts.length !== 1 || planInterrupts.length !== 1) return invalidPlanInteraction()
  const interrupt = planInterrupts[0]
  if (!interrupt) return invalidPlanInteraction()
  const metadata = runtimeEnvelopeMetadata(interrupt)
  if (!metadata) return invalidPlanInteraction()
  if (metadata.origin !== 'plan') return invalidPlanInteraction()

  if (interrupt.reason === 'tinkerfin:plan_clarification') {
    const clarification = metadata.clarification
    if (!clarification || typeof clarification !== 'object' || Array.isArray(clarification)) return invalidPlanInteraction()
    if (!hasOnlyKeys(clarification as JsonObject, ['form'])) return invalidPlanInteraction()
    const form = clarification.form
    if (!form || typeof form !== 'object' || Array.isArray(form)) return invalidPlanInteraction()
    if (
      !hasOnlyKeys(form as JsonObject, ['description', 'questions', 'title'])
      || !isStringWithinLength(form.title, 20)
      || !isStringWithinLength(form.description, 60)
    ) return invalidPlanInteraction()
    const questions = parsePlanQuestions(form.questions)
    if (!questions) return invalidPlanInteraction()
    return {
      kind: 'questions',
      interruptId: interrupt.id,
      title: form.title,
      description: form.description,
      activeQuestionIndex: 0,
      form: structuredClone(form as JsonObject),
      questions,
      submitted: false,
    }
  }

  if (interrupt.reason === 'tinkerfin:plan_review') {
    const allowedActions = parsePlanReviewActions(interrupt.responseSchema)
    if (!allowedActions) return invalidPlanInteraction()
    const review = metadata.review
    if (
      !review
      || typeof review !== 'object'
      || Array.isArray(review)
      || !hasOnlyKeys(review as JsonObject, ['draft'])
    ) return invalidPlanInteraction()
    const draft = review.draft
    if (
      !draft
      || typeof draft !== 'object'
      || Array.isArray(draft)
    ) return invalidPlanInteraction()
    const contentSchema = draft.contentSchema
    const content = draft.content
    if (
      !hasOnlyKeys(draft as JsonObject, ['content', 'contentSchema', 'revision'])
      || typeof draft.revision !== 'number'
      || !Number.isInteger(draft.revision)
      || draft.revision < 1
      || !contentSchema
      || typeof contentSchema !== 'object'
      || Array.isArray(contentSchema)
      || !hasOnlyKeys(contentSchema as JsonObject, ['fingerprint', 'mediaType'])
      || contentSchema.mediaType !== 'text/markdown'
      || typeof contentSchema.fingerprint !== 'string'
      || !/^[0-9a-f]{64}$/.test(contentSchema.fingerprint)
      || !content
      || typeof content !== 'object'
      || Array.isArray(content)
      || !hasOnlyKeys(content as JsonObject, ['description', 'markdown'])
      || !isStringWithinLength(content.description, 80)
      || typeof content.markdown !== 'string'
      || !content.markdown.trim()
    ) return invalidPlanInteraction()
    return {
      kind: 'review',
      interruptId: interrupt.id,
      revision: draft.revision,
      draft: structuredClone(draft) as unknown as MarkdownPlanDraft,
      allowedActions,
      submitted: false,
    }
  }

  return undefined
}

const attachApproval = (conversation: Conversation, interrupts: InterruptEvent[]) => ({
  ...conversation,
  approval: {
    items: approvalItemsFromInterrupts(interrupts),
    activeIndex: 0,
    submitted: false,
    mode: "options" as const,
  },
})

const upsertAssistantMessage = (
  conversation: Conversation,
  messageId: string,
  patch: (message: Message | null) => Message,
) => {
  const index = findMessageIndex(conversation, (message) => message.id === messageId)
  if (index < 0) {
    return {
      ...conversation,
      messages: [...conversation.messages, patch(null)],
    }
  }

  return {
    ...conversation,
    messages: conversation.messages.map((message, currentIndex) =>
      currentIndex === index ? patch(message) : message),
  }
}

const upsertToolMessage = (
  conversation: Conversation,
  toolCallId: string,
  patch: (message: Message | null) => Message,
) => {
  const index = findMessageIndex(
    conversation,
    (message) => message.role === "tool" && message.meta?.toolCallId === toolCallId,
  )
  if (index < 0) {
    return {
      ...conversation,
      messages: [...conversation.messages, patch(null)],
    }
  }

  return {
    ...conversation,
    messages: conversation.messages.map((message, currentIndex) =>
      currentIndex === index ? patch(message) : message),
  }
}

const parseTaskDescriptor = (params: string) => {
  const parsed = parseJsonObject(params)
  if (!parsed) return null

  return {
    agentName:
      typeof parsed.subagent_type === "string" && parsed.subagent_type
        ? parsed.subagent_type
        : "subagent",
    input:
      typeof parsed.description === "string" && parsed.description
        ? parsed.description
        : "",
  }
}

type ResolvedRawEventContext = RawEventContext & Required<
  Pick<RawEventContext, "streamMode" | "source">
>

const runIdForSource = (
  conversation: Conversation,
  rawEvent: ResolvedRawEventContext,
) => (
  rawEvent.source.agentType === "subagent"
  && rawEvent.source.subagentInvocationId
    ? rawEvent.source.subagentInvocationId
    : rawEvent.runId
) ?? conversation.messages.find(
  (message) =>
    message.role === "subagent"
    && rawEvent.source.graphTaskId != null
    && message.meta?.graphTaskId === rawEvent.source.graphTaskId,
)?.meta?.subRunId

const rawEventOrMain = (
  conversation: Conversation,
  rawEvent: RawEventContext | undefined,
): ResolvedRawEventContext => ({
  ...rawEvent,
  streamMode: rawEvent?.streamMode ?? "messages",
  source: rawEvent?.source ?? {
    kind: "root",
    agentType: "main",
    agentName: "main",
    graphNamespace: [],
  },
  runId: rawEvent?.runId ?? conversation.activeRunId,
})

const startSubagentRun = (
  conversation: Conversation,
  provenance: SubagentProvenance,
): Conversation => {
  const subRunId = provenance.subagentInvocationId
  const existing = conversation.messages.find(
    (message) => message.role === "subagent" && message.meta?.subRunId === subRunId,
  )
  if (existing) {
    if (
      existing.meta?.originMainRunId == null
      || existing.meta?.agentName !== provenance.agentName
      || (existing.meta?.graphTaskId != null && existing.meta.graphTaskId !== provenance.graphTaskId)
      || existing.meta?.toolCallId !== provenance.parentToolCallId
      || (existing.meta?.graphTaskId != null && existing.meta.input !== provenance.description)
    ) throw new Error(`子 Agent 身份冲突: ${subRunId}`)
    return updateMessage(
      conversation,
      (message) => message.id === existing.id
        || (message.role === "tool" && message.meta?.toolCallId === provenance.parentToolCallId),
      (message) => ({
        ...message,
        meta: {
          ...message.meta,
          status: "running",
          lastMainRunId: provenance.requestRunId,
          graphTaskId: provenance.graphTaskId,
          input: message.role === "subagent" ? provenance.description : message.meta?.input,
          completedAt: undefined,
          durationMs: undefined,
        },
      }),
    )
  }

  const graphTaskId = provenance.graphTaskId
  const pendingTasks = conversation.messages.filter(
    (message) =>
      message.role === "tool"
      && message.meta?.toolName === "task"
      && message.meta?.status === "running"
      && !message.meta?.subRunId,
  )
  const task = pendingTasks.find(
    (message) => message.meta?.toolCallId === provenance.parentToolCallId,
  )
  const createdAt = nowIso()
  const subagentMessage: Message = {
    id: subRunId,
    role: "subagent",
    content: task?.content ?? "",
    createdAt,
    meta: {
      agentName: provenance.agentName,
      input: provenance.description,
      result: "",
      status: "running",
      toolCallId: provenance.parentToolCallId,
      subRunId,
      runId: subRunId,
      originMainRunId: provenance.requestRunId,
      lastMainRunId: provenance.requestRunId,
      graphTaskId,
    },
  }

  return {
    ...conversation,
    messages: [
      ...conversation.messages.map((message) => message.id === task?.id
        ? {
            ...message,
            meta: {
              ...message.meta,
              subRunId,
              graphTaskId,
              lastMainRunId: provenance.requestRunId,
            },
          }
        : message),
      subagentMessage,
    ],
  }
}

const updateSubagentRun = (
  conversation: Conversation,
  rawEvent: ResolvedRawEventContext,
  updater: (message: Message) => Message,
): Conversation => {
  const runId = runIdForSource(conversation, rawEvent)
  const index = findMessageIndex(
    conversation,
    (message) =>
      message.role === "subagent"
      && (runId != null
        ? message.meta?.subRunId === runId
        : (
          rawEvent.source.graphTaskId != null
          && message.meta?.graphTaskId === rawEvent.source.graphTaskId
        )),
  )

  if (index < 0) return conversation

  return {
    ...conversation,
    messages: conversation.messages.map((message, currentIndex) =>
      currentIndex === index ? updater(message) : message),
  }
}

const syncTodosFromState = (
  conversation: Conversation,
  state: JsonObject | undefined,
): Conversation => {
  const rawTodos = state?.todos
  if (!Array.isArray(rawTodos)) return conversation

  const nextTodos: TodoItem[] = rawTodos
    .filter(isStateTodo)
    .map((todo, index) => {
      const previous = conversation.todos[index]
      return {
        id: previous?.id ?? `todo-${index}`,
        content: todo.content,
        status: toTodoStatus(todo.status),
        result: previous?.result,
      }
    })

  return {
    ...conversation,
    todos: nextTodos,
  }
}

const isAgentMode = (value: unknown): value is AgentMode =>
  value === "default" || value === "plan"

const forwardedPropsFor = (
  model: string,
  mode: AgentMode,
): ChatRequestPayload["forwardedProps"] => ({
  model,
  command: { plan: mode === "plan" ? "on" : "off" },
})

const syncEffectiveModeFromState = (
  conversation: Conversation,
  state: JsonObject,
): Conversation => {
  const plan = state.tinkerfin_plan
  if (!plan || typeof plan !== "object" || Array.isArray(plan)) return conversation
  const mode = plan.effectiveMode
  return isAgentMode(mode) ? { ...conversation, mode } : conversation
}

export const buildInitialPayload = (
  conversation: Conversation,
  content: string,
  attachments: readonly Attachment[] = [],
): ChatRequestPayload => {
  const runId = createRunId()
  return {
    threadId: conversation.threadId,
    runId,
    state: {},
    messages: [
      {
        id: `request-${runId}`,
        role: "user",
        content: attachments.length ? [...(content ? [{ type: "text", text: content }] : []), ...attachments.map(attachmentInput)] : content,
      } satisfies ChatMessageInput,
    ],
    tools: [],
    context: [],
    forwardedProps: forwardedPropsFor(conversation.model, conversation.mode),
  }
}

const matchesApprovalGroup = (
  approval: ApprovalState | undefined,
  expectedInterruptIds: readonly string[],
) => Boolean(
  approval
  && approval.items.length === expectedInterruptIds.length
  && approval.items.every(
    (item, index) => item.interruptId === expectedInterruptIds[index],
  ),
)

export const buildResumePayload = (
  conversation: Conversation,
  expectedInterruptIds?: readonly string[],
): ChatRequestPayload => {
  const approval = conversation.approval
  if (!approval || approval.items.length === 0) {
    throw new ConversationError("approval_stale")
  }
  if (approval.submitted) throw new ConversationError("approval_stale")
  if (
    expectedInterruptIds
    && !matchesApprovalGroup(approval, expectedInterruptIds)
  ) throw new ConversationError("approval_stale")

  const seenInterruptIds = new Set<string>()
  for (const item of approval.items) {
    if (!item.interruptId.trim()) throw new ConversationError("approval_stale")
    if (seenInterruptIds.has(item.interruptId)) throw new ConversationError("approval_stale")
    seenInterruptIds.add(item.interruptId)
    if (!item.decision) throw new ConversationError("approval_incomplete")
    if (item.decision === "rejected") {
      if (!item.allowedDecisions.includes("reject")) {
        throw new ConversationError("approval_stale")
      }
      continue
    }
    if (!item.allowedDecisions.includes("approve")) {
      throw new ConversationError("approval_stale")
    }
  }

  const items = approval.items
  const resume = items.map<ChatResumeEntry>((item) => {
    if (item.decision === "rejected") {
      return {
        interruptId: item.interruptId,
        status: "resolved",
        payload: {
          type: "reject",
          ...(item.rejectionReason ? { message: item.rejectionReason } : {}),
        },
      }
    }

    return {
      interruptId: item.interruptId,
      status: "resolved",
      payload: { type: "approve" },
    }
  })

  return {
    threadId: conversation.threadId,
    runId: createRunId(),
    state: {},
    messages: [],
    tools: [],
    context: [],
    forwardedProps: forwardedPropsFor(conversation.model, conversation.mode),
    resume,
  }
}

export const buildPlanResumePayload = (
  conversation: Conversation,
): ChatRequestPayload => {
  const interaction = conversation.planInteraction
  if (!interaction) throw new ConversationError("plan_stale")
  if (interaction.submitted) throw new ConversationError("plan_already_submitted")

  let payload: ChatResumeEntry['payload']
  if (interaction.kind === 'questions') {
    const answers: Record<string, PlanClarificationAnswer> = {}
    interaction.questions.forEach((question) => {
      if (question.skipped) {
        if (question.required) throw new ConversationError("plan_required_answers_missing")
        answers[question.id] = { status: 'skipped' }
        return
      }
      if (question.answerType === 'single_choice') {
        const option = question.options.find((item) => item.id === question.selectedOptionId)
        const customAnswer = question.customAnswer?.trim() ?? ''
        if (!option && !customAnswer) {
          if (question.required) throw new ConversationError("plan_required_answers_missing")
          answers[question.id] = { status: 'skipped' }
          return
        }
        if (customAnswer && !question.allowFreeText) {
          throw new ConversationError("plan_option_required")
        }
        answers[question.id] = option
          ? { status: 'answered', answerType: 'single_choice', optionId: option.id }
          : { status: 'answered', answerType: 'single_choice', customAnswer }
        return
      }
      if (question.answerType === 'multiple_choice') {
        const knownIds = new Set(question.options.map((option) => option.id))
        const optionIds = [...new Set(question.selectedOptionIds)].filter((id) => knownIds.has(id))
        const customAnswer = question.customAnswer?.trim() ?? ''
        const count = optionIds.length + Number(Boolean(customAnswer))
        const maximum = question.maxSelections
          ?? question.options.length + Number(question.allowFreeText)
        if (count === 0) {
          if (question.required) throw new ConversationError("plan_required_answers_missing")
          answers[question.id] = { status: 'skipped' }
          return
        }
        if (
          (customAnswer && !question.allowFreeText)
          || count < question.minSelections
          || count > maximum
        ) throw new ConversationError("plan_answer_invalid")
        answers[question.id] = {
          status: 'answered',
          answerType: 'multiple_choice',
          optionIds,
          ...(customAnswer ? { customAnswer } : {}),
        }
        return
      }
      if (question.answerType === 'text') {
        const answer = question.answer?.trim() ?? ''
        if (!answer) {
          if (question.required) throw new ConversationError("plan_required_answers_missing")
          answers[question.id] = { status: 'skipped' }
          return
        }
        answers[question.id] = { status: 'answered', answerType: 'text', answer }
        return
      }
      if (question.answerType === 'date') {
        const date = question.date?.trim() ?? ''
        if (!date) {
          if (question.required) throw new ConversationError("plan_required_answers_missing")
          answers[question.id] = { status: 'skipped' }
          return
        }
        if (!isIsoCalendarDate(date)) throw new ConversationError("plan_answer_invalid")
        answers[question.id] = { status: 'answered', answerType: 'date', date }
        return
      }
      if (question.answerType === 'datetime') {
        const dateTime = normalizeMinuteDateTime(question.dateTime?.trim() ?? '')
        if (!dateTime) {
          if (question.required) throw new ConversationError("plan_required_answers_missing")
          answers[question.id] = { status: 'skipped' }
          return
        }
        if (
          (question.minimum != null && dateTime < question.minimum)
          || (question.maximum != null && dateTime > question.maximum)
        ) throw new ConversationError("plan_answer_invalid")
        answers[question.id] = {
          status: 'answered',
          answerType: 'datetime',
          dateTime: `${dateTime}:00`,
        }
        return
      }
      const time = normalizeMinuteTime(question.time?.trim() ?? '')
      if (!time) {
        if (question.required) throw new ConversationError("plan_required_answers_missing")
        answers[question.id] = { status: 'skipped' }
        return
      }
      if (
        (question.minimum != null && time < question.minimum)
        || (question.maximum != null && time > question.maximum)
      ) throw new ConversationError("plan_answer_invalid")
      answers[question.id] = {
        status: 'answered',
        answerType: 'time',
        time: `${time}:00`,
      }
    })
    payload = { type: 'respond', answers }
  } else {
    if (!interaction.action) throw new ConversationError("plan_action_required")
    if (!interaction.allowedActions?.includes(interaction.action)) {
      throw new ConversationError("plan_action_required")
    }
    if (interaction.action === 'approve') {
      payload = { type: 'approve', baseRevision: interaction.revision }
    } else if (interaction.action === 'reject') {
      const message = interaction.message?.trim()
      payload = {
        type: 'reject',
        baseRevision: interaction.revision,
        ...(message ? { message } : {}),
      }
    } else {
      payload = { type: 'cancel', baseRevision: interaction.revision }
    }
  }

  return {
    threadId: conversation.threadId,
    runId: createRunId(),
    state: {},
    messages: [],
    tools: [],
    context: [],
    forwardedProps: forwardedPropsFor(
      conversation.model,
      interaction.kind === 'review' && interaction.action === 'approve'
        ? 'default'
        : 'plan',
    ),
    resume: [{
      interruptId: interaction.interruptId,
      status: 'resolved',
      payload,
    }],
  }
}

export const buildPlanAbandonPayload = (
  conversation: Conversation,
): ChatRequestPayload => {
  const interaction = conversation.planInteraction
  if (!interaction) throw new ConversationError("plan_stale")
  return {
    threadId: conversation.threadId,
    runId: createRunId(),
    state: {},
    messages: [],
    tools: [],
    context: [],
    forwardedProps: forwardedPropsFor(conversation.model, 'default'),
    resume: [{
      interruptId: interaction.interruptId,
      status: 'cancelled',
    }],
  }
}

export const markConversationDetached = (
  conversation: Conversation,
  reason = translateCurrent('已停止接收实时输出，后端任务可能仍在继续'),
): Conversation => {
  if (conversation.runStatus !== "streaming") return conversation
  return setConversationNotice(
    {
      ...conversation,
      runStatus: "detached",
    },
    reason,
    "info",
  )
}

export const prepareResumeSubmission = (
  conversation: Conversation,
  expectedInterruptIds?: readonly string[],
): Conversation => {
  if (
    expectedInterruptIds
    && !matchesApprovalGroup(conversation.approval, expectedInterruptIds)
  ) return conversation

  return {
    ...conversation,
    runStatus: "streaming",
    notice: undefined,
    messages: conversation.messages.map((message) => {
      if (message.role !== "tool") return message
      const matchesApproval = conversation.approval?.items.some(
        (item) => item.toolCallId && item.toolCallId === message.meta?.toolCallId,
      )
      if (!matchesApproval) return message
      return {
        ...message,
        meta: {
          ...message.meta,
          status: "running",
          interruptId: undefined,
        },
      }
    }),
    approval: conversation.approval
      ? { ...conversation.approval, submitted: true, error: undefined }
      : conversation.approval,
  }
}

const restorePendingInteraction = (conversation: Conversation): Conversation => {
  const approval = conversation.approval
    ? { ...conversation.approval, submitted: false }
    : undefined
  if (approval) delete approval.error
  const planInteraction = conversation.planInteraction
    ? { ...conversation.planInteraction, submitted: false }
    : undefined
  if (planInteraction) delete planInteraction.error
  const interruptByToolId = new Map<string, string>()
  for (const item of approval?.items ?? []) {
    if (item.toolCallId) interruptByToolId.set(item.toolCallId, item.interruptId)
  }
  const interruptedRunIds = new Set(
    conversation.messages.flatMap((message) => {
      const meta = message.meta
      if (
        message.role !== "tool"
        || !meta
        || meta.subRunId != null
        || typeof meta.runId !== "string"
        || typeof meta.toolCallId !== "string"
        || !interruptByToolId.has(meta.toolCallId)
      ) return []
      return [meta.runId]
    }),
  )
  return {
    ...conversation,
    runStatus: "waiting_approval",
    activeRunId: undefined,
    approval,
    planInteraction,
    messages: conversation.messages.map((message) => {
      const toolCallId = message.meta?.toolCallId
      const interruptId = toolCallId
        ? interruptByToolId.get(toolCallId)
        : undefined
      if (
        message.role !== "tool"
        || (message.meta?.status !== "running" && message.meta?.status !== "paused")
        || message.meta.subRunId != null
        || typeof message.meta.runId !== "string"
        || !interruptedRunIds.has(message.meta.runId)
      ) return message
      return {
        ...message,
        meta: {
          ...message.meta,
          status: "paused",
          interruptId,
        },
      }
    }),
  }
}

export const applyConversationEvent = (
  conversation: Conversation,
  event: ConversationAgUiEvent,
): Conversation => {
  switch (event.type) {
    case "RAW": {
      if (
        event.source !== "langgraph.tasks"
        || event.rawEvent?.type !== "tasks"
        || event.rawEvent.phase !== "start"
      ) return conversation
      const provenanceValue = event.event.provenance
      if (
        !provenanceValue
        || typeof provenanceValue !== "object"
        || Array.isArray(provenanceValue)
      ) return conversation
      const subagents = provenanceValue.subagents
      if (!Array.isArray(subagents)) return conversation
      return subagents.reduce((current, value) => {
        const provenance = parseSubagentProvenance(value)
        return startSubagentRun(current, provenance)
      }, conversation)
    }

    case "RUN_STARTED": {
      const isResume = Boolean(
        conversation.approval?.submitted
        || conversation.planInteraction?.submitted,
      )
      const initializationFailed = event.rawEvent?.initializationFailed === true
      const preservePending = initializationFailed
        && Boolean(conversation.approval || conversation.planInteraction)
      const pending = preservePending
        ? restorePendingInteraction(conversation)
        : conversation
      return {
        ...pending,
        threadId: event.threadId,
        ...(event.title !== undefined ? mergeConversationTitle(conversation, { ...event, title: event.title.trim() || conversation.title }) : {}),
        activeRunId: preservePending ? undefined : event.runId,
        runStatus: preservePending ? "waiting_approval" : "streaming",
        notice: undefined,
        approval: isResume && !preservePending ? undefined : pending.approval,
        planInteraction: preservePending ? pending.planInteraction : undefined,
        messages: pending.messages.map((message) => (
          isResume && !preservePending && message.meta?.status === "paused"
            ? {
                ...message,
                meta: {
                  ...message.meta,
                  status: "running" as const,
                  interruptId: undefined,
                },
              }
            : message
        )),
      }
    }

    case "CUSTOM": {
      if (event.name === 'studio.conversation.title.updated') {
        if (!isConversationTitle(event.value) || event.value.threadId !== conversation.threadId) return conversation
        return { ...conversation, ...mergeConversationTitle(conversation, event.value) }
      }
      if (event.name !== 'tinkerfin.message.attachments' || !isJsonObject(event.value)) return conversation
      const messageId = event.value.messageId
      const attachments = event.value.attachments
      if (typeof messageId !== 'string' || !Array.isArray(attachments)) return conversation
      const additions = attachments.filter(isAttachment)
      const mergeAttachments = (message: Message | null) =>
        [...new Map([...(message?.attachments ?? []), ...additions].map(item => [item.id, item])).values()]
      const rawEvent = rawEventOrMain(conversation, event.rawEvent)
      if (rawEvent.source.agentType === 'subagent') {
        return updateSubagentRun(conversation, rawEvent, message => ({
          ...message,
          attachments: mergeAttachments(message),
        }))
      }
      return upsertAssistantMessage(conversation, messageId, message => ({
        id: messageId, role: 'assistant', content: message?.content ?? '', createdAt: message?.createdAt ?? nowIso(),
        meta: message?.meta,
        attachments: mergeAttachments(message),
      }))
    }

    case "MESSAGES_SNAPSHOT":
      // 快照按工具调用 ID 同步附件；已有卡片的文本、运行状态和来源仍由实时事件维护
      return {
        ...conversation,
        messages: conversation.messages.length === 0
          ? event.messages
            .filter((message) => message.role === "user" || message.role === "assistant" || message.role === "tool")
            .map((message): Message => message.role === "tool" ? {
              id: message.toolCallId!,
              role: "tool",
              content: "tool",
              attachments: message.attachments ?? messageAttachments(message.content),
              createdAt: nowIso(),
              meta: { toolCallId: message.toolCallId, result: messageText(message.content), status: message.error ? "failed" : "completed" },
            } : {
              id: message.id,
              role: message.role === "user" ? "user" : "assistant",
              content: messageText(message.content),
              attachments: message.attachments ?? messageAttachments(message.content),
              createdAt: nowIso(),
            })
          : conversation.messages.map(message => {
              const snapshot = event.messages.find(item => item.role === message.role && (
                message.role === "tool"
                  ? item.toolCallId === message.meta?.toolCallId
                  : item.id === message.id
              ))
              return snapshot ? {
                ...message,
                attachments: snapshot.attachments ?? messageAttachments(snapshot.content),
              } : message
            }),
      }

    case "STATE_SNAPSHOT":
      return syncEffectiveModeFromState(syncTodosFromState({
        ...conversation,
        serverState: event.snapshot,
      }, event.snapshot), event.snapshot)

    case "STATE_DELTA":
      {
        const nextState = applyStateDelta(conversation.serverState, event.delta)
        return syncEffectiveModeFromState(syncTodosFromState({
          ...conversation,
          serverState: nextState,
        }, nextState), nextState)
      }

    case "TEXT_MESSAGE_START": {
      const rawEvent = rawEventOrMain(conversation, event.rawEvent)
      if (rawEvent.source.agentType === "subagent") {
        return updateSubagentRun(conversation, rawEvent, (message) => ({
          ...message,
          meta: {
            ...message.meta,
            agentName: rawEvent.source.agentName,
            status: "running",
          },
        }))
      }
      return upsertAssistantMessage(conversation, event.messageId, (message) => ({
        id: event.messageId,
        role: "assistant",
        content: message?.content ?? "",
        attachments: message?.attachments,
        createdAt: message?.createdAt ?? nowIso(),
        meta: {
          ...message?.meta,
          status: "running",
          runId: conversation.activeRunId,
        },
      }))
    }

    case "TEXT_MESSAGE_CONTENT": {
      const rawEvent = rawEventOrMain(conversation, event.rawEvent)
      if (rawEvent.source.agentType === "subagent") {
        return updateSubagentRun(conversation, rawEvent, (message) => ({
          ...message,
          meta: {
            ...message.meta,
            agentName: rawEvent.source.agentName,
            result: `${message.meta?.result ?? ""}${event.delta}`,
            status: "running",
          },
        }))
      }
      return upsertAssistantMessage(conversation, event.messageId, (message) => ({
        id: event.messageId,
        role: "assistant",
        content: `${message?.content ?? ""}${event.delta}`,
        attachments: message?.attachments,
        createdAt: message?.createdAt ?? nowIso(),
        meta: {
          ...message?.meta,
          status: "running",
          runId: conversation.activeRunId,
        },
      }))
    }

    case "TEXT_MESSAGE_END": {
      const rawEvent = rawEventOrMain(conversation, event.rawEvent)
      if (rawEvent.source.agentType === "subagent") {
        return updateSubagentRun(conversation, rawEvent, (message) => ({
          ...message,
          meta: {
            ...message.meta,
            agentName: rawEvent.source.agentName,
          },
        }))
      }
      return updateMessage(
        conversation,
        (message) => message.id === event.messageId,
        (message) => ({
          ...message,
          meta: {
            ...message.meta,
            status: "completed",
          },
        }),
      )
    }

    case "REASONING_START":
    case "REASONING_MESSAGE_START":
    case "REASONING_MESSAGE_CONTENT":
    case "REASONING_MESSAGE_END":
    case "REASONING_END":
      // 线上保留标准 AG-UI 事件，但会话工作区不持久化或渲染模型推理
      return conversation

    case "TOOL_CALL_START":
      {
        const rawEvent = rawEventOrMain(conversation, event.rawEvent)
        const sourceRunId = runIdForSource(conversation, rawEvent)
        const withSubagentName = rawEvent.source.agentType === "subagent"
          ? updateSubagentRun(conversation, rawEvent, (message) => ({
              ...message,
              meta: {
                ...message.meta,
                agentName: rawEvent.source.agentName,
                status: "running",
              },
            }))
          : conversation
        return upsertToolMessage(withSubagentName, event.toolCallId, (message) => ({
          id: message?.id ?? event.toolCallId,
          role: "tool",
          content: event.toolCallName === "task"
            ? `委派 ${message?.meta?.agentName ?? "subagent"}`
            : event.toolCallName,
          createdAt: message?.createdAt ?? nowIso(),
          meta: {
            ...message?.meta,
            toolName: event.toolCallName,
            params: message?.meta?.params ?? "",
            result: message?.meta?.result ?? "",
            status: message?.meta?.status === "paused" ? "paused" : "running",
            toolCallId: event.toolCallId,
            parentMessageId: event.parentMessageId,
            batchId: message?.meta?.batchId ?? event.parentMessageId,
            runId: sourceRunId ?? conversation.activeRunId,
            graphTaskId: rawEvent.source.graphTaskId ?? message?.meta?.graphTaskId,
            sourceAgentName:
              rawEvent.source.agentType === "subagent"
                ? rawEvent.source.agentName
                : undefined,
          },
        }))
      }

    case "TOOL_CALL_ARGS":
      return upsertToolMessage(conversation, event.toolCallId, (message) => {
        const params = `${message?.meta?.params ?? ""}${event.delta}`
        const taskDescriptor = message?.meta?.toolName === "task" ? parseTaskDescriptor(params) : null
        return {
          id: message?.id ?? event.toolCallId,
          role: "tool",
          content: taskDescriptor
            ? `委派 ${taskDescriptor.agentName}`
            : (message?.content ?? message?.meta?.toolName ?? "tool"),
          createdAt: message?.createdAt ?? nowIso(),
          meta: {
            ...message?.meta,
            params,
            agentName: taskDescriptor?.agentName ?? message?.meta?.agentName,
            input: taskDescriptor?.input ?? message?.meta?.input,
            status: message?.meta?.status === "paused" ? "paused" : "running",
          },
        }
      })

    case "TOOL_CALL_END":
      return conversation

    case "TOOL_CALL_RESULT":
      {
        const rawEvent = rawEventOrMain(conversation, event.rawEvent)
        const completedAt = nowIso()
        let next = upsertToolMessage(conversation, event.toolCallId, (message) => {
          const createdAt = message?.createdAt ?? completedAt
          return {
            id: message?.id ?? event.toolCallId,
            role: "tool",
            content: message?.content ?? message?.meta?.toolName ?? "tool",
            attachments: event.attachments,
            createdAt,
            meta: {
              ...message?.meta,
              result: event.content,
              status: rawEvent.toolResultStatus === "error" ? "failed" : "completed",
              completedAt,
              durationMs: elapsedMs(createdAt, completedAt),
              runId:
                message?.meta?.runId
                ?? runIdForSource(conversation, rawEvent)
                ?? conversation.activeRunId,
              graphTaskId:
                rawEvent.source.graphTaskId
                ?? message?.meta?.graphTaskId,
              sourceAgentName:
                rawEvent.source.agentType === "subagent"
                  ? rawEvent.source.agentName
                  : message?.meta?.sourceAgentName,
            },
          }
        })
        const taskMessage = next.messages.find(
          (message) =>
            message.role === "tool"
            && message.meta?.toolCallId === event.toolCallId
            && message.meta?.toolName === "task",
        )
        const relatedSubagentInvocationId = rawEvent.relatedSubagentInvocationId
        if (taskMessage && relatedSubagentInvocationId) {
          const relatedSubagent = next.messages.find(
            (message) => message.role === "subagent"
              && message.meta?.subRunId === relatedSubagentInvocationId,
          )
          const graphTaskId = relatedSubagent?.meta?.graphTaskId
          next = updateMessage(
            next,
            (message) => message.id === taskMessage.id,
            (message) => ({
              ...message,
              meta: {
                ...message.meta,
                subRunId: relatedSubagentInvocationId,
                graphTaskId,
              },
            }),
          )
          next = updateMessage(
            next,
            (message) => message.role === "subagent"
              && message.meta?.subRunId === relatedSubagentInvocationId,
            (message) => ({
              ...message,
              content: taskMessage.content,
              meta: {
                ...message.meta,
                agentName: taskMessage.meta?.agentName ?? message.meta?.agentName,
                input: taskMessage.meta?.input ?? message.meta?.input,
                result: event.content,
                status: rawEvent.toolResultStatus === "error" ? "failed" : "completed",
                toolCallId: event.toolCallId,
                graphTaskId,
                completedAt: message.meta?.completedAt ?? completedAt,
                durationMs:
                  message.meta?.durationMs
                  ?? elapsedMs(message.createdAt, completedAt),
              },
            }),
          )
        } else if (rawEvent.source.agentType === "subagent") {
          next = updateSubagentRun(next, rawEvent, (message) => ({
            ...message,
            meta: {
              ...message.meta,
              agentName: rawEvent.source.agentName,
            },
          }))
        }
        return next
      }

    case "RUN_FINISHED":
      {
        const outcome = event.outcome ?? { type: "success" as const }
        if (conversation.activeRunId && event.runId !== conversation.activeRunId) {
          return conversation
        }

      if (outcome.type === "interrupt") {
        const planInteraction = planInteractionFromInterrupts(outcome.interrupts)
        if (planInteraction) {
          return {
            ...conversation,
            threadId: event.threadId,
            runStatus: "waiting_approval",
            activeRunId: undefined,
            approval: undefined,
            planInteraction,
          }
        }
        return attachApproval(
          markInterruptedToolCards(
            {
              ...conversation,
              threadId: event.threadId,
              runStatus: "waiting_approval",
              activeRunId: undefined,
            },
            event.runId,
            outcome.interrupts,
          ),
          outcome.interrupts,
        )
      }

      return {
        ...conversation,
        threadId: event.threadId,
        runStatus: "idle",
        activeRunId: undefined,
        approval: undefined,
        planInteraction: undefined,
      }
      }

    case "RUN_ERROR":
      {
        const rawEvent = rawEventOrMain(conversation, event.rawEvent)
        const completedAt = nowIso()
        const errorMessage = conversationErrorMessage(
          new ConversationError(event.code === 'runtime_initialization_error' ? 'run_initialization_failed' : 'run_failed', event.message),
          'run_failed',
        )
        const isCancelled = event.code === "cancelled" || event.code === "resume_cancelled"
        const visibleMessage = isCancelled
          ? translateCurrent(event.code === "resume_cancelled" ? '已取消' : '任务已停止')
          : errorMessage
        const errorRunId = rawEvent.runId ?? conversation.activeRunId
        const runFailures = !isCancelled && errorRunId
              && conversation.messages.some(message => message.role === 'user' && message.meta?.runId === errorRunId)
              ? [...(conversation.runFailures ?? []).filter(item => item.runId !== errorRunId), {
                  runId: errorRunId,
                  errorCode: event.code ?? null,
                  failedAt: completedAt,
                  retryable: event.code === 'runtime_initialization_error',
                }]
              : conversation.runFailures
        if (conversation.activeRunId && errorRunId && conversation.activeRunId !== errorRunId) {
          return { ...conversation, runFailures }
        }
        if (
          rawEvent.initializationFailed === true
          && (conversation.approval || conversation.planInteraction)
        ) {
          return setConversationNotice(
            restorePendingInteraction(conversation),
            conversationErrorMessage(
              new ConversationError('resume_failed', event.message),
              'resume_failed',
            ),
            "error",
            `${errorRunId}:terminal`,
          )
        }
        // 用户主动停止是正常业务终态，不能把未完成工作渲染成系统故障
        return {
            ...conversation,
            notice: undefined,
            runFailures,
            runStatus: isCancelled ? "idle" : "error",
            activeRunId: undefined,
            approval: undefined,
            planInteraction: undefined,
            messages: conversation.messages.map((message) => (
              (message.role === "tool" || message.role === "subagent")
                && (message.meta?.status === "running" || message.meta?.status === "paused")
                && (
                  message.meta?.subRunId == null
                  || message.meta.lastMainRunId === errorRunId
                )
                ? {
                    ...message,
                    meta: {
                      ...message.meta,
                      status: isCancelled ? "cancelled" as const : "failed" as const,
                      result: message.role === "subagent"
                        ? message.meta?.result || visibleMessage
                        : visibleMessage,
                      completedAt,
                      durationMs: elapsedMs(message.createdAt, completedAt),
                    },
                  }
                : message
            )),
            todos: conversation.todos.map((todo) => (
              isCancelled && todo.status === "running"
                ? { ...todo, status: "cancelled" as const }
                : todo
            )),
          }
      }

    default:
      return conversation
  }
}
