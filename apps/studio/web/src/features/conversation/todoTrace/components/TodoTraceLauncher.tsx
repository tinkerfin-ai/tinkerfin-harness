import { ListChecks, TriangleAlert } from 'lucide-react'
import { forwardRef } from 'react'

import { useI18n } from '../../../../i18n'
import type { WebTaskTraceViewState } from '../../../../types'
import { IconButton } from '../../../../components/ui'

export const TodoTraceLauncher = forwardRef<HTMLButtonElement, {
  taskTrace: WebTaskTraceViewState
  open: boolean
  loadFailed: boolean
  onToggle: () => void
  onRetry: () => void
}>(function TodoTraceLauncher({
  taskTrace,
  open,
  loadFailed,
  onToggle,
  onRetry,
}, ref) {
  const { t } = useI18n()
  if (open || (taskTrace.phase === 'unloaded' && !loadFailed)) return null
  if (taskTrace.phase === 'loading') {
    return (
      <IconButton
        ref={ref}
        size="xs" loading
        className="todo-trace-launcher"
        label={t('正在加载任务轨迹')}
        icon={<ListChecks size={17} />}
      />
    )
  }
  if (taskTrace.phase === 'unavailable' || loadFailed) {
    return (
      <IconButton
        ref={ref}
        size="xs"
        className="todo-trace-launcher"
        label={t('重试任务轨迹')}
        tooltip={t('重试任务轨迹')}
        icon={<TriangleAlert size={17} />}
        onClick={onRetry}
      />
    )
  }
  if (taskTrace.phase !== 'ready' || taskTrace.snapshot.todoGroups.length === 0) {
    return null
  }
  const count = taskTrace.snapshot.todoGroups.length
  return (
    <IconButton
      ref={ref}
      size="xs"
      className="todo-trace-launcher"
      aria-expanded={open}
      aria-controls="todo-trace-drawer"
      label={t('任务轨迹 {count}', { count })}
      tooltip={t('任务轨迹')}
      icon={<><ListChecks size={17} /><span>{count}</span></>}
      onClick={onToggle}
    />
  )
})
