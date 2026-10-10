import { useEffect, useRef, useState } from 'react'

import { fetchRunCalendar, fetchRunPage, fetchTaskPage, type Page } from './api'
import { watchResource } from '../../api/shared/watchResource'
import { dateBoundary, isRunInProgress, shiftDate, type AutomationRun, type AutomationTask } from './model'

interface Snapshot {
  tasks: AutomationTask[]
  runs: AutomationRun[]
  cursors: Record<string, string | null>
  loading: boolean
  error: boolean
}
const initial: Snapshot = { tasks: [], runs: [], cursors: {}, loading: true, error: false }

async function readPages<T>(read: (cursor?: string) => Promise<Page<T>>, count: number, first?: Page<T>): Promise<Page<T>> {
  const result = first ?? await read()
  const items = [...result.items]
  let nextCursor = result.nextCursor
  for (let index = 1; index < count && nextCursor; index += 1) {
    const next = await read(nextCursor)
    items.push(...next.items)
    nextCursor = next.nextCursor
  }
  return { items, nextCursor }
}

/** 周历一次读取七天，追加分页只读取所选分组；筛选或离页会取消旧请求 */
export function useAutomation({ projectId, page, query, status, dates, view, onLoadError }: {
  projectId: string
  page: 'tasks' | 'history'
  query: string
  status: string
  dates: string[]
  view: 'week' | 'list'
  onLoadError?: () => void
}) {
  const search = query.trim()
  const dateKey = dates.join(',')
  const key = JSON.stringify([projectId, page, search, status, dateKey, view])
  const [snapshot, setSnapshot] = useState<{ key: string; value: Snapshot }>({ key, value: initial })
  const actions = useRef<{ reload: () => void; loadMore: (group: string) => void } | null>(null)
  const latestLoadError = useRef(onLoadError)
  latestLoadError.current = onLoadError
  useEffect(() => {
    const days = dateKey.split(',')
    const counts: Record<string, number> = {}
    const base = { projectId, query: search || undefined, status: status === 'all' ? undefined : status }
    let value = initial
    let baseline: AbortSignal | null = null
    let pagination: AbortController | null = null
    let watch: ReturnType<typeof watchResource<Snapshot>> | undefined
    const publish = (next: Snapshot) => { value = next; setSnapshot({ key, value }) }
    const fail = () => { publish({ ...value, loading: false, error: true }); latestLoadError.current?.() }
    const readRuns = (day: string, signal: AbortSignal, cursor?: string) => fetchRunPage({
      ...base, from: dateBoundary(day),
      until: dateBoundary(shiftDate(view === 'week' ? day : days[6], 1)), cursor,
    }, signal)
    const start = () => {
      watch = watchResource({
        matches: change => change.topic === (page === 'tasks' ? 'automation.task.changed' : 'automation.execution.changed'),
        read: async signal => {
          pagination?.abort()
          baseline = null
          publish({ ...value, loading: true, error: false })
          if (page === 'tasks') {
            const tasks = await readPages(cursor => fetchTaskPage({ ...base, cursor }, signal), counts.tasks ?? 1)
            return { ...initial, tasks: tasks.items, cursors: { tasks: tasks.nextCursor }, loading: false }
          }
          const firstPages = view === 'week' ? await fetchRunCalendar({ ...base, weekStart: days[0] }, signal)
            : [{ date: days[0], ...await readRuns(days[0], signal) }]
          const groups = await Promise.all(firstPages.map(async first => ({
            date: first.date,
            ...await readPages(cursor => readRuns(first.date, signal, cursor), counts[first.date] ?? 1, first),
          })))
          return { ...initial, runs: groups.flatMap(group => group.items),
            cursors: Object.fromEntries(groups.map(group => [group.date, group.nextCursor])), loading: false }
        },
        update: (next, signal) => { baseline = signal; publish(next) },
        refreshWhile: next => next.runs.some(isRunInProgress),
        onError: fail,
      })
    }
    const loadMore = async (group: string) => {
      const cursor = value.cursors[group]
      if (!cursor || value.loading || value.error || !baseline || baseline.aborted || document.hidden) return
      const controller = new AbortController()
      pagination = controller
      // 分页归属于已显示的基线；刷新、隐藏或身份切换会同时使其失效
      const signal = AbortSignal.any([baseline, controller.signal])
      publish({ ...value, loading: true })
      try {
        let update: Snapshot
        if (page === 'tasks') {
          const next = await fetchTaskPage({ ...base, cursor }, signal)
          const tasks = [...value.tasks, ...next.items]
          update = { ...value, tasks: [...new Map(tasks.map(task => [task.id, task])).values()],
            cursors: { ...value.cursors, [group]: next.nextCursor }, loading: false }
        } else {
          const next = await readRuns(group, signal, cursor)
          const runs = [...value.runs, ...next.items]
          update = { ...value, runs: [...new Map(runs.map(run => [run.id, run])).values()],
            cursors: { ...value.cursors, [group]: next.nextCursor }, loading: false }
        }
        if (signal.aborted) return
        counts[group] = (counts[group] ?? 1) + 1
        watch?.update(update)
      } catch { if (!signal.aborted) fail() }
    }
    const debounce = search ? setTimeout(start, 250) : undefined
    if (!search) start()
    actions.current = { reload: () => watch?.refresh(), loadMore: group => { void loadMore(group) } }
    return () => { clearTimeout(debounce); watch?.close(); pagination?.abort(); actions.current = null }
  }, [projectId, key, page, search, status, dateKey, view])

  return { ...(snapshot.key === key ? snapshot.value : initial),
    reload: () => actions.current?.reload(),
    loadMore: (group: string) => actions.current?.loadMore(group),
  }
}
