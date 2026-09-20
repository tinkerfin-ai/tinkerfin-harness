import { useCallback, useLayoutEffect, useRef, useState } from 'react'
import {
  COMPOSER_WIDTH_EXTRA, CONVERSATION_WIDTH_KEY, MIN_CONVERSATION_WIDTH,
  parseWidthPreference, persistWidthPreference, readWidthPreference, resolveConversationWidth,
} from './widthPreference'

/** 跟随主区尺寸限制显示宽度，窗口收窄不覆盖用户保存的选择 */
export function useConversationWidth(resizeContent: (apply: () => void) => void) {
  const rootRef = useRef<HTMLElement>(null)
  const saved = useRef(readWidthPreference())
  const preview = useRef<number | null>(null)
  const [dimensions, setDimensions] = useState({ width: 0, max: 0 })
  const resize = useRef(resizeContent)
  resize.current = resizeContent

  const publish = useCallback(() => {
    const root = rootRef.current
    if (!root) return
    const gutter = parseFloat(getComputedStyle(root).getPropertyValue('--layout-page-gutter'))
    const next = resolveConversationWidth(root.clientWidth, gutter, preview.current ?? saved.current)
    if (root.style.getPropertyValue('--layout-conversation-width') !== `${next.width}px`) {
      resize.current(() => {
        root.style.setProperty('--layout-conversation-width', `${next.width}px`)
        root.style.setProperty('--layout-composer-width', `${next.width + COMPOSER_WIDTH_EXTRA}px`)
      })
    }
    setDimensions(current => current.width === next.width && current.max === next.max ? current : next)
    return next
  }, [])

  useLayoutEffect(() => {
    const root = rootRef.current
    if (!root) return
    publish()
    const observer = new ResizeObserver(publish)
    observer.observe(root)
    // 断点切换会改变边距，即使容器尺寸恰好相同也需要重新计算
    window.addEventListener('resize', publish)
    const onStorage = (event: StorageEvent) => {
      if (event.key !== CONVERSATION_WIDTH_KEY && event.key !== null) return
      saved.current = parseWidthPreference(event.newValue)
      preview.current = null
      publish()
    }
    window.addEventListener('storage', onStorage)
    return () => {
      observer.disconnect()
      window.removeEventListener('resize', publish)
      window.removeEventListener('storage', onStorage)
    }
  }, [publish])

  const previewWidth = useCallback((value: number) => {
    preview.current = value
    publish()
  }, [publish])
  const cancel = useCallback(() => {
    preview.current = null
    publish()
  }, [publish])
  const commit = useCallback((value: number) => {
    const current = publish()
    if (!current || current.max <= MIN_CONVERSATION_WIDTH) return
    value = Math.round(Math.min(current.max, Math.max(MIN_CONVERSATION_WIDTH, value)))
    preview.current = null
    saved.current = value
    persistWidthPreference(value)
    publish()
  }, [publish])

  return { rootRef, ...dimensions, previewWidth, cancel, commit }
}

export type ConversationWidthControl = ReturnType<typeof useConversationWidth>
