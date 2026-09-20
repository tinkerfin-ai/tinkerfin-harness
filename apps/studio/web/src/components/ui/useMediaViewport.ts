import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import type { PointerEvent } from 'react'

/** 图片和图表共享比例与拖动；使用真实滚动位置，放大后仍可用滚动条浏览 */
export function useMediaViewport(mediaId: string) {
  const stage = useRef<HTMLDivElement>(null)
  const [bounds, setBounds] = useState({ width: 0, height: 0 })
  const [media, setMedia] = useState({ width: 0, height: 0 })
  const [zoom, setZoom] = useState<number | 'fit'>('fit')
  const drag = useRef<{ id: number; x: number; y: number; left: number; top: number } | null>(null)
  useEffect(() => {
    setMedia({ width: 0, height: 0 })
    setZoom('fit')
    drag.current = null
  }, [mediaId])
  useLayoutEffect(() => {
    const element = stage.current
    if (!element) return
    const update = () => setBounds({ width: element.clientWidth, height: element.clientHeight })
    update()
    const observer = new ResizeObserver(update)
    observer.observe(element)
    return () => observer.disconnect()
  }, [])
  const fit = media.width && media.height && bounds.width && bounds.height
    ? Math.min(bounds.width / media.width, bounds.height / media.height) : 1
  const scale = zoom === 'fit' ? fit : zoom
  const width = media.width * scale
  const height = media.height * scale
  const canPan = width > bounds.width || height > bounds.height
  useLayoutEffect(() => {
    const element = stage.current
    if (!element) return
    element.scrollLeft = Math.max(0, (width - bounds.width) / 2)
    element.scrollTop = Math.max(0, (height - bounds.height) / 2)
  }, [width, height, bounds.width, bounds.height])
  const changeZoom = (value: number | 'fit') => {
    setZoom(value)
    drag.current = null
  }
  return {
    stage, media, setMedia, scale, zoom, changeZoom, canPan,
    zoomIn: () => changeZoom(Math.min(Math.max(4, fit), scale * 1.25)),
    zoomOut: () => changeZoom(Math.max(Math.min(fit, 0.1), scale / 1.25)),
    canZoomIn: scale < Math.max(4, fit),
    canZoomOut: scale > Math.min(fit, 0.1),
    mediaStyle: { width: width || undefined, height: height || undefined },
    frameStyle: { width: Math.max(bounds.width, width), height: Math.max(bounds.height, height) },
    pan: (x: number, y: number) => {
      const element = stage.current
      if (element) { element.scrollLeft += x; element.scrollTop += y }
    },
    pointerDown: (event: PointerEvent<HTMLDivElement>) => {
      if (event.button !== 0 || !canPan) return
      event.currentTarget.setPointerCapture(event.pointerId)
      drag.current = { id: event.pointerId, x: event.clientX, y: event.clientY, left: event.currentTarget.scrollLeft, top: event.currentTarget.scrollTop }
    },
    pointerMove: (event: PointerEvent<HTMLDivElement>) => {
      const start = drag.current
      if (start?.id !== event.pointerId) return
      event.currentTarget.scrollLeft = start.left + start.x - event.clientX
      event.currentTarget.scrollTop = start.top + start.y - event.clientY
    },
    pointerEnd: () => { drag.current = null },
  }
}
