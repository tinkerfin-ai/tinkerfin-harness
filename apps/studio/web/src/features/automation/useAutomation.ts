import { useEffect, useMemo, useRef, useState } from 'react'

import { fetchCounts, fetchRunPage, fetchTaskPage, type Page } from './api'
import { dateBoundary, isActiveRun, shiftDate, type AutomationRun, type AutomationTask } from './model'

interface Snapshot {
  tasks: AutomationTask[]
  runs: AutomationRun[]
  counts: Record<string, number>
  cursors: Record<string, string | null>
  loading: boolean
  error: boolean
}
const initial: Snapshot = { tasks: [], runs: [], counts: {}, cursors: {}, loading: true, error: false }

/** 按当前视图读取服务端分页；刷新已加载页，隐藏或离开页面时取消请求 */
export function useAutomation({ page, query, status, dates, view }: {
  page: 'tasks' | 'history'
  query: string
  status: string
  dates: string[]
  view: 'week' | 'list'
}) {
  const key = JSON.stringify([page, query, status, dates, view])
  const [pages, setPages] = useState<{ key: string; counts: Record<string, number> }>({ key: '', counts: {} })
  const [revision, setRevision] = useState(0)
  const [snapshot, setSnapshot] = useState<Snapshot>(initial)
  const displayedKey = useRef('')
  const pageCounts = JSON.stringify(pages.key === key ? pages.counts : {})
  const dateKey = dates.join(',')
  useEffect(() => {
    const days = dateKey.split(',')
    const counts = JSON.parse(pageCounts) as Record<string, number>
    let closed = false
    let pending: AbortController | null = null
    let timer: ReturnType<typeof setTimeout> | undefined
    let first = true

    const load = async () => {
      if (closed || document.hidden) return
      pending?.abort()
      const controller = new AbortController()
      pending = controller
      if (first) {
        if (displayedKey.current !== key) setSnapshot({ ...initial })
        else setSnapshot(current => ({ ...current, loading: true, error: false }))
        displayedKey.current = key
      }
      try {
        async function readPages<T>(read: (cursor: string | undefined) => Promise<Page<T>>, count: number) {
          const items: T[] = []
          let cursor: string | undefined
          let nextCursor: string | null = null
          for (let index = 0; index < count; index += 1) {
            const result = await read(cursor)
            items.push(...result.items)
            nextCursor = result.nextCursor
            if (!nextCursor) break
            cursor = nextCursor
          }
          return { items, nextCursor }
        }
        const selectedStatus = status === 'all' ? undefined : status
        const base = { query: query.trim() || undefined, from: dateBoundary(days[0]), until: dateBoundary(shiftDate(days[6], 1)) }
        const taskRead = page === 'tasks' ? readPages(cursor => fetchTaskPage({ query: base.query, status: selectedStatus, cursor }, controller.signal), counts.tasks ?? 1) : Promise.resolve({ items: [], nextCursor: null })
        const runKeys = page === 'history' ? view === 'week' ? days : [days[0]] : []
        const [tasks, runs, totals] = await Promise.all([
          taskRead,
          Promise.all(runKeys.map(async day => ({ day, ...await readPages(cursor => fetchRunPage({ ...base, from: dateBoundary(day), until: view === 'week' ? dateBoundary(shiftDate(day, 1)) : base.until, status: selectedStatus, cursor }, controller.signal), counts[day] ?? 1) }))),
          fetchCounts(page === 'tasks' ? 'tasks' : 'runs', page === 'tasks' ? { query: base.query } : base, controller.signal),
        ])
        if (closed || controller.signal.aborted) return
        setSnapshot({ tasks: tasks.items, runs: runs.flatMap(group => group.items), counts: totals, cursors: Object.fromEntries([['tasks', tasks.nextCursor], ...runs.map(group => [group.day, group.nextCursor])]), loading: false, error: false })
        first = false
        const active = runs.some(group => group.items.some(run => isActiveRun(run.status)))
        timer = setTimeout(() => void load(), active ? 2000 : 10000)
      } catch {
        if (closed || controller.signal.aborted) return
        controller.abort()
        setSnapshot(current => ({ ...current, loading: false, error: true }))
      }
    }
    const visibility = () => {
      clearTimeout(timer)
      if (document.hidden) pending?.abort()
      else void load()
    }
    void load()
    document.addEventListener('visibilitychange', visibility)
    return () => { closed = true; pending?.abort(); clearTimeout(timer); document.removeEventListener('visibilitychange', visibility) }
  }, [key, pageCounts, revision, page, query, status, dateKey, view])

  return useMemo(() => ({ ...snapshot,
    reload: () => setRevision(value => value + 1),
    loadMore: (group: string) => setPages(current => ({ key, counts: { ...(current.key === key ? current.counts : {}), [group]: (current.key === key ? current.counts[group] ?? 1 : 1) + 1 } })),
  }), [snapshot, key])
}
