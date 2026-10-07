import { useCallback, useEffect, useRef, useState } from 'react'
import type { AuthUser } from '../../api/auth/types'
import { getServerAddress } from '../../api/shared/config'
import { watchResource } from '../../api/shared/watchResource'
import { isTranslationKey, useI18n } from '../../i18n'
import { readProjectFromLocation, writeProjectToLocation, writeWorkspaceToLocation } from '../../lib/threadRoute'
import { listProjects, saveProject, type Project } from './api'

/** 管理项目目录、当前选择和名称表单；退出工作区后释放读取与保存请求 */
export function useProjectManagement(user: AuthUser) {
  const { t } = useI18n()
  const storageKey = `tinkerfin:project:${getServerAddress()}:${user.user_id}`
  const [projects, setProjects] = useState<Project[]>([])
  const [status, setStatus] = useState<'loading' | 'ready' | 'error'>('loading')
  const [activeId, setActiveId] = useState(() => {
    const route = readProjectFromLocation()
    if (route) return route
    try { return window.localStorage.getItem(storageKey) ?? '' } catch { return '' }
  })
  const [editor, setEditor] = useState<{ project: Project | null; trigger: HTMLElement | null } | null>(null)
  const [name, setName] = useState('')
  const [error, setError] = useState('')
  const [validationAttempt, setValidationAttempt] = useState(0)
  const [saving, setSaving] = useState(false)
  const mutation = useRef<AbortController | null>(null)
  const refresh = useRef<() => void>(() => {})
  const mutationGeneration = useRef(0)
  const previousActiveId = useRef(activeId)
  useEffect(() => {
    if (previousActiveId.current === activeId) return
    previousActiveId.current = activeId
    // 项目范围变化后撤销原项目的编辑与请求，结果不得写入另一个项目的表单
    mutation.current?.abort()
    mutation.current = null
    setEditor(null); setName(''); setError(''); setValidationAttempt(0); setSaving(false)
  }, [activeId])
  const select = useCallback((id: string, threadId = '') => {
    writeProjectToLocation(id, threadId)
    setActiveId(id)
    try { window.localStorage.setItem(storageKey, id) } catch { /* 存储受限时仍可切换 */ }
  }, [storageKey])
  useEffect(() => {
    const watcher = watchResource({
      read: async signal => { const generation = mutationGeneration.current; return { items: await listProjects(signal), generation } }, matches: change => change.topic === 'studio.projects.changed',
      update: ({ items, generation }) => {
        if (generation !== mutationGeneration.current) return
        setProjects(items); setStatus('ready')
        setActiveId(current => {
          if (items.some(item => item.id === current)) return current
          const next = items[0]?.id ?? ''
          if (next) writeProjectToLocation(next, '', { history: 'replace' })
          return next
        })
      },
      onError: () => setStatus('error'),
    })
    refresh.current = watcher.refresh
    const locationChanged = () => setActiveId(readProjectFromLocation())
    window.addEventListener('popstate', locationChanged)
    return () => { watcher.close(); mutation.current?.abort(); window.removeEventListener('popstate', locationChanged) }
  }, [])
  const project = projects.find(item => item.id === activeId)
  useEffect(() => {
    if (project && readProjectFromLocation() !== project.id) writeProjectToLocation(project.id, '', { history: 'replace' })
  }, [project])
  const openEditor = (item: Project | null) => {
    if (mutation.current) return
    setName(item?.name ?? ''); setError(''); setValidationAttempt(0)
    setEditor({ project: item, trigger: document.activeElement instanceof HTMLElement ? document.activeElement : null })
  }
  const submit = async () => {
    if (!editor || mutation.current) return
    const value = name.trim()
    if (!value) { setError(t('请输入项目名称')); setValidationAttempt(current => current + 1); return }
    const request = new AbortController()
    mutation.current = request; setSaving(true); setError('')
    try {
      const item = await saveProject(value, editor.project?.id ?? null, request.signal)
      if (request.signal.aborted) return
      mutationGeneration.current += 1
      setProjects(current => editor.project ? current.map(existing => existing.id === item.id ? item : existing) : [...current, item])
      setStatus('ready'); setEditor(null)
      if (!editor.project) { writeWorkspaceToLocation('conversation', ''); select(item.id) }
      refresh.current()
    } catch (reason) {
      if (!request.signal.aborted) {
        setError(reason instanceof Error ? isTranslationKey(reason.message) ? t(reason.message) : reason.message : t('项目未能保存，请重试'))
        setValidationAttempt(current => current + 1)
      }
    } finally {
      if (mutation.current === request) mutation.current = null
      if (!request.signal.aborted) setSaving(false)
    }
  }
  return {
    project, projects, status, editor, name, error, validationAttempt, saving, select, openEditor, submit,
    setName: (value: string) => { setName(value); setError('') },
    closeEditor: () => { if (!mutation.current) setEditor(null) },
    retry: () => { setStatus('loading'); refresh.current() },
  }
}
