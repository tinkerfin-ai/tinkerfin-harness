import { useEffect, useRef, useState, type PointerEvent } from 'react'

/** 指针捕获期间合并调宽预览；取消手势或卸载时交还原有宽度 */
export function useHorizontalResize({ width, multiplier, previewWidth, commit, cancel }: {
  width: number
  multiplier: number
  previewWidth: (width: number) => void
  commit: (width: number) => void
  cancel: () => void
}) {
  const current = useRef({ width, multiplier, previewWidth, commit, cancel })
  current.current = { width, multiplier, previewWidth, commit, cancel }
  const drag = useRef<{ id: number; origin: number; base: number; latest: number } | null>(null)
  const frame = useRef<number | null>(null)
  const [dragging, setDragging] = useState(false)
  const stopFrame = () => {
    if (frame.current !== null) cancelAnimationFrame(frame.current)
    frame.current = null
  }
  const proposed = (x: number) => {
    const gesture = drag.current
    return gesture ? gesture.base + (x - gesture.origin) * current.current.multiplier : current.current.width
  }
  const cancelDrag = () => {
    if (!drag.current) return
    stopFrame()
    drag.current = null
    setDragging(false)
    current.current.cancel()
  }
  useEffect(() => () => {
    if (frame.current !== null) cancelAnimationFrame(frame.current)
    if (drag.current) current.current.cancel()
  }, [])

  return {
    dragging,
    onPointerDown: (event: PointerEvent<HTMLElement>) => {
      if (event.button !== 0 || drag.current) return
      event.preventDefault()
      drag.current = { id: event.pointerId, origin: event.clientX, latest: event.clientX, base: current.current.width }
      event.currentTarget.setPointerCapture(event.pointerId)
      setDragging(true)
    },
    onPointerMove: (event: PointerEvent<HTMLElement>) => {
      if (drag.current?.id !== event.pointerId) return
      drag.current.latest = event.clientX
      if (frame.current === null) frame.current = requestAnimationFrame(() => {
        frame.current = null
        if (drag.current) current.current.previewWidth(proposed(drag.current.latest))
      })
    },
    onPointerUp: (event: PointerEvent<HTMLElement>) => {
      if (drag.current?.id !== event.pointerId) return
      const value = proposed(event.clientX)
      const moved = event.clientX !== drag.current.origin
      stopFrame()
      drag.current = null
      setDragging(false)
      if (moved) current.current.commit(value)
      else current.current.cancel()
      event.currentTarget.releasePointerCapture(event.pointerId)
    },
    onPointerCancel: cancelDrag,
    onLostPointerCapture: cancelDrag,
  }
}
