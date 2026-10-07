import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError } from '../../api/shared/http'
import { translateCurrent } from '../../i18n'
import { isTranslationKey } from '../../i18n/messages'
import { browseSkills, installSkill, listSkillSources, readRemoteSkill, setSkillEnabled, uninstallSkill, updateSkill } from './api'
import { useInstalledSkills } from './useInstalledSkills'
import type { InstalledSkill, RemoteSkill, RemoteSkillPage, SkillSource } from './model'

type Status = 'loading' | 'ready' | 'error'
export const skillError = (error: unknown, fallback: string) => error instanceof ApiError
  ? isTranslationKey(error.message) ? translateCurrent(error.message) : error.message
  : fallback

/** 安装与来源独立加载，命令只更新服务端确认后的状态 */
export function useSkillLibrary(projectId: string | null) {
  const [sources, setSources] = useState<SkillSource[]>([])
  const installedList = useInstalledSkills(projectId)
  const installed = installedList.items
  const [sourceStatus, setSourceStatus] = useState<Status>('loading')
  const [sourceError, setSourceError] = useState<unknown>(null)
  const [generation, setGeneration] = useState(0)
  const [busy, setBusy] = useState(new Set<string>())
  const [errors, setErrors] = useState<Record<string, unknown>>({})
  const requests = useRef(new Set<AbortController>())
  const pending = useRef(new Set<string>())
  const attempts = useRef(new Map<string, { signature: string; requestId: string }>())
  const installTargets = useRef(new Map<string, { signature: string; skill: RemoteSkill }>())
  useEffect(() => {
    const owned = requests.current
    return () => { for (const controller of owned) controller.abort() }
  }, [])
  useEffect(() => {
    const controller = new AbortController()
    setSourceStatus('loading')
    void listSkillSources(controller.signal).then(value => {
      if (!controller.signal.aborted) { setSources(value); setSourceStatus('ready') }
    }).catch(error => { if (!controller.signal.aborted) { setSourceError(error); setSourceStatus('error') } })
    return () => controller.abort()
  }, [generation])
  const reloadInstalled = installedList.refresh
  const refresh = useCallback(() => {
    reloadInstalled()
    setGeneration(value => value + 1)
  }, [reloadInstalled])
  const execute = async <T,>(key: string, signature: string, action: (requestId: string, signal: AbortSignal) => Promise<T>): Promise<{ value: T } | null> => {
    if (pending.current.has(key)) return null
    pending.current.add(key); setBusy(new Set(pending.current))
    setErrors(previous => { const next = { ...previous }; delete next[key]; return next })
    let attempt = attempts.current.get(key)
    if (!attempt || attempt.signature !== signature) {
      attempt = { signature, requestId: crypto.randomUUID() }
      attempts.current.set(key, attempt)
    }
    const controller = new AbortController(); requests.current.add(controller)
    try {
      const value = await action(attempt.requestId, controller.signal)
      if (controller.signal.aborted) return null
      attempts.current.delete(key)
      reloadInstalled()
      return { value }
    } catch (error) {
      if (!controller.signal.aborted) setErrors(previous => ({ ...previous, [key]: error }))
      return null
    } finally {
      requests.current.delete(controller); pending.current.delete(key)
      if (!controller.signal.aborted) setBusy(new Set(pending.current))
    }
  }
  const install = async (skill: RemoteSkill) => {
    const key = `${skill.source_id}/${skill.id}`
    const signature = JSON.stringify(['install', skill.source_id, skill.id, skill.revision])
    const result = await execute(key, signature, async (requestId, signal) => {
      const previous = installTargets.current.get(key)
      const fixed = previous?.signature === signature ? previous.skill : skill.revision ? skill : (await readRemoteSkill(skill.source_id, skill.id, null, signal)).skill
      if (!fixed.revision) throw new Error('Missing release')
      installTargets.current.set(key, { signature, skill: fixed })
      return installSkill(projectId, fixed.source_id, fixed.id, fixed.revision, requestId, signal)
    })
    if (result) installTargets.current.delete(key)
    return Boolean(result)
  }
  const toggle = async (skill: InstalledSkill) => Boolean(await execute(skill.id, `enabled:${!skill.enabled}`, (requestId, signal) => setSkillEnabled(projectId, skill.id, !skill.enabled, requestId, signal)))
  const uninstall = async (skill: InstalledSkill) => Boolean(await execute(skill.id, 'uninstall', (requestId, signal) => uninstallSkill(projectId,  skill.id, requestId, signal)))
  const update = async (skill: InstalledSkill) => (await execute(skill.id, 'update', (requestId, signal) => updateSkill(projectId, skill.id, requestId, undefined, signal)))?.value
  return { sources, installed, sourceStatus, installedStatus: installedList.status, sourceError, installedError: installedList.error, refresh, busy, errors, install, toggle, uninstall, update }

}

interface CatalogState extends RemoteSkillPage { key: string; status: Status; loadingMore: boolean; error: unknown }
const emptyCatalog: CatalogState = { key: '', items: [], cursor: null, status: 'loading', loadingMore: false, error: null }

/** 每次来源或搜索改变都取消旧请求，游标始终属于产生它的来源和查询 */
export function useRemoteSkills(source: string, query: string, enabled: boolean) {
  const key = `${source}:${query}`
  const cache = useRef(new Map<string, CatalogState>())
  const [state, setState] = useState<CatalogState>(emptyCatalog)
  const [revision, setRevision] = useState(0)
  const controller = useRef<AbortController | null>(null)
  const paging = useRef<AbortController | null>(null)
  const currentKey = useRef(key); currentKey.current = key
  useEffect(() => {
    if (!enabled || !source) return
    const active = new AbortController(); controller.current = active
    const previous = cache.current.get(key)
    if (previous && revision === 0) { setState(previous); return () => active.abort() }
    setState({ ...emptyCatalog, key })
    const timer = window.setTimeout(() => {
      void browseSkills(source, query, null, active.signal).then(page => {
        if (active.signal.aborted) return
        const next: CatalogState = { ...page, key, status: 'ready', loadingMore: false, error: null }
        cache.current.set(key, next); setState(next)
      }).catch(error => { if (!active.signal.aborted) setState({ ...emptyCatalog, key, status: 'error', error }) })
    }, query ? 250 : 0)
    return () => { window.clearTimeout(timer); active.abort() }
  }, [source, query, key, enabled, revision])
  const more = useCallback(async () => {
    if (!state.cursor || state.loadingMore || !controller.current || state.key !== key) return
    const active = controller.current
    if (paging.current === active || active.signal.aborted) return
    paging.current = active
    setState(previous => ({ ...previous, loadingMore: true, error: null }))
    try {
      const page = await browseSkills(source, query, state.cursor, active.signal)
      if (active.signal.aborted || currentKey.current !== key) return
      if (page.cursor === state.cursor) throw new Error('Skill catalog cursor did not advance')
      const items = Array.from(new Map([...state.items, ...page.items].map(item => [item.id, item])).values())
      const next: CatalogState = { ...page, items, key, status: 'ready', loadingMore: false, error: null }
      cache.current.set(key, next); setState(next)
    } catch (error) { if (!active.signal.aborted) setState(previous => ({ ...previous, loadingMore: false, error })) }
    finally { if (paging.current === active) paging.current = null }
  }, [state, key, source, query])
  return { ...(state.key === key ? state : { ...emptyCatalog, key }), more, retry: () => setRevision(value => value + 1) }
}
