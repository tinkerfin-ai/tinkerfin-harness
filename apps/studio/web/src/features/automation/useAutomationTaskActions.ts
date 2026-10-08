import { useEffect, useRef, useState } from 'react'
import type { ToastHandler } from '../../components/ui/ToastViewport'
import { useI18n } from '../../i18n'
import { batchTasks, commandTask } from './api'
import type { AutomationTask } from './model'

interface TaskDeletion {
  tasks: AutomationTask[]
  trigger: HTMLElement
}

/** 任务操作拥有请求、重试身份和删除确认；离开页面时取消请求并完成待确认结果 */
export function useAutomationTaskActions(projectId: string, tasks: AutomationTask[], reload: () => void, onToast: ToastHandler) {
  const { t } = useI18n()
  const [busy, setBusy] = useState(new Set<string>())
  const [deletion, setDeletion] = useState<TaskDeletion | null>(null)
  const busyRef = useRef(new Set<string>())
  const owned = useRef(new Set<AbortController>())
  const commandIds = useRef(new Map<string, string>())
  const mounted = useRef(true)
  const pendingDeletion = useRef<{ intent: TaskDeletion; resolve: (failed: ReadonlySet<string>) => void } | null>(null)

  useEffect(() => {
    mounted.current = true
    const requests = owned.current
    return () => {
      mounted.current = false
      for (const request of requests) request.abort()
      const pending = pendingDeletion.current
      pending?.resolve(new Set(pending.intent.tasks.map(task => task.id)))
      pendingDeletion.current = null
    }
  }, [])

  const requestId = (key: string) => {
    if (!commandIds.current.has(key)) commandIds.current.set(key, crypto.randomUUID())
    return commandIds.current.get(key)!
  }
  const begin = (ids: string[]) => {
    if (ids.some(id => busyRef.current.has(id))) return null
    ids.forEach(id => busyRef.current.add(id))
    setBusy(new Set(busyRef.current))
    const controller = new AbortController()
    owned.current.add(controller)
    return controller
  }
  const finish = (ids: string[], controller: AbortController) => {
    owned.current.delete(controller)
    ids.forEach(id => busyRef.current.delete(id))
    if (mounted.current) setBusy(new Set(busyRef.current))
  }
  const execute = async (id: string, operation: 'pause' | 'enable' | 'run') => {
    const task = tasks.find(item => item.id === id)
    if (!task) return
    const controller = begin([id])
    if (!controller) return
    const key = `${id}:${task.revision}:${operation}`
    try {
      await commandTask(task, operation, requestId(key), controller.signal)
      if (controller.signal.aborted || !mounted.current) return
      commandIds.current.delete(key)
      onToast('info', t(operation === 'run' ? '已加入运行队列' : operation === 'pause' ? '任务已暂停' : '任务已启用'))
      reload()
    } catch (error) {
      if (!controller.signal.aborted && mounted.current) onToast('error', error instanceof Error ? error.message : t('操作失败，请重试'))
    } finally { finish([id], controller) }
  }
  const batch = async (selected: AutomationTask[], operation: 'pause' | 'delete'): Promise<ReadonlySet<string>> => {
    const ids = selected.map(task => task.id)
    if (!ids.length) return new Set()
    const controller = begin(ids)
    if (!controller) return new Set(ids)
    const keys = Object.fromEntries(selected.map(task => [task.id, `${task.id}:${task.revision}:${operation}`]))
    try {
      const result = await batchTasks(projectId, selected, operation, Object.fromEntries(ids.map(id => [id, requestId(keys[id])])), controller.signal)
      if (controller.signal.aborted || !mounted.current) return new Set(ids)
      result.filter(item => item.succeeded).forEach(item => commandIds.current.delete(keys[item.taskId]))
      const failed = result.filter(item => !item.succeeded)
      onToast(failed.length ? 'error' : 'info', failed.length ? t('有 {count} 个任务操作失败，请重试', { count: failed.length }) : t(operation === 'delete' ? '所选任务已删除，运行历史已保留' : '已暂停所选任务'))
      reload()
      return new Set(failed.map(item => item.taskId))
    } catch (error) {
      if (!controller.signal.aborted && mounted.current) onToast('error', error instanceof Error ? error.message : t('操作失败，请重试'))
      return new Set(ids)
    } finally { finish(ids, controller) }
  }
  const requestDelete = (ids: ReadonlySet<string>, trigger: HTMLElement): Promise<ReadonlySet<string>> => {
    if (pendingDeletion.current) return Promise.resolve(new Set(ids))
    const intent = { tasks: tasks.filter(task => ids.has(task.id)), trigger }
    if (!intent.tasks.length) return Promise.resolve(new Set())
    return new Promise(resolve => { pendingDeletion.current = { intent, resolve }; setDeletion(intent) })
  }
  const completeDeletion = (pending: NonNullable<typeof pendingDeletion.current>, failed: ReadonlySet<string>) => {
    if (pendingDeletion.current !== pending) return
    pending.resolve(failed)
    pendingDeletion.current = null
    if (mounted.current) setDeletion(null)
  }
  const cancelDelete = () => {
    const pending = pendingDeletion.current
    if (pending && !busyRef.current.size) completeDeletion(pending, new Set(pending.intent.tasks.map(task => task.id)))
  }
  const confirmDelete = async () => {
    const pending = pendingDeletion.current
    if (pending && !busyRef.current.size) completeDeletion(pending, await batch(pending.intent.tasks, 'delete'))
  }
  return {
    busy, deletion, execute, requestDelete, cancelDelete, confirmDelete,
    pause: (ids: ReadonlySet<string>) => batch(tasks.filter(task => ids.has(task.id)), 'pause'),
  }
}
