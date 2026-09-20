import { useCallback, useLayoutEffect, useState } from 'react'

export const MIN_DRAWER_WIDTH = 300
export const MAX_DRAWER_WIDTH = 520
const DEFAULT_DRAWER_WIDTH = 400

/** 保留主内容所需空间；临时收窄和隐藏不覆盖页面内的用户宽度 */
export function useDrawerLayout(contentMinWidth: number, resizeContent?: (apply: () => void) => void) {
  const [host, hostRef] = useState<HTMLDivElement | null>(null)
  const [hostWidth, setHostWidth] = useState(0)
  const [preferredWidth, setPreferredWidth] = useState(DEFAULT_DRAWER_WIDTH)
  const [preview, setPreview] = useState<number | null>(null)

  useLayoutEffect(() => {
    if (!host) return
    const measure = () => setHostWidth(host.clientWidth)
    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(host)
    return () => observer.disconnect()
  }, [host])

  const max = Math.min(MAX_DRAWER_WIDTH, Math.max(0, hostWidth - contentMinWidth))
  const available = max >= MIN_DRAWER_WIDTH
  const clamp = useCallback((width: number) => (
    Math.round(Math.max(MIN_DRAWER_WIDTH, Math.min(max, width)))
  ), [max])
  const changeWidth = useCallback((width: number, update: () => void) => {
    const apply = () => {
      // 宿主先保护阅读位置，再同步改变宽度，让容器查询也处于同一次调宽中
      host?.style.setProperty('--layout-drawer-width', `${width}px`)
      update()
    }
    if (resizeContent) resizeContent(apply)
    else apply()
  }, [host, resizeContent])
  const previewWidth = useCallback((width: number) => {
    const next = clamp(width)
    changeWidth(next, () => setPreview(next))
  }, [changeWidth, clamp])
  const cancel = useCallback(() => {
    changeWidth(available ? Math.min(max, preferredWidth) : 0, () => setPreview(null))
  }, [available, changeWidth, max, preferredWidth])
  const commit = useCallback((width: number) => {
    const next = clamp(width)
    changeWidth(next, () => {
      setPreferredWidth(next)
      setPreview(null)
    })
  }, [changeWidth, clamp])

  return {
    hostRef, available, max,
    width: available ? Math.min(max, preview ?? preferredWidth) : 0,
    previewWidth, cancel, commit,
  }
}

export type DrawerLayout = ReturnType<typeof useDrawerLayout>
