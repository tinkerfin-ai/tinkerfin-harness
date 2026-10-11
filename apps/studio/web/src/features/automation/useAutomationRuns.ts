import { useEffect, useRef, useState } from 'react'
import { getResourceConnectionState, subscribeResourceChanges, type ResourceChange } from '../../api/notifications'
import { ApiError } from '../../api/shared/http'
import { watchResource } from '../../api/shared/watchResource'
import { getAuthorizationHeader, subscribeAuthSession } from '../../auth/session'
import { getServerAddress, subscribeServerAddress } from '../../api/shared/config'
import type { ToastHandler } from '../../components/ui/ToastViewport'
import { useI18n } from '../../i18n'
import { executeTask, fetchActiveRunPage, fetchRunStatus, fetchSubmission } from './api'
import { isRunInProgress, runStatusLabels, type AutomationRunRecord, type AutomationTask, type RunStatus } from './model'

interface Submission { task: AutomationTask; requestId: string; pending: boolean; epoch: number }
export type TaskExecutionState = RunStatus | 'submitting' | 'uncertain'
export interface AutomationRuns {
  states: ReadonlyMap<string, TaskExecutionState>
  syncError: boolean
  execute: (task: AutomationTask) => Promise<void>
  refresh: () => void
}

/** 登录工作区确认本页提交并跟踪在线执行；重连只刷新基线，不补发离线完成提示 */
export function useAutomationRuns(userId: number, projectIds: string[], onToast: ToastHandler | undefined): AutomationRuns {
  const { t } = useI18n()
  const latest = useRef({ projectIds, onToast, t })
  latest.current = { projectIds, onToast, t }
  const [states, setStates] = useState<ReadonlyMap<string, TaskExecutionState>>(new Map())
  const [syncError, setSyncError] = useState(false)
  const actions = useRef<Pick<AutomationRuns, 'execute' | 'refresh'> | null>(null)
  useEffect(() => {
    const runs = new Map<string, AutomationRunRecord>()
    const submissions = new Map<string, Submission>()
    const notified = new Set<string>()
    const requests = new Set<AbortController>()
    const pending = new Map<string, { projectId: string; live: boolean }>()
    let identity = `${getServerAddress()}\n${getAuthorizationHeader()}`
    let closed = false, baseline = true, epoch = 0
    let online = getResourceConnectionState() === 'ready' && !document.hidden
    const publish = () => {
      if (closed) return
      const next = new Map<string, TaskExecutionState>()
      // 同一任务可以有多次执行；任一次仍活跃时不能被另一条完成结果清除 loading
      for (const run of [...runs.values()].sort((a, b) => a.queuedAt.localeCompare(b.queuedAt))) {
        if (run.taskId && !['queued', 'running', 'cancel_requested'].includes(next.get(run.taskId) ?? '')) next.set(run.taskId, run.status)
      }
      for (const [id, submission] of submissions) next.set(id, submission.pending ? 'submitting' : 'uncertain')
      setStates(next)
    }
    const accept = (run: AutomationRunRecord, live: boolean) => {
      const previous = runs.get(run.id)
      if (previous && (Date.parse(previous.updatedAt) > Date.parse(run.updatedAt)
        || (previous.status !== 'queued' && run.status === 'queued')
        || (previous.status === 'cancel_requested' && run.status === 'running')
        || (!isRunInProgress(previous) && isRunInProgress(run)))) return
      runs.set(run.id, run)
      const key = `${run.id}:${run.status}`
      if (live && !isRunInProgress(run) && !notified.has(key)) {
        notified.add(key)
        if (notified.size > 1024) notified.delete(notified.values().next().value!)
        const { t, onToast } = latest.current
        onToast?.(run.status === 'succeeded' ? 'success' : run.status === 'cancelled' ? 'info'
          : run.status === 'needs_attention' || run.status === 'interrupted' ? 'warning' : 'error',
        t('{name}：{status}', { name: run.name || t('定时任务'), status: t(runStatusLabels[run.status]) }))
      }
      const completed = [...runs.values()].filter(item => !isRunInProgress(item))
      for (const item of completed.slice(0, Math.max(0, completed.length - 256))) runs.delete(item.id)
    }
    const confirmSubmission = (submission: Submission, run: AutomationRunRecord) => {
      // 回执与 POST 只确认它们所属的提交，迟到回复不能释放同一任务的新请求
      if (submissions.get(submission.task.id) !== submission) return
      submissions.delete(submission.task.id)
      accept(run, submission.epoch === epoch && !document.hidden)
      if (isRunInProgress(runs.get(run.id) ?? run)) latest.current.onToast?.('info', latest.current.t('已加入运行队列'))
    }
    const changedRun = (change: ResourceChange) => {
      if (change.topic === 'automation.execution.changed' && change.scope.namespace === 'studio_automation') {
        const projectId = latest.current.projectIds.find(id => change.scope.owner_id === `${userId}:${id}`)
        return projectId ? { id: change.key, projectId, live: online } : null
      }
      return null
    }
    const release = subscribeResourceChanges(notice => {
      if (notice.kind === 'connection' || notice.kind === 'resync') {
        online = notice.kind === 'resync' && !document.hidden
        epoch += 1
        baseline = true
        pending.clear()
      } else {
        const change = changedRun(notice.change)
        if (change) pending.set(change.id, { projectId: change.projectId, live: change.live || pending.get(change.id)?.live === true })
      }
    })
    const watch = watchResource({
      matches: change => changedRun(change) !== null,
      read: async signal => {
        const readEpoch = epoch
        const changes = new Map(pending)
        const unconfirmed = [...submissions.values()]
        const values: Array<{ run: AutomationRunRecord; live: boolean }> = []
        const receipts: Array<{ submission: Submission; run: AutomationRunRecord }> = []
        if (baseline) {
          let cursor: string | undefined
          const active = new Set<string>()
          do {
            const page = await fetchActiveRunPage(cursor, signal)
            for (const run of page.items) { active.add(run.id); values.push({ run, live: false }) }
            cursor = page.nextCursor ?? undefined
          } while (cursor && !signal.aborted)
          for (const run of runs.values()) if (isRunInProgress(run) && !active.has(run.id) && !changes.has(run.id)) changes.set(run.id, { projectId: run.projectId, live: false })
        } else {
          for (const run of runs.values()) if (isRunInProgress(run) && !changes.has(run.id)) changes.set(run.id, { projectId: run.projectId, live: false })
        }
        for (const [id, change] of changes) {
          if (signal.aborted) break
          values.push({ run: await fetchRunStatus(change.projectId, id, signal), live: change.live })
        }
        for (const submission of unconfirmed) {
          if (signal.aborted) break
          const run = await fetchSubmission(submission.task.projectId, submission.task.id, submission.requestId, signal)
          if (run) receipts.push({ submission, run })
        }
        return { values, receipts, readEpoch, changes }
      },
      update: ({ values, receipts, readEpoch, changes }) => {
        if (readEpoch !== epoch) return
        baseline = false
        // 只有成功应用的同周期查询才能消费事件，读取期间的新事件保留下一轮
        for (const [id, change] of changes) if (pending.get(id) === change) pending.delete(id)
        for (const { run, live } of values) accept(run, live)
        for (const { submission, run } of receipts) confirmSubmission(submission, run)
        setSyncError(false); publish()
      },
      refreshWhile: () => submissions.size > 0 || [...runs.values()].some(isRunInProgress),
      onError: () => { baseline = true; setSyncError(true) },
    })
    const reset = () => {
      identity = `${getServerAddress()}\n${getAuthorizationHeader()}`
      epoch += 1; baseline = true; online = false
      requests.forEach(request => request.abort()); requests.clear()
      runs.clear(); pending.clear(); submissions.clear(); notified.clear(); publish()
    }
    const releaseAuth = subscribeAuthSession(() => {
      if (identity !== `${getServerAddress()}\n${getAuthorizationHeader()}`) reset()
    })
    const releaseServer = subscribeServerAddress(reset)
    const visibility = () => { epoch += 1; baseline = true; pending.clear(); online = false }
    document.addEventListener('visibilitychange', visibility)
    actions.current = {
      refresh: watch.refresh,
      execute: async task => {
        if (closed || submissions.get(task.id)?.pending || (!submissions.has(task.id) && [...runs.values()].some(run => run.taskId === task.id && isRunInProgress(run)))) return
        // 未确认提交重用完整原命令；即使用户已编辑任务，也不能将同一请求 ID 配给新内容
        const submission = submissions.get(task.id) ?? { task: structuredClone(task), requestId: crypto.randomUUID(), pending: false, epoch }
        submission.pending = true; submission.epoch = epoch; submissions.set(task.id, submission); publish()
        const controller = new AbortController()
        requests.add(controller)
        const current = () => !closed && !controller.signal.aborted && submissions.get(task.id) === submission
        try {
          const response = executeTask(submission.task, submission.requestId, controller.signal)
          watch.refresh()
          const run = await response
          if (!current()) return
          confirmSubmission(submission, run)
          watch.refresh()
        } catch (error) {
          if (!current()) return
          submission.pending = false
          if (error instanceof ApiError && error.status >= 400 && error.status < 500) {
            submissions.delete(task.id)
            latest.current.onToast?.('error', error.message)
          } else latest.current.onToast?.('error', latest.current.t('暂时无法确认提交结果，请重试确认'))
        } finally { requests.delete(controller); if (!controller.signal.aborted) publish() }
      },
    }
    return () => {
      closed = true; actions.current = null
      requests.forEach(request => request.abort())
      watch.close(); release(); releaseAuth(); releaseServer()
      document.removeEventListener('visibilitychange', visibility)
    }
  }, [userId])
  return { states, syncError, execute: task => actions.current?.execute(task) ?? Promise.resolve(), refresh: () => actions.current?.refresh() }
}
