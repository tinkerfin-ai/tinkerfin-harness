import { useCallback, useEffect, useRef, useState } from 'react'
import { emptySkillFilters, type SkillFilters, type SkillView } from './model'

function readLocation() {
  const params = new URLSearchParams(location.search)
  const view: SkillView = params.get('skillView') === 'mine' ? 'mine' : 'discover'
  const source = params.get('skillSource') ?? (view === 'mine' ? 'all' : '')
  const filters: SkillFilters = {
    query: params.get('skillQuery') ?? '', category: params.get('skillCategory') ?? '',
    sort: params.get('skillSort') === 'name' ? 'name' : 'updated',
    status: params.get('skillStatus') === 'enabled' ? 'enabled' : params.get('skillStatus') === 'disabled' ? 'disabled' : 'all',
  }
  return { view, source, filters }
}

/** 每个页签和来源各自保存筛选与阅读位置，地址栏记录当前视图 */
export function useSkillsNavigation() {
  const [initial] = useState(readLocation)
  const [view, setView] = useState(initial.view)
  const [sources, setSources] = useState({ discover: '', mine: 'all', [initial.view]: initial.source })
  const [filters, setFilters] = useState<Record<string, SkillFilters>>({ [`${initial.view}:${initial.source}`]: initial.filters })
  const scroll = useRef(new Map<string, number>())
  const source = sources[view]
  const key = `${view}:${source}`
  const current = filters[key] ?? emptySkillFilters
  useEffect(() => {
    const restore = () => {
      if (new URLSearchParams(location.search).get('page') !== 'skills') return
      const next = readLocation()
      setView(next.view)
      setSources(previous => ({ ...previous, [next.view]: next.source }))
      setFilters(previous => ({ ...previous, [`${next.view}:${next.source}`]: next.filters }))
    }
    window.addEventListener('popstate', restore)
    return () => window.removeEventListener('popstate', restore)
  }, [])

  const write = useCallback((nextView: SkillView, nextSource: string, next: SkillFilters, replace = false) => {
    const url = new URL(location.href)
    url.searchParams.set('page', 'skills')
    url.searchParams.delete('thread')
    const values = { skillView: nextView, skillSource: nextSource, skillQuery: next.query, skillSort: next.sort, skillStatus: next.status, skillCategory: next.category }
    Object.entries(values).forEach(([name, value]) => value ? url.searchParams.set(name, value) : url.searchParams.delete(name))
    const address = `${url.pathname}${url.search}`
    if (address !== `${location.pathname}${location.search}`) window.history[replace ? 'replaceState' : 'pushState'](null, '', address)
  }, [])

  const selectSource = (next: string, replace = false) => {
    const nextFilters = filters[`${view}:${next}`] ?? (source ? emptySkillFilters : current)
    setSources(previous => ({ ...previous, [view]: next }))
    setFilters(previous => ({ ...previous, [`${view}:${next}`]: nextFilters }))
    write(view, next, nextFilters, replace)
  }
  const selectView = (next: SkillView) => {
    setView(next)
    write(next, sources[next], filters[`${next}:${sources[next]}`] ?? emptySkillFilters)
  }
  const changeFilters = (patch: Partial<SkillFilters>) => {
    const next = { ...current, ...patch }
    setFilters(previous => ({ ...previous, [key]: next }))
    scroll.current.set(key, 0)
    write(view, source, next, 'query' in patch)
  }
  return { view, source, key, filters: current, scroll, selectSource, selectView, changeFilters }
}
