import type { ConversationTitleSnapshot } from "./titles"
import type { AccessMode, AgentMode, JsonObject, JsonValue } from "../../types"

export type { AgentMode }

export type ConversationPlanCommand = "on" | "off"

export interface ConversationCommandMap extends JsonObject {
  plan: ConversationPlanCommand
}

export interface ConversationForwardedProps extends JsonObject {
  model: string
  accessMode: AccessMode
  command: ConversationCommandMap
}

export interface ChatMessageInput {
  id: string
  role: "user"
  content: string | JsonObject[]
}

export type PlanClarificationAnswer =
  | { status: "skipped" }
  | {
    status: "answered"
    answerType: "single_choice"
    optionId?: string
    customAnswer?: string
  }
  | {
    status: "answered"
    answerType: "multiple_choice"
    optionIds: string[]
    customAnswer?: string
  }
  | { status: "answered"; answerType: "text"; answer: string }
  | { status: "answered"; answerType: "date"; date: string }
  | { status: "answered"; answerType: "time"; time: string }
  | { status: "answered"; answerType: "datetime"; dateTime: string }

export type ChatResumePayload =
  | { type: "approve" }
  | { type: "reject"; message?: string }
  | {
    type: "edit"
    edited_action: { name: string; args: JsonObject }
  }
  | {
    type: "respond"
    answers: Record<string, PlanClarificationAnswer>
  }
  | { type: "approve"; baseRevision: number }
  | { type: "cancel"; baseRevision: number }
  | { type: "reject"; baseRevision: number; message?: string }

export interface ChatResumeEntry {
  interruptId: string
  status: "resolved" | "cancelled"
  payload?: ChatResumePayload
}

export interface ChatRequestPayload {
  threadId: string
  runId: string
  parentRunId?: string
  state: JsonObject
  messages: ChatMessageInput[]
  tools: JsonValue[]
  context: JsonValue[]
  forwardedProps: ConversationForwardedProps
  resume?: ChatResumeEntry[]
}

interface EventSourceInfoBase {
  kind: "root" | "compiled_subgraph" | "deep_agent_subagent"
  graphNamespace: string[]
  graphTaskId?: string | null
  nodeName?: string | null
  parentGraphNamespace?: string[] | null
  parentToolCallId?: string | null
  subagentInput?: string | null
  subagentInvocationId?: string | null
}

interface RootEventSourceInfo extends EventSourceInfoBase {
  kind: "root"
  agentType: "main"
  agentName: string
}

interface CompiledSubgraphEventSourceInfo extends EventSourceInfoBase {
  kind: "compiled_subgraph"
  agentType?: never
  agentName?: never
}

interface DeepAgentSubagentEventSourceInfo extends EventSourceInfoBase {
  kind: "deep_agent_subagent"
  agentType: "subagent"
  agentName: string
}

export type EventSourceInfo =
  | RootEventSourceInfo
  | CompiledSubgraphEventSourceInfo
  | DeepAgentSubagentEventSourceInfo

export interface RawEventContext {
  streamMode?: "messages" | "tasks" | "values"
  source?: EventSourceInfo
  runId?: string
  relatedSubagentInvocationId?: string
  /** 项目扩展：创建该子智能体的父级 task 工具调用 */
  parentToolCallId?: string
  /** 项目扩展：已校验的子智能体任务描述 */
  subagentInput?: string
  langgraphNode?: string
  interruptId?: string
  initializationFailed?: boolean
  /** 项目扩展：保留 LangChain ToolMessage.status */
  toolResultStatus?: "success" | "error"
}

export interface RunStartedEvent extends Partial<Pick<ConversationTitleSnapshot, "titleSource" | "titleGenerationStatus" | "titleSeq">> {
  type: "RUN_STARTED"
  threadId: string
  runId: string
  parentRunId?: string
  /** 项目扩展：合成运行的子智能体来源信息 */
  rawEvent?: RawEventContext
  /** Studio 扩展：服务端已持久化的权威会话标题 */
  title?: string
}

export interface MessageSnapshotItem {
  toolCallId?: string
  error?: string
  attachments?: import('../../features/conversation/attachments/content').Attachment[]
  id: string
  role: string
  content?: JsonValue
}

export interface MessagesSnapshotEvent {
  type: "MESSAGES_SNAPSHOT"
  rawEvent?: RawEventContext
  messages: MessageSnapshotItem[]
}

export interface StateSnapshotEvent {
  type: "STATE_SNAPSHOT"
  /** 可选项目扩展，标准 AG-UI 生产端可以省略 */
  rawEvent?: RawEventContext
  snapshot: JsonObject
}

export type StateDeltaOperation =
  | { op: "add" | "replace"; path: string; value: JsonValue }
  | { op: "remove"; path: string; value?: never }

export interface StateDeltaEvent {
  type: "STATE_DELTA"
  rawEvent?: RawEventContext
  delta: StateDeltaOperation[]
}

export interface TextMessageStartEvent {
  type: "TEXT_MESSAGE_START"
  rawEvent?: RawEventContext
  messageId: string
  role: string
  name?: string
}

export interface TextMessageContentEvent {
  type: "TEXT_MESSAGE_CONTENT"
  rawEvent?: RawEventContext
  messageId: string
  delta: string
}

export interface TextMessageEndEvent {
  type: "TEXT_MESSAGE_END"
  rawEvent?: RawEventContext
  messageId: string
}

export interface ReasoningStartEvent {
  type: "REASONING_START"
  rawEvent?: RawEventContext
  messageId: string
}

export interface ReasoningMessageStartEvent {
  type: "REASONING_MESSAGE_START"
  rawEvent?: RawEventContext
  messageId: string
  role: "reasoning"
}

export interface ReasoningMessageContentEvent {
  type: "REASONING_MESSAGE_CONTENT"
  rawEvent?: RawEventContext
  messageId: string
  delta: string
}

export interface ReasoningMessageEndEvent {
  type: "REASONING_MESSAGE_END"
  rawEvent?: RawEventContext
  messageId: string
}

export interface ReasoningEndEvent {
  type: "REASONING_END"
  rawEvent?: RawEventContext
  messageId: string
}

export interface ToolCallStartEvent {
  type: "TOOL_CALL_START"
  rawEvent?: RawEventContext
  toolCallId: string
  toolCallName: string
  parentMessageId?: string
}

export interface ToolCallArgsEvent {
  type: "TOOL_CALL_ARGS"
  rawEvent?: RawEventContext
  toolCallId: string
  delta: string
}

export interface ToolCallEndEvent {
  type: "TOOL_CALL_END"
  rawEvent?: RawEventContext
  toolCallId: string
}

export interface ToolCallResultEvent {
  attachments?: import('../../features/conversation/attachments/content').Attachment[]
  type: "TOOL_CALL_RESULT"
  rawEvent?: RawEventContext
  messageId: string
  toolCallId: string
  content: string
  role: string
}

export interface CustomEvent {
  type: "CUSTOM"
  rawEvent?: RawEventContext
  name: string
  value: JsonValue
}

export interface RawStreamEvent {
  type: "RAW"
  rawEvent?: JsonObject
  event: JsonObject
  source?: string
}

export interface InterruptEvent {
  id: string
  reason: string
  message?: string | null
  toolCallId?: string | null
  responseSchema?: JsonObject | null
  metadata?: JsonObject | null
}

export interface RunFinishedSuccessOutcome {
  type: "success"
}

export interface RunFinishedInterruptOutcome {
  type: "interrupt"
  interrupts: InterruptEvent[]
}

export interface RunFinishedEvent {
  type: "RUN_FINISHED"
  rawEvent?: RawEventContext
  threadId: string
  runId: string
  /** ag-ui-protocol 允许生产端省略 outcome，省略表示成功 */
  outcome?: RunFinishedSuccessOutcome | RunFinishedInterruptOutcome
}

export interface RunErrorEvent {
  type: "RUN_ERROR"
  rawEvent?: RawEventContext
  message?: string
  code?: string
  details?: JsonValue
}

export type ConversationAgUiEvent =
  | RunStartedEvent
  | MessagesSnapshotEvent
  | StateSnapshotEvent
  | StateDeltaEvent
  | TextMessageStartEvent
  | TextMessageContentEvent
  | TextMessageEndEvent
  | ReasoningStartEvent
  | ReasoningMessageStartEvent
  | ReasoningMessageContentEvent
  | ReasoningMessageEndEvent
  | ReasoningEndEvent
  | ToolCallStartEvent
  | ToolCallArgsEvent
  | ToolCallEndEvent
  | ToolCallResultEvent
  | CustomEvent
  | RawStreamEvent
  | RunFinishedEvent
  | RunErrorEvent
