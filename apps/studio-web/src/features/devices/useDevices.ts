import { useEffect, useRef, useState } from 'react'
import { watchResource } from '../../api/shared/watchResource'
import { fetchDevices, type LocalDevice } from './api'

export function useDevices() {
  const [value, setValue] = useState<{
    devices: LocalDevice[]
    status: 'loading' | 'ready' | 'error'
    refreshing: boolean
  }>({ devices: [], status: 'loading', refreshing: false })
  const refresh = useRef<(() => void) | null>(null)
  useEffect(() => {
    let active = true
    let refreshRead = false
    let requested = false
    const subscription = watchResource({
      read: async signal => {
        // 基线在途时，主动刷新等待后续新读取，不能把基线结果当作刷新完成
        refreshRead = requested
        try { return await fetchDevices(signal) }
        finally {
          if (active && signal.aborted) {
            requested = false
            setValue(current => ({ ...current, refreshing: false }))
          }
        }
      },
      update: devices => {
        if (refreshRead) requested = false
        setValue({ devices, status: 'ready', refreshing: requested })
      },
      matches: change => change.topic === 'studio.devices.changed',
      // 在线状态有服务端有效期；可见页面定期核对，断线不推断任务已停止
      refreshWhile: devices => devices.some(device => !device.revoked),
      onError: () => {
        if (refreshRead) requested = false
        setValue(current => ({ ...current, status: 'error', refreshing: requested }))
      },
    })
    refresh.current = () => {
      if (requested) return
      requested = true
      setValue(current => ({ ...current, refreshing: true }))
      subscription.refresh()
    }
    return () => { active = false; subscription.close(); refresh.current = null }
  }, [])
  return { ...value, refresh: () => refresh.current?.() }
}
