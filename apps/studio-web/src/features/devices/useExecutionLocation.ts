import { useCallback, useEffect, useMemo, useState } from 'react'
import { getServerAddress } from '../../api/shared/config'
import { fetchThreadLocalTarget, type LocalTarget } from './api'
import type { useDevices } from './useDevices'

interface ExecutionLocationOptions {
  projectId: string
  userId: number
  threadId: string
  pendingRunId: string | null
  draftKey: string
  devices: ReturnType<typeof useDevices>
  onPreferenceError: () => void
}

/** 管理项目内的执行位置；页面按账号与项目挂载，提交后保留每个草稿和会话的独立绑定 */
export function useExecutionLocation({
  projectId, userId, threadId, pendingRunId, draftKey, devices, onPreferenceError,
}: ExecutionLocationOptions) {
  const draftRoot = `${projectId}:`
  const preferenceKey = JSON.stringify(['tinkerfin.execution-location', getServerAddress(), userId, projectId])
  const [targets, setTargets] = useState<Record<string, LocalTarget | undefined>>(() => {
    try {
      const stored: unknown = JSON.parse(localStorage.getItem(preferenceKey) ?? 'null')
      const target = stored && typeof stored === 'object' && 'device_id' in stored && typeof stored.device_id === 'string'
        ? { device_id: stored.device_id, workspace_id: projectId } : undefined
      return { [draftRoot]: target }
    } catch { return {} }
  })
  const preferredTarget = targets[draftRoot]
  const currentDeviceId = devices.status === 'ready' ? devices.devices.find(device => !device.revoked)?.device_id : undefined
  useEffect(() => {
    if (!preferredTarget || !currentDeviceId || preferredTarget.device_id === currentDeviceId) return
    // 新草稿沿用本地偏好并使用当前设备；已开始会话的独立绑定保持不变
    const next = { device_id: currentDeviceId, workspace_id: projectId }
    setTargets(current => ({ ...current, [draftRoot]: next }))
    try { localStorage.setItem(preferenceKey, JSON.stringify(next)) } catch { /* 当前选择不依赖偏好持久化 */ }
  }, [currentDeviceId, preferredTarget, projectId, draftRoot, preferenceKey])

  const known = !threadId || Object.hasOwn(targets, draftKey)
  const [readFailed, setReadFailed] = useState(false)
  const [readAttempt, setReadAttempt] = useState(0)
  useEffect(() => {
    if (!threadId || known) return
    const controller = new AbortController()
    setReadFailed(false)
    void fetchThreadLocalTarget(threadId, controller.signal).then(target => {
      if (!controller.signal.aborted) setTargets(current => ({ ...current, [draftKey]: target ?? undefined }))
    }).catch(() => { if (!controller.signal.aborted) setReadFailed(true) })
    return () => controller.abort()
  }, [threadId, draftKey, known, readAttempt])

  const selectedTarget = targets[draftKey]
  const target = useMemo(() => selectedTarget && currentDeviceId
    ? { ...selectedTarget, device_id: currentDeviceId } : selectedTarget,
  [selectedTarget, currentDeviceId])
  const locked = Boolean(threadId || pendingRunId)
  const isAvailable = useCallback((value: LocalTarget) => devices.status === 'ready'
    && devices.devices.some(device => !device.revoked && device.device_id === value.device_id && value.workspace_id === projectId),
  [devices.devices, devices.status, projectId])
  const retry = useCallback(() => setReadAttempt(value => value + 1), [])
  const select = useCallback((value: LocalTarget | undefined): boolean => {
    if (locked) return false
    try { localStorage.setItem(preferenceKey, JSON.stringify(value ?? null)) }
    catch { onPreferenceError() }
    setTargets(current => ({ ...current, [draftRoot]: value }))
    return !known || target?.device_id !== value?.device_id || target?.workspace_id !== value?.workspace_id
  }, [locked, preferenceKey, onPreferenceError, draftRoot, known, target])

  const recordSubmission = useCallback((pendingKey: string, value: LocalTarget | undefined) => {
    setTargets(current => {
      const next = { ...current, [pendingKey]: value }
      if (draftKey !== draftRoot) delete next[draftKey]
      return next
    })
  }, [draftKey, draftRoot])
  const registerThread = useCallback((pendingKey: string, registeredThreadId: string, value: LocalTarget | undefined) => {
    setTargets(current => {
      const next = { ...current, [`thread:${registeredThreadId}`]: value }
      delete next[pendingKey]
      return next
    })
  }, [])

  return {
    target,
    state: known ? 'ready' as const : readFailed ? 'error' as const : 'loading' as const,
    locked,
    isAvailable,
    retry,
    select,
    recordSubmission,
    registerThread,
  }
}
