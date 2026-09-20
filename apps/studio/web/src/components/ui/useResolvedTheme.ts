import { useSyncExternalStore } from 'react'

const readTheme = () => document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light'
const subscribe = (notify: () => void) => {
  const observer = new MutationObserver(notify)
  observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
  return () => observer.disconnect()
}

/** 读取已应用的主题，不覆盖用户设置或重复注册系统主题监听 */
export function useResolvedTheme() {
  return useSyncExternalStore(subscribe, readTheme)
}
