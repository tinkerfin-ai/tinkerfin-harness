const completed = 'tinkerfin:execution-location-removed'
const activeRunKey = 'tinkerfin:active-conversation-run'
const record = (value: unknown): value is Record<string, unknown> => Boolean(value) && typeof value === 'object' && !Array.isArray(value)

/** 一次性清理执行位置偏好和保存请求，保留草稿、确认选择及原运行身份 */
export function convertComputerResources(): void {
  try {
    if (localStorage.getItem(completed) !== 'true') {
      const keys = Array.from({ length: localStorage.length }, (_, index) => localStorage.key(index)).filter((key): key is string => key !== null)
      for (const key of keys) {
        let parsed: unknown
        try { parsed = JSON.parse(key) } catch { continue }
        if (Array.isArray(parsed) && parsed.length === 4 && parsed[0] === 'tinkerfin.execution-location') localStorage.removeItem(key)
      }
      localStorage.setItem(completed, 'true')
    }
    // 每个标签页有独立恢复缓存，不能用账号共享的偏好标记跳过它
    if (sessionStorage.getItem(completed) === 'true') return
    const saved = sessionStorage.getItem(activeRunKey)
    if (saved) {
      const value: unknown = JSON.parse(saved)
      if (record(value) && Array.isArray(value.runs)) {
        let changed = false
        for (const run of value.runs) {
          if (!record(run) || !record(run.payload) || !record(run.payload.forwardedProps)) continue
          const props = run.payload.forwardedProps
          for (const field of ['localTarget', 'local_target']) {
            if (!Object.hasOwn(props, field)) continue
            delete props[field]
            changed = true
          }
        }
        if (changed) sessionStorage.setItem(activeRunKey, JSON.stringify(value))
      }
    }
    sessionStorage.setItem(completed, 'true')
  } catch {
    // 浏览器禁止存储时仍可启动；无法读取的恢复缓存由原读取边界处理
  }
}
