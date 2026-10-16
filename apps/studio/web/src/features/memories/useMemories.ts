import { useCallback, useEffect, useRef, useState } from 'react'
import { watchResource } from '../../api/shared/watchResource'
import { listMemories, type MemoryPage } from './api'

export function useMemories(projectId: string, query: string) {
  const search = query.trim()
  const [page, setPage] = useState<MemoryPage>({ items: [], nextOffset: null })
  const [status, setStatus] = useState<'loading' | 'ready' | 'error'>('loading')
  const [loadingMore, setLoadingMore] = useState(false)
  const [moreError, setMoreError] = useState(false)
  const watcher = useRef<ReturnType<typeof watchResource> | null>(null)
  const request = useRef<AbortController | null>(null)
  const baseline = useRef<AbortSignal | null>(null)
  useEffect(() => {
    setStatus('loading'); setMoreError(false); setLoadingMore(false)
    const start = () => { watcher.current = watchResource({
      read: signal => {
        request.current?.abort(); baseline.current = null; setLoadingMore(false)
        return listMemories(projectId, search, 0, signal)
      },
      matches: change => change.topic === 'studio.memories.changed' && change.key === projectId,
      update: (value, signal) => { baseline.current = signal; setPage(value); setStatus('ready') },
      onError: () => setStatus('error'),
    }) }
    const debounce = search ? setTimeout(start, 250) : undefined
    if (!search) start()
    return () => { clearTimeout(debounce); baseline.current = null; watcher.current?.close(); watcher.current = null; request.current?.abort() }
  }, [projectId, search])
  const more = useCallback(async () => {
    if (status !== 'ready' || page.nextOffset === null || loadingMore || !baseline.current || baseline.current.aborted
      || request.current && !request.current.signal.aborted) return
    const current = new AbortController(); request.current = current
    const signal = AbortSignal.any([current.signal, baseline.current])
    setLoadingMore(true); setMoreError(false)
    try {
      const next = await listMemories(projectId, search, page.nextOffset, signal)
      if (signal.aborted) return
      setPage(previous => ({ ...next, items: [...new Map([...previous.items, ...next.items].map(item => [item.path, item])).values()] }))
    } catch { if (!signal.aborted) setMoreError(true) }
    finally {
      if (request.current === current) {
        request.current = null
        if (!current.signal.aborted) setLoadingMore(false)
      }
    }
  }, [projectId, search, page.nextOffset, loadingMore, status])
  return { ...page, status, loadingMore, moreError, more, refresh: () => watcher.current?.refresh() }
}
