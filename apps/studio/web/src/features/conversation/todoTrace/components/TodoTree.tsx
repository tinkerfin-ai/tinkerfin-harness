import { CircleCheck, Circle, LoaderCircle, X } from 'lucide-react'

import type {
  TodoGroup,
  TodoTraceItemStatus,
} from '../../../../api/conversation/taskTrace'
import { useI18n } from '../../../../i18n'

const statusIcon = (status: TodoTraceItemStatus) => {
  if (status === 'completed') {
    return (
      <CircleCheck
        size={16}
        strokeWidth={1.5}
        className="todo-trace-completed-mark"
        aria-hidden="true"
      />
    )
  }
  if (status === 'running') {
    return <LoaderCircle size={16} className="todo-trace-spin" aria-hidden="true" />
  }
  if (status === 'failed' || status === 'cancelled') {
    return <X size={16} aria-hidden="true" />
  }
  return <Circle size={16} strokeWidth={1.5} aria-hidden="true" />
}

export function TodoTree({ group }: { group: TodoGroup }) {
  const { t } = useI18n()
  const labels: Record<TodoTraceItemStatus, string> = {
    pending: t('待执行'),
    running: t('执行中'),
    completed: t('已完成'),
    failed: t('失败'),
    cancelled: t('已取消'),
  }

  return (
    <ul className="todo-trace-todo-list" aria-label={t('任务列表')}>
      {group.todos.map((todo) => (
        <li key={todo.id} className={`todo-trace-todo is-${todo.status}`}>
          <span className="todo-trace-node-icon" aria-hidden="true">
            {statusIcon(todo.status)}
          </span>
          <span className="todo-trace-todo-content">{todo.content}</span>
          {todo.status !== 'completed' && todo.status !== 'pending' && (
            <span className="todo-trace-status-label">{labels[todo.status]}</span>
          )}
        </li>
      ))}
    </ul>
  )
}
