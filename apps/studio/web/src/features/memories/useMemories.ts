import { useCallback, useEffect, useRef, useState } from 'react'
import { watchResource } from '../../api/shared/watchResource'
import { listMemories, type MemoryPage } from './api'

export function useMemories(projectId: string, query: string) {
  const [page, setPage] = useState<MemoryPage>({ items: [], nextOffset: null })
  const [status, setStatus] = useState<'loading' | 'ready' | 'error'>('loading')
  const [loadingMore, setLoadingMore] = useState(false)
  const [moreError, setMoreError] = useState(false)
  const watcher = useRef<ReturnType<typeof watchResource> | null>(null)
  const request = useRef<AbortController | null>(null)
  const revision = useRef(0)
  useEffect(() => {
    setStatus('loading'); setMoreError(false); setLoadingMore(false)
    watcher.current = watchResource({
      read: signal => listMemories(projectId, query, 0, signal),
      matches: change => change.topic === 'studio.memories.changed' && change.key === projectId,
      update: value => { request.current?.abort(); revision.current += 1; setLoadingMore(false); setPage(value); setStatus('ready') },
      onError: () => setStatus('error'),
    })
    return () => { revision.current += 1; watcher.current?.close(); request.current?.abort() }
  }, [projectId, query])
  const more = useCallback(async () => {
    if (page.nextOffset === null || loadingMore) return
    const current = new AbortController(); request.current = current
    const generation = revision.current
    setLoadingMore(true); setMoreError(false)
    try {
      const next = await listMemories(projectId, query, page.nextOffset, current.signal)
      if (current.signal.aborted || revision.current !== generation) return
      setPage(previous => ({ ...next, items: [...new Map([...previous.items, ...next.items].map(item => [item.path, item])).values()] }))
    } catch { if (!current.signal.aborted) setMoreError(true) }
    finally { if (!current.signal.aborted) setLoadingMore(false) }
  }, [projectId, query, page.nextOffset, loadingMore])
  return { ...page, status, loadingMore, moreError, more, refresh: () => watcher.current?.refresh() }
}
