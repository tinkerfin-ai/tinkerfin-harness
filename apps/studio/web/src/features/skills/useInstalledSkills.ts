import { useCallback, useEffect, useState } from 'react'
import { watchResource } from '../../api/shared/watchResource'
import { listInstalledSkills } from './api'
import type { InstalledSkill } from './model'

/** 页面和输入框共用安装列表订阅；主动刷新取消旧读取，通知仅触发权威查询 */
export function useInstalledSkills(active = true) {
  const [items, setItems] = useState<InstalledSkill[]>([])
  const [status, setStatus] = useState<'loading' | 'ready' | 'error'>('loading')
  const [error, setError] = useState<unknown>(null)
  const [generation, setGeneration] = useState(0)
  useEffect(() => {
    if (!active) { setStatus('loading'); return }
    const watcher = watchResource({
      read: listInstalledSkills,
      matches: change => change.topic === 'studio.skills.changed',
      update: value => { setItems(value); setStatus('ready'); setError(null) },
      onError: failure => { setError(failure); setStatus('error') },
    })
    return watcher.close
  }, [active, generation])
  return { items, status, error, refresh: useCallback(() => setGeneration(value => value + 1), []) }
}
