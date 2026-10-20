import { ListChecks } from 'lucide-react'
import { useState } from 'react'

import type { TodoGroup } from '../../../../api/conversation/taskTrace'
import type { Message } from '../../../../types'
import { ToolCallRow } from '../../components/ToolCallRow'
import { todoProgress } from '../domain'
import { TodoTree } from './TodoTree'

export function TodoGroupRow({
  group,
  message,
}: {
  group: TodoGroup
  message: Message
}) {
  const [open, setOpen] = useState(false)
  const progress = todoProgress(group)

  return (
    <ToolCallRow
      message={message}
      className="todo-trace-update-row"
      open={open}
      onOpenChange={setOpen}
      presentationOverride={{
        title: 'Todos',
        summary: `${progress.activeOrCompleted}/${progress.total}`,
        icon: <ListChecks size={14} strokeWidth={2} />,
      }}
    >
      <div className="todo-trace-update-body">
        <TodoTree group={group} />
      </div>
    </ToolCallRow>
  )
}
