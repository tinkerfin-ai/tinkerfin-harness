import type {
  TaskTraceSnapshot,
  TodoGroup,
  TodoTraceItem,
  TodoTraceItemStatus,
} from '../../../api/conversation/taskTrace'
import type { DeepReadonly, JsonValue } from '../../../types'

export interface RootTodoValue {
  id?: string
  content: string
  status: 'pending' | 'in_progress' | 'completed'
}

const INPUT_STATUSES = new Set<RootTodoValue['status']>([
  'pending',
  'in_progress',
  'completed',
])

export const todoGroupId = (originRunId: string) => `todo-group:${originRunId}`

export const normalizeUserMessagePreview = (content: string) => {
  const normalized = content.trim().replace(/\s+/gu, ' ')
  const characters = Array.from(normalized)
  return characters.length <= 160
    ? normalized
    : `${characters.slice(0, 160).join('')}…`
}

export const parseRootTodos = (value: DeepReadonly<JsonValue>): RootTodoValue[] | null => {
  if (!Array.isArray(value)) return null
  const result: RootTodoValue[] = []
  const identifiers = new Set<string>()
  for (const item of value) {
    if (!item || typeof item !== 'object' || Array.isArray(item)) return null
    const content = item.content
    const status = item.status
    const id = item.id
    if (
      typeof content !== 'string'
      || !content.trim()
      || typeof status !== 'string'
      || !INPUT_STATUSES.has(status as RootTodoValue['status'])
      || (id !== undefined && (
        typeof id !== 'string'
        || !id
        || id !== id.trim()
      ))
    ) return null
    if (typeof id === 'string') {
      if (identifiers.has(id)) return null
      identifiers.add(id)
    }
    result.push({
      ...(typeof id === 'string' ? { id } : {}),
      content,
      status: status as RootTodoValue['status'],
    })
  }
  return result
}

const outputStatus = (
  status: RootTodoValue['status'],
  groupStatus: TodoGroup['status'],
): TodoTraceItemStatus => {
  if (status === 'completed') return 'completed'
  if (status === 'pending') return 'pending'
  if (groupStatus === 'failed') return 'failed'
  if (groupStatus === 'cancelled') return 'cancelled'
  if (groupStatus === 'incomplete' || groupStatus === 'completed') return 'incomplete'
  return 'running'
}

/** 运行结束不代表模型已确认清单中的每一项完成 */
export const resolveTodoGroupStatus = (
  todos: readonly RootTodoValue[],
  status: TodoGroup['status'],
): TodoGroup['status'] => (
  status === 'completed' || status === 'incomplete'
    ? todos.every(todo => todo.status === 'completed') ? 'completed' : 'incomplete'
    : status
)

export const projectTodoItems = (
  todos: readonly RootTodoValue[],
  groupId: string,
  groupStatus: TodoGroup['status'],
): TodoTraceItem[] => todos.map((todo, index) => ({
  id: todo.id ?? `${groupId}:todo:${index}`,
  content: todo.content,
  status: outputStatus(todo.status, groupStatus),
}))

export const todoProgress = (group: TodoGroup) => ({
  completed: group.todos.filter((todo) => todo.status === 'completed').length,
  total: group.todos.length,
})

export const semanticTaskTrace = (snapshot: TaskTraceSnapshot) => ({
  status: snapshot.status,
  ...(snapshot.status === 'unavailable' ? { errorCode: snapshot.errorCode } : {}),
  todoGroups: snapshot.todoGroups.map((group) => ({
    id: group.id,
    userMessagePreview: group.userMessagePreview,
    status: group.status,
    todos: group.todos,
  })),
})
