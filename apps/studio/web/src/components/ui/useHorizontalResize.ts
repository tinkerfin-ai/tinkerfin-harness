import { useEffect, useRef, useState, type KeyboardEvent, type PointerEvent } from 'react'

/** 指针捕获合并调宽预览，键盘按同一方向调整；取消手势或卸载时交还原有宽度 */
export function useHorizontalResize({ width, min, max, multiplier, previewWidth, commit, cancel }: {
  width: number
  min: number
  max: number
  multiplier: number
  previewWidth: (width: number) => void
  commit: (width: number) => void
  cancel: () => void
}) {
  const current = useRef({ width, min, max, multiplier, previewWidth, commit, cancel })
  current.current = { width, min, max, multiplier, previewWidth, commit, cancel }
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
    onKeyDown: (event: KeyboardEvent<HTMLElement>) => {
      if (drag.current) return
      const options = current.current
      const width = event.key === 'Home' ? options.min
        : event.key === 'End' ? options.max
          : event.key === 'ArrowLeft' ? options.width - 16 * options.multiplier
            : event.key === 'ArrowRight' ? options.width + 16 * options.multiplier
              : undefined
      if (width === undefined) return
      event.preventDefault()
      options.commit(Math.max(options.min, Math.min(options.max, width)))
    },
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
