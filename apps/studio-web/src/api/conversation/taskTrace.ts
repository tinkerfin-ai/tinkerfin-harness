export type TodoGroupStatus = 'running' | 'completed' | 'incomplete' | 'failed' | 'cancelled'

export type TodoTraceItemStatus =
  | 'pending'
  | 'running'
  | 'completed'
  | 'incomplete'
  | 'failed'
  | 'cancelled'

export type TaskTraceErrorCode =
  | 'trace_incomplete'
  | 'todo_state_omitted'
  | 'todo_state_invalid'

export interface TodoTraceItem {
  id: string
  content: string
  status: TodoTraceItemStatus
}

export interface TodoGroup {
  id: string
  userMessageId: string
  userMessagePreview: string
  groupToolCallId: string
  createdAt: string
  status: TodoGroupStatus
  todos: TodoTraceItem[]
}

export interface ReadyTaskTraceSnapshot {
  status: 'ready'
  todoGroups: TodoGroup[]
  errorCode?: never
}

export interface UnavailableTaskTraceSnapshot {
  status: 'unavailable'
  todoGroups: []
  errorCode: TaskTraceErrorCode
}

export type TaskTraceSnapshot =
  | ReadyTaskTraceSnapshot
  | UnavailableTaskTraceSnapshot

const SNAPSHOT_KEYS = new Set(['status', 'todoGroups', 'errorCode'])
const GROUP_KEYS = new Set([
  'id',
  'userMessageId',
  'userMessagePreview',
  'groupToolCallId',
  'createdAt',
  'status',
  'todos',
])
const TODO_KEYS = new Set(['id', 'content', 'status'])
const GROUP_STATUSES = new Set<TodoGroupStatus>([
  'running',
  'completed',
  'incomplete',
  'failed',
  'cancelled',
])
const TODO_STATUSES = new Set<TodoTraceItemStatus>([
  'pending',
  'running',
  'completed',
  'incomplete',
  'failed',
  'cancelled',
])
const ERROR_CODES = new Set<TaskTraceErrorCode>([
  'trace_incomplete',
  'todo_state_omitted',
  'todo_state_invalid',
])

const isRecord = (value: unknown): value is Record<string, unknown> => (
  value !== null && typeof value === 'object' && !Array.isArray(value)
)

const hasOnlyKeys = (value: Record<string, unknown>, keys: Set<string>) => (
  Object.keys(value).every((key) => keys.has(key))
)

const isCanonicalText = (value: unknown) => (
  typeof value === 'string' && value.length > 0 && value === value.trim()
)

const isVisibleText = (value: unknown) => (
  typeof value === 'string' && value.trim().length > 0
)

const isUtcInstant = (value: unknown) => (
  typeof value === 'string'
  && value.endsWith('Z')
  && Number.isFinite(Date.parse(value))
)

const isTodoTraceItem = (value: unknown): value is TodoTraceItem => {
  if (!isRecord(value) || !hasOnlyKeys(value, TODO_KEYS)) return false
  return isCanonicalText(value.id)
    && isVisibleText(value.content)
    && typeof value.status === 'string'
    && TODO_STATUSES.has(value.status as TodoTraceItemStatus)
}

const isTodoGroup = (value: unknown): value is TodoGroup => {
  if (!isRecord(value) || !hasOnlyKeys(value, GROUP_KEYS)) return false
  if (
    !isCanonicalText(value.id)
    || !isCanonicalText(value.userMessageId)
    || !isVisibleText(value.userMessagePreview)
    || Array.from(value.userMessagePreview as string).length > 161
    || !isCanonicalText(value.groupToolCallId)
    || !isUtcInstant(value.createdAt)
    || typeof value.status !== 'string'
    || !GROUP_STATUSES.has(value.status as TodoGroupStatus)
    || !Array.isArray(value.todos)
    || !value.todos.every(isTodoTraceItem)
  ) return false

  const todoIds = new Set<string>()
  for (const todo of value.todos) {
    if (todoIds.has(todo.id)) return false
    todoIds.add(todo.id)
  }
  return true
}

/** 严格校验当前任务轨迹 wire 契约，并保留原始对象图 */
export const parseTaskTraceSnapshot = (value: unknown): TaskTraceSnapshot => {
  if (!isRecord(value) || !hasOnlyKeys(value, SNAPSHOT_KEYS)) {
    throw new TypeError('任务轨迹响应格式无效')
  }
  if (!Array.isArray(value.todoGroups) || !value.todoGroups.every(isTodoGroup)) {
    throw new TypeError('任务轨迹分组格式无效')
  }

  const groupIds = new Set<string>()
  let previousCreatedAt = Number.POSITIVE_INFINITY
  for (const group of value.todoGroups) {
    if (groupIds.has(group.id)) throw new TypeError('任务轨迹分组身份重复')
    groupIds.add(group.id)
    const createdAt = Date.parse(group.createdAt)
    if (createdAt > previousCreatedAt) throw new TypeError('任务轨迹分组顺序无效')
    previousCreatedAt = createdAt
  }

  if (value.status === 'ready') {
    if (Object.hasOwn(value, 'errorCode')) {
      throw new TypeError('可用任务轨迹不能包含错误码')
    }
    return value as unknown as ReadyTaskTraceSnapshot
  }
  if (
    value.status !== 'unavailable'
    || value.todoGroups.length !== 0
    || typeof value.errorCode !== 'string'
    || !ERROR_CODES.has(value.errorCode as TaskTraceErrorCode)
  ) {
    throw new TypeError('不可用任务轨迹缺少稳定错误码')
  }
  return value as unknown as UnavailableTaskTraceSnapshot
}
