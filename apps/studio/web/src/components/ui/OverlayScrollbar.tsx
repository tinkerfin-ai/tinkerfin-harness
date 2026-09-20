import { useCallback, useLayoutEffect, useRef, useState } from 'react'
import type { PointerEvent as ReactPointerEvent, RefObject } from 'react'

const HIDE_DELAY_MS = 1_000
const MIN_THUMB_SIZE_PX = 24

export type OverlayScrollbarAxis = 'vertical' | 'horizontal'
export type OverlayScrollbarSize = 'regular' | 'compact'
export type OverlayScrollbarVisibility = 'persistent' | 'transient'

export interface OverlayScrollbarProps {
  viewportRef: RefObject<HTMLElement | null>
  axis?: OverlayScrollbarAxis
  size?: OverlayScrollbarSize
  visibility?: OverlayScrollbarVisibility
  onUserScrollIntent?: () => void
}

interface ScrollbarGeometry {
  contentLength: number
  maxScroll: number
  travel: number
}

const geometryForSize = (size: OverlayScrollbarSize) => (
  size === 'compact'
    ? { crossSize: 6, inset: 2 }
    : { crossSize: 8, inset: 3 }
)

/** 镜像内容区滚动位置；自动模式在悬停、拖拽或键盘操作时保持可见 */
export function OverlayScrollbar({
  viewportRef,
  axis = 'vertical',
  size = 'regular',
  visibility = 'transient',
  onUserScrollIntent,
}: OverlayScrollbarProps) {
  const overlayRef = useRef<HTMLDivElement>(null)
  const thumbRef = useRef<HTMLSpanElement>(null)
  const hideTimerRef = useRef<number | null>(null)
  const measureFrameRef = useRef<number | null>(null)
  const scrollableRef = useRef(false)
  const hoveredRef = useRef(false)
  const keyboardInputRef = useRef(true)
  const keyboardFocusRef = useRef(false)
  const revealAfterMeasureRef = useRef(false)
  const geometryRef = useRef<ScrollbarGeometry>({ contentLength: 0, maxScroll: 0, travel: 0 })
  const dragRef = useRef<{
    pointerId: number
    startPointer: number
    startScroll: number
    maxScroll: number
    travel: number
  } | null>(null)
  const [visible, setVisible] = useState(false)

  const clearHideTimer = useCallback(() => {
    if (hideTimerRef.current == null) return
    window.clearTimeout(hideTimerRef.current)
    hideTimerRef.current = null
  }, [])

  const measure = useCallback(() => {
    const viewport = viewportRef.current
    const overlay = overlayRef.current
    const thumb = thumbRef.current
    const host = overlay?.parentElement
    if (!viewport || !overlay || !thumb || !host) return false

    // overlay 只镜像原滚动容器的几何，不接管内容布局和业务滚动事件
    const viewportRect = viewport.getBoundingClientRect()
    const hostRect = host.getBoundingClientRect()
    const { crossSize, inset } = geometryForSize(size)
    const viewportLength = axis === 'vertical' ? viewport.clientHeight : viewport.clientWidth
    const contentLength = axis === 'vertical' ? viewport.scrollHeight : viewport.scrollWidth
    const scrollOffset = axis === 'vertical' ? viewport.scrollTop : viewport.scrollLeft
    const trackLength = Math.max(0, viewportLength - (inset * 2))
    const maxScroll = Math.max(0, contentLength - viewportLength)
    const scrollable = trackLength > 0 && maxScroll > 1
    scrollableRef.current = scrollable
    overlay.dataset.scrollable = String(scrollable)

    if (axis === 'vertical') {
      overlay.style.top = `${viewportRect.top - hostRect.top + inset}px`
      overlay.style.left = `${viewportRect.right - hostRect.left - inset - crossSize}px`
      overlay.style.width = `${crossSize}px`
      overlay.style.height = `${trackLength}px`
    } else {
      overlay.style.top = `${viewportRect.bottom - hostRect.top - inset - crossSize}px`
      overlay.style.left = `${viewportRect.left - hostRect.left + inset}px`
      overlay.style.width = `${trackLength}px`
      overlay.style.height = `${crossSize}px`
    }

    if (!scrollable) {
      geometryRef.current = { contentLength, maxScroll: 0, travel: 0 }
      setVisible(false)
      return false
    }

    const thumbLength = Math.max(
      MIN_THUMB_SIZE_PX,
      Math.min(trackLength, trackLength * viewportLength / contentLength),
    )
    const travel = Math.max(0, trackLength - thumbLength)
    const thumbOffset = maxScroll > 0 ? travel * scrollOffset / maxScroll : 0
    geometryRef.current = { contentLength, maxScroll, travel }
    if (axis === 'vertical') {
      thumb.style.width = '100%'
      thumb.style.height = `${thumbLength}px`
      thumb.style.transform = `translate3d(0, ${thumbOffset}px, 0)`
    } else {
      thumb.style.width = `${thumbLength}px`
      thumb.style.height = '100%'
      thumb.style.transform = `translate3d(${thumbOffset}px, 0, 0)`
    }
    if (visibility === 'persistent') setVisible(true)
    return true
  }, [axis, size, viewportRef, visibility])

  const scheduleMeasure = useCallback(() => {
    // 内容变化和滚动事件在同一帧只提交一次最终几何，避免分页期间出现中间滑块位置
    if (measureFrameRef.current != null) return
    measureFrameRef.current = window.requestAnimationFrame(() => {
      measureFrameRef.current = null
      const scrollable = measure()
      if (revealAfterMeasureRef.current) {
        revealAfterMeasureRef.current = false
        if (scrollable) setVisible(true)
      }
    })
  }, [measure])

  const reveal = useCallback(() => {
    clearHideTimer()
    if (measure()) setVisible(true)
  }, [clearHideTimer, measure])

  const scheduleHide = useCallback(() => {
    if (visibility === 'persistent') return
    clearHideTimer()
    hideTimerRef.current = window.setTimeout(() => {
      if (!hoveredRef.current && !keyboardFocusRef.current && !dragRef.current) setVisible(false)
      hideTimerRef.current = null
    }, HIDE_DELAY_MS)
  }, [clearHideTimer, visibility])

  const handleThumbPointerDown = (event: ReactPointerEvent<HTMLSpanElement>) => {
    if (event.button !== 0 || event.pointerType === 'touch') return
    const viewport = viewportRef.current
    const geometry = geometryRef.current
    if (!viewport || geometry.maxScroll <= 0 || geometry.travel <= 0) return

    onUserScrollIntent?.()
    event.preventDefault()
    event.currentTarget.setPointerCapture?.(event.pointerId)
    clearHideTimer()
    hoveredRef.current = true
    dragRef.current = {
      pointerId: event.pointerId,
      startPointer: axis === 'vertical' ? event.clientY : event.clientX,
      startScroll: axis === 'vertical' ? viewport.scrollTop : viewport.scrollLeft,
      maxScroll: geometry.maxScroll,
      travel: geometry.travel,
    }
    setVisible(true)
  }

  const handleThumbPointerMove = (event: ReactPointerEvent<HTMLSpanElement>) => {
    const drag = dragRef.current
    const viewport = viewportRef.current
    if (!drag || !viewport || drag.pointerId !== event.pointerId) return
    const pointer = axis === 'vertical' ? event.clientY : event.clientX
    const nextScroll = drag.startScroll
      + (pointer - drag.startPointer) * drag.maxScroll / drag.travel
    if (axis === 'vertical') viewport.scrollTop = Math.max(0, Math.min(drag.maxScroll, nextScroll))
    else viewport.scrollLeft = Math.max(0, Math.min(drag.maxScroll, nextScroll))
    scheduleMeasure()
  }

  const finishThumbDrag = (event: ReactPointerEvent<HTMLSpanElement>) => {
    if (dragRef.current?.pointerId !== event.pointerId) return
    dragRef.current = null
    event.currentTarget.releasePointerCapture?.(event.pointerId)
    hoveredRef.current = Boolean(viewportRef.current?.matches(':hover') || thumbRef.current?.matches(':hover'))
    if (!hoveredRef.current && !keyboardFocusRef.current) scheduleHide()
  }

  useLayoutEffect(() => {
    const viewport = viewportRef.current
    if (!viewport) return undefined

    const handlePointerEnter = (event: PointerEvent) => {
      if (event.pointerType === 'touch') return
      // 鼠标仍停留在区域内时保持可见，只在真正离开后启动隐藏计时
      hoveredRef.current = true
      reveal()
    }
    const handlePointerLeave = (event: PointerEvent) => {
      if (event.pointerType === 'touch') return
      hoveredRef.current = false
      if (!dragRef.current && !keyboardFocusRef.current) scheduleHide()
    }
    const handleFocusIn = () => {
      keyboardFocusRef.current = keyboardInputRef.current
      reveal()
      if (!keyboardFocusRef.current && !hoveredRef.current && !dragRef.current) scheduleHide()
    }
    const handleFocusOut = (event: FocusEvent) => {
      if (event.relatedTarget instanceof Node && viewport.contains(event.relatedTarget)) return
      keyboardFocusRef.current = false
      if (!dragRef.current && !hoveredRef.current) scheduleHide()
    }
    // 鼠标点击保留原控件焦点，但不据此常显；再次使用键盘时恢复可见反馈
    const handlePointerDown = () => {
      keyboardInputRef.current = false
      if (!keyboardFocusRef.current) return
      keyboardFocusRef.current = false
      if (!dragRef.current && !hoveredRef.current) scheduleHide()
    }
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.altKey || event.ctrlKey || event.metaKey
        || ['Alt', 'Control', 'Meta', 'Shift'].includes(event.key)) return
      keyboardInputRef.current = true
      if (!viewport.contains(document.activeElement)) return
      keyboardFocusRef.current = true
      reveal()
    }
    const handleScroll = () => {
      clearHideTimer()
      revealAfterMeasureRef.current = true
      scheduleMeasure()
      if (!hoveredRef.current && !keyboardFocusRef.current) scheduleHide()
    }

    document.addEventListener('pointerdown', handlePointerDown, true)
    document.addEventListener('keydown', handleKeyDown, true)
    viewport.addEventListener('pointerenter', handlePointerEnter)
    viewport.addEventListener('pointerleave', handlePointerLeave)
    viewport.addEventListener('focusin', handleFocusIn)
    viewport.addEventListener('focusout', handleFocusOut)
    viewport.addEventListener('scroll', handleScroll, { passive: true })

    const resizeObserver = typeof ResizeObserver === 'undefined'
      ? undefined
      : new ResizeObserver(scheduleMeasure)
    resizeObserver?.observe(viewport)
    for (const child of viewport.children) resizeObserver?.observe(child)

    const mutationObserver = typeof MutationObserver === 'undefined'
      ? undefined
      : new MutationObserver(() => {
        for (const child of viewport.children) resizeObserver?.observe(child)
        scheduleMeasure()
      })
    mutationObserver?.observe(viewport, { childList: true, subtree: true, characterData: true })
    measure()

    return () => {
      clearHideTimer()
      if (measureFrameRef.current != null) window.cancelAnimationFrame(measureFrameRef.current)
      measureFrameRef.current = null
      resizeObserver?.disconnect()
      mutationObserver?.disconnect()
      document.removeEventListener('pointerdown', handlePointerDown, true)
      document.removeEventListener('keydown', handleKeyDown, true)
      viewport.removeEventListener('pointerenter', handlePointerEnter)
      viewport.removeEventListener('pointerleave', handlePointerLeave)
      viewport.removeEventListener('focusin', handleFocusIn)
      viewport.removeEventListener('focusout', handleFocusOut)
      viewport.removeEventListener('scroll', handleScroll)
    }
  }, [
    clearHideTimer,
    measure,
    reveal,
    scheduleHide,
    scheduleMeasure,
    viewportRef,
  ])

  return (
    <div
      ref={overlayRef}
      className={`ui-overlay-scrollbar ui-overlay-scrollbar--${axis} ui-overlay-scrollbar--${size}${visible ? ' is-visible' : ''}`}
      data-axis={axis}
      data-scrollable="false"
      data-visibility={visibility}
      aria-hidden="true"
    >
      <span
        ref={thumbRef}
        className="ui-overlay-scrollbar__thumb"
        onPointerEnter={() => {
          hoveredRef.current = true
          reveal()
        }}
        onPointerLeave={() => {
          hoveredRef.current = false
          if (!dragRef.current && !keyboardFocusRef.current) scheduleHide()
        }}
        onPointerDown={handleThumbPointerDown}
        onPointerMove={handleThumbPointerMove}
        onPointerUp={finishThumbDrag}
        onPointerCancel={finishThumbDrag}
        onLostPointerCapture={finishThumbDrag}
      />
    </div>
  )
}
