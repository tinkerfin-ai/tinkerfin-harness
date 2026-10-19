import { useEffect, useRef, useState } from 'react'
import { watchResource } from '../../api/shared/watchResource'
import { fetchDevices, type LocalDevice } from './api'

export function useDevices() {
  const [value, setValue] = useState<{ devices: LocalDevice[]; status: 'loading' | 'ready' | 'error' }>({ devices: [], status: 'loading' })
  const watch = useRef<ReturnType<typeof watchResource<LocalDevice[]>> | null>(null)
  useEffect(() => {
    const subscription = watchResource({
      read: fetchDevices,
      update: devices => setValue({ devices, status: 'ready' }),
      matches: change => change.topic === 'studio.devices.changed',
      // 在线状态有服务端有效期；可见页面定期核对，断线不推断任务已停止
      refreshWhile: devices => devices.some(device => !device.revoked),
      onError: () => setValue(current => ({ ...current, status: 'error' })),
    })
    watch.current = subscription
    return () => { subscription.close(); watch.current = null }
  }, [])
  return { ...value, refresh: () => watch.current?.refresh() }
}
