import { useEffect, useRef, useState } from 'react'
import { ApiError, requestJson } from '../../api/shared/http'
import { emptyServices, newServiceDraft, serviceDirty, serviceDraft, serviceWrite, type SavedServices, type ServiceCapability, type ServiceDraft, type ServiceSettings } from './serviceSettings'

type TestState = { outcome: 'running' | 'success' | 'failed' | 'stopped'; code: string } | null
const draftDefaults = () => ({ web_search: newServiceDraft('web_search'), image_generation: newServiceDraft('image_generation') })

/** 两种能力独立保存草稿，失效请求和停止测试不能覆盖较新的状态 */
export function useServiceSettings(active: boolean) {
  const [capability, setCapability] = useState<ServiceCapability>('web_search')
  const [saved, setSaved] = useState<SavedServices>(emptyServices)
  const [drafts, setDrafts] = useState(draftDefaults)
  const [loading, setLoading] = useState(false)
  const [loadFailed, setLoadFailed] = useState(false)
  const [reload, setReload] = useState(0)
  const [saving, setSaving] = useState<ServiceCapability | null>(null)
  const [feedback, setFeedback] = useState<Record<ServiceCapability, string>>({ web_search: '', image_generation: '' })
  const [errors, setErrors] = useState<Record<ServiceCapability, string>>({ web_search: '', image_generation: '' })
  const [tests, setTests] = useState<Record<ServiceCapability, TestState>>({ web_search: null, image_generation: null })
  const requests = useRef(new Map<string, AbortController>())
  useEffect(() => {
    if (!active) return
    const controller = new AbortController()
    const owned = requests.current
    setLoading(true); setLoadFailed(false)
    void requestJson<SavedServices>('/api/services/settings', { signal: controller.signal, suppressGlobalError: true })
      .then(value => {
        if (controller.signal.aborted) return
        setSaved(value)
        setDrafts({ web_search: serviceDraft(value.web_search, 'web_search'), image_generation: serviceDraft(value.image_generation, 'image_generation') })
        setFeedback({ web_search: '', image_generation: '' }); setErrors({ web_search: '', image_generation: '' }); setTests({ web_search: null, image_generation: null })
      })
      .catch(() => { if (!controller.signal.aborted) setLoadFailed(true) })
      .finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => { controller.abort(); for (const request of owned.values()) request.abort(); owned.clear(); setSaving(null) }
  }, [active, reload])

  const change = (draft: ServiceDraft) => {
    setDrafts(previous => ({ ...previous, [draft.capability]: draft }))
    setFeedback(previous => ({ ...previous, [draft.capability]: '' }))
    setErrors(previous => ({ ...previous, [draft.capability]: '' }))
  }
  const write = async (capability: ServiceCapability, clear = false) => {
    if (requests.current.has('save')) return false
    const controller = new AbortController()
    requests.current.set('save', controller); setSaving(capability)
    setErrors(previous => ({ ...previous, [capability]: '' }))
    try {
      const value = await requestJson<ServiceSettings | null>(`/api/services/${capability}`, { method: clear ? 'DELETE' : 'PUT', body: clear ? undefined : serviceWrite(drafts[capability]), signal: controller.signal, suppressGlobalError: true })
      if (controller.signal.aborted) return false
      const result = clear ? null : value
      setSaved(previous => ({ ...previous, [capability]: result }))
      setDrafts(previous => ({ ...previous, [capability]: serviceDraft(result, capability) }))
      setTests(previous => ({ ...previous, [capability]: null }))
      setFeedback(previous => ({ ...previous, [capability]: clear ? '已清除配置' : '已保存' }))
      return true
    } catch (error) {
      if (!controller.signal.aborted) setErrors(previous => ({ ...previous, [capability]: error instanceof ApiError ? error.message : '保存失败，请重试' }))
      return false
    } finally {
      if (requests.current.get('save') === controller) { requests.current.delete('save'); setSaving(null) }
    }
  }
  const test = async (capability: ServiceCapability) => {
    const key = `test-${capability}`
    const pending = requests.current.get(key)
    if (pending) {
      pending.abort(); requests.current.delete(key)
      setTests(previous => ({ ...previous, [capability]: { outcome: 'stopped', code: 'stopped' } }))
      return
    }
    if (!saved[capability]?.enabled || serviceDirty(drafts[capability], saved[capability])) return
    const controller = new AbortController()
    requests.current.set(key, controller)
    setTests(previous => ({ ...previous, [capability]: { outcome: 'running', code: 'running' } }))
    try {
      const result = await requestJson<{ outcome: 'success' | 'failed'; code: string }>(`/api/services/${capability}/test`, { method: 'POST', signal: controller.signal, suppressGlobalError: true })
      if (!controller.signal.aborted) setTests(previous => ({ ...previous, [capability]: result }))
    } catch {
      if (!controller.signal.aborted) setTests(previous => ({ ...previous, [capability]: { outcome: 'failed', code: 'network_error' } }))
    } finally {
      if (requests.current.get(key) === controller) requests.current.delete(key)
    }
  }
  return {
    capability, setCapability, saved, drafts, loading, loadFailed, saving, feedback, errors, tests,
    dirty: serviceDirty(drafts.web_search, saved.web_search) || serviceDirty(drafts.image_generation, saved.image_generation),
    change, save: (capability: ServiceCapability) => write(capability), clear: (capability: ServiceCapability) => write(capability, true), test,
    reset: (capability: ServiceCapability) => change(serviceDraft(saved[capability], capability)),
    reload: () => setReload(value => value + 1),
  }
}

export type ServiceSettingsState = ReturnType<typeof useServiceSettings>
