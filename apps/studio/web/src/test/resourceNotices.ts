import { vi } from 'vitest'
import * as notifications from '../api/notifications'
import type { ResourceChange, ResourceNotice } from '../api/notifications'

/** 通过公开订阅边界向业务读取者提供确定性的变化信号 */
export function mockResourceNotices() {
  const listeners = new Set<(notice: ResourceNotice) => void>()
  vi.spyOn(notifications, 'subscribeResourceChanges').mockImplementation(listener => {
    listeners.add(listener)
    return () => { listeners.delete(listener) }
  })
  return {
    changed: (topic: string, key: string, details: ResourceChange['details'] = {}) => {
      for (const listener of listeners) listener({ kind: 'change', change: {
        scope: { namespace: 'ns_1', owner_id: null }, topic, key, details,
      } })
    },
    resync: () => { for (const listener of listeners) listener({ kind: 'resync' }) },
  }
}
