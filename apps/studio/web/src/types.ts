import type { ConversationTitleSnapshot } from "./api/conversation/titles"
import type { ConversationHistoryCoreDetail } from './api/conversation/history'
import type {
  ReadyTaskTraceSnapshot,
  UnavailableTaskTraceSnapshot,
} from './api/conversation/taskTrace'

export type MessageRole = 'user' | 'assistant' | 'process' | 'tool' | 'subagent' | 'approval' | 'error'
export type TodoStatus = 'pending' | 'running' | 'completed' | 'failed' | 'cancelled'
export type ApprovalDecision = 'approved' | 'rejected'
export type ConversationRunStatus = 'idle' | 'streaming' | 'waiting_approval' | 'detached' | 'error'
export type PendingInteractionKind =
  | 'tool_approval'
  | 'plan_clarification'
  | 'plan_review'
  | 'input_required'
export type ApprovalMode = 'options' | 'reject'
export type ApprovalAllowedDecision = 'approve' | 'edit' | 'reject' | 'respond'
export type AgentMode = 'default' | 'plan'

export type WebTaskTraceViewState =
  | { phase: 'unloaded' }
  | { phase: 'loading' }
  | { phase: 'ready'; snapshot: ReadyTaskTraceSnapshot }
  | { phase: 'unavailable'; snapshot: UnavailableTaskTraceSnapshot }

export interface JsonObject {
  [key: string]: JsonValue
}

export type JsonValue =
  | string
  | number
  | boolean
  | null
  | JsonObject
  | JsonValue[]

export interface Message {
  /** 仅供本次实时正文逐字展示；历史数据不创建此标记 */
  liveText?: { key: string; initialContent: string }
  attachments?: import('./features/conversation/attachments/content').Attachment[]
  id: string
  role: MessageRole
  content: string
  createdAt: string
  meta?: {
    /** 历史消息的 Trace 关联键，用于将任务定位到对应的会话消息 */
    traceMessageId?: string
    contentOmitted?: boolean
    title?: string
    toolName?: string
    params?: string
    input?: string
    result?: string
    reasoning?: string
    status?: 'running' | 'completed' | 'failed' | 'paused' | 'cancelled'
    batchId?: string
    agentName?: string
    sourceAgentName?: string
    /** 工具所属的图作用域，用于区分根任务与子图任务清单 */
    graphNamespace?: string[]
    toolCallId?: string
    parentMessageId?: string
    subRunId?: string
    originMainRunId?: string
    lastMainRunId?: string
    graphTaskId?: string
    runId?: string
    completedAt?: string
    durationMs?: number
    interruptId?: string
  }
}

export interface ConversationNotice {
  kind: 'error' | 'info' | 'warning'
  content: string
  /** 同一次请求或连接恢复提示的去重标识 */
  id?: string
  recovery?: 'connection' | 'history'
}

export interface TodoItem {
  id: string
  content: string
  status: TodoStatus
  result?: string
}

export interface ApprovalItem {
  id: string
  interruptId: string
  toolCallId?: string
  toolName: string
  params: string
  input: string
  description: string
  originalArgs: JsonObject
  allowedDecisions: ApprovalAllowedDecision[]
  decision?: ApprovalDecision
  rejectionReason?: string
}

export interface ApprovalState {
  items: ApprovalItem[]
  activeIndex: number
  submitted: boolean
  mode?: ApprovalMode
  error?: string
}

export interface PlanQuestionOption {
  id: string
  label: string
  description?: string | null
  recommended: boolean
  attributes?: JsonObject | null
}

interface PlanQuestionBase {
  id: string
  answerType: 'single_choice' | 'multiple_choice' | 'text' | 'date' | 'time' | 'datetime'
  prompt: string
  required: boolean
  attributes?: JsonObject | null
  skipped?: boolean
}

export interface PlanSingleChoiceQuestion extends PlanQuestionBase {
  answerType: 'single_choice'
  options: PlanQuestionOption[]
  allowFreeText: boolean
  selectedOptionId?: string
  customAnswer?: string
}

export interface PlanMultipleChoiceQuestion extends PlanQuestionBase {
  answerType: 'multiple_choice'
  options: PlanQuestionOption[]
  allowFreeText: boolean
  minSelections: number
  maxSelections?: number | null
  selectedOptionIds: string[]
  customAnswer?: string
}

export interface PlanTextQuestion extends PlanQuestionBase {
  answerType: 'text'
  answer?: string
}

export interface PlanDateQuestion extends PlanQuestionBase {
  answerType: 'date'
  date?: string
}

export interface PlanTimeQuestion extends PlanQuestionBase {
  answerType: 'time'
  timeZone: string
  minimum?: string | null
  maximum?: string | null
  time?: string
}

export interface PlanDateTimeQuestion extends PlanQuestionBase {
  answerType: 'datetime'
  timeZone: string
  minimum?: string | null
  maximum?: string | null
  dateTime?: string
}

export type PlanQuestionItem =
  | PlanSingleChoiceQuestion
  | PlanMultipleChoiceQuestion
  | PlanTextQuestion
  | PlanDateQuestion
  | PlanTimeQuestion
  | PlanDateTimeQuestion

export interface PlanQuestionState {
  kind: 'questions'
  interruptId: string
  title: string
  description: string
  activeQuestionIndex: number
  form: JsonObject
  questions: PlanQuestionItem[]
  submitted: boolean
  error?: string
}

export interface PlanContentSchemaReference extends JsonObject {
  fingerprint: string
  mediaType: 'text/markdown'
}

export interface MarkdownPlanContent extends JsonObject {
  description: string
  markdown: string
}

export interface MarkdownPlanDraft extends JsonObject {
  revision: number
  contentSchema: PlanContentSchemaReference
  content: MarkdownPlanContent
}

export interface PlanReviewState {
  kind: 'review'
  interruptId: string
  revision: number
  draft: MarkdownPlanDraft
  allowedActions: Array<'approve' | 'reject' | 'cancel'>
  action?: 'approve' | 'reject' | 'cancel'
  message?: string
  submitted: boolean
  error?: string
}

export type PlanInteraction = PlanQuestionState | PlanReviewState

export type AccessMode = "full" | "write_approval"

export interface Conversation extends Partial<Pick<ConversationTitleSnapshot, "titleSource" | "titleGenerationStatus" | "titleSeq">> {
  threadId: string
  title: string
  pinned: boolean
  updatedAt: string
  model: string
  mode: AgentMode
  accessMode: AccessMode
  messages: Message[]
  /** 尚未加载或发生失败时可为空；运行结果不属于消息正文 */
  runFailures?: import('./api/conversation/history').ConversationRunFailure[]
  notice?: ConversationNotice
  todos: TodoItem[]
  taskTrace: WebTaskTraceViewState
  approval?: ApprovalState
  planInteraction?: PlanInteraction
  pendingInteractionKind?: PendingInteractionKind
  runStatus: ConversationRunStatus
  activeRunId?: string
  serverState?: JsonObject
  /** 最后一条已持久化 AG-UI 事件序号，用于 afterSeq 续传 */
  lastSeq?: number
  /** Trace 历史或 detached follow 使用的唯一权威语义视图 */
  trace?: ConversationHistoryCoreDetail
  /** 完整会话详情是否已从后端历史恢复 */
  isHydrated?: boolean
}

export interface WorkspaceState {
  conversations: Conversation[]
  currentThreadId: string
}
