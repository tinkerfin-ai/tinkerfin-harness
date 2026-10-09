import { useEffect, useMemo, useRef, useState } from 'react'

import { fetchRunPage, fetchTaskPage, type Page } from './api'
import { watchResource } from '../../api/shared/watchResource'
import { dateBoundary, shiftDate, type AutomationRun, type AutomationTask } from './model'

interface Snapshot {
  tasks: AutomationTask[]
  runs: AutomationRun[]
  cursors: Record<string, string | null>
  loading: boolean
  error: boolean
}
const initial: Snapshot = { tasks: [], runs: [], cursors: {}, loading: true, error: false }

/** 按当前视图读取服务端分页；刷新已加载页，隐藏或离开页面时取消请求 */
export function useAutomation({ projectId, page, query, status, dates, view, onLoadError }: {
  projectId: string
  page: 'tasks' | 'history'
  query: string
  status: string
  dates: string[]
  view: 'week' | 'list'
  onLoadError?: () => void
}) {
  const key = JSON.stringify([projectId, page, query, status, dates, view])
  const [pages, setPages] = useState<{ key: string; counts: Record<string, number> }>({ key: '', counts: {} })
  const [revision, setRevision] = useState(0)
  const [snapshot, setSnapshot] = useState<Snapshot>(initial)
  const latestLoadError = useRef(onLoadError)
  latestLoadError.current = onLoadError
  const displayedKey = useRef('')
  const pageCounts = JSON.stringify(pages.key === key ? pages.counts : {})
  const dateKey = dates.join(',')
  useEffect(() => {
    const days = dateKey.split(',')
    const counts = JSON.parse(pageCounts) as Record<string, number>
    let first = true
    const watch = watchResource({
      matches: change => change.topic === 'automation.task.changed' || change.topic === 'automation.execution.changed',
      read: async (signal) => {
        if (first) {
          if (displayedKey.current !== key) setSnapshot({ ...initial })
          else setSnapshot(current => ({ ...current, loading: true, error: false }))
          displayedKey.current = key
        }
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
        const base = { projectId, query: query.trim() || undefined, from: dateBoundary(days[0]), until: dateBoundary(shiftDate(days[6], 1)) }
        const taskRead = page === 'tasks' ? readPages(cursor => fetchTaskPage({ projectId, query: base.query, status: selectedStatus, cursor }, signal), counts.tasks ?? 1) : Promise.resolve({ items: [], nextCursor: null })
        const runKeys = page === 'history' ? view === 'week' ? days : [days[0]] : []
        const [tasks, runs] = await Promise.all([
          taskRead,
          Promise.all(runKeys.map(async day => ({ day, ...await readPages(cursor => fetchRunPage({ ...base, from: dateBoundary(day), until: view === 'week' ? dateBoundary(shiftDate(day, 1)) : base.until, status: selectedStatus, cursor }, signal), counts[day] ?? 1) }))),
        ])
        return { tasks: tasks.items, runs: runs.flatMap(group => group.items),
          cursors: Object.fromEntries([['tasks', tasks.nextCursor], ...runs.map(group => [group.day, group.nextCursor])]), loading: false, error: false }
      },
      update: value => { setSnapshot(value); first = false },
      onError: () => {
        setSnapshot(current => ({ ...current, loading: false, error: true }))
        latestLoadError.current?.()
      },
    })
    return watch.close
  }, [projectId, key, pageCounts, revision, page, query, status, dateKey, view])

  return useMemo(() => ({ ...snapshot,
    reload: () => setRevision(value => value + 1),
    loadMore: (group: string) => setPages(current => ({ key, counts: { ...(current.key === key ? current.counts : {}), [group]: (current.key === key ? current.counts[group] ?? 1 : 1) + 1 } })),
  }), [snapshot, key])
}
