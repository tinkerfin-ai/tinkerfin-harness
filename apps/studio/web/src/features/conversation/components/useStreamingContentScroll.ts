import { useCallback, useLayoutEffect, useRef, useState } from 'react'

const BOTTOM_TOLERANCE = 2

const initialReading = () => ({
  live: false,
  following: true,
  top: 0,
  height: 0,
  viewportHeight: 0,
  visible: false,
  viewport: null as HTMLDivElement | null,
})

const updateFollowing = (state: ReturnType<typeof initialReading>, top: number, bottom: number) => {
  if (!state.live) return
  if (top < Math.min(state.top, bottom) && bottom - top > BOTTOM_TOLERANCE) {
    state.following = false
  } else if (top > state.top) {
    const previousBottom = state.height - state.viewportHeight
    const reachedBottom = previousBottom > 0 ? Math.min(previousBottom, bottom) : bottom
    if (reachedBottom - top <= BOTTOM_TOLERANCE) state.following = true
  }
}

/** 每个流式字段独立保留阅读意图；首次打开已完成内容时从顶部阅读 */
export function useStreamingContentScroll({
  identity,
  value,
  running,
  visible,
}: {
  identity: string
  value: string | undefined
  running: boolean
  visible: boolean
}) {
  const [viewport, viewportRef] = useState<HTMLDivElement | null>(null)
  const [content, contentRef] = useState<HTMLElement | null>(null)
  const reading = useRef(initialReading())

  useLayoutEffect(() => {
    reading.current = initialReading()
  }, [identity])

  const reconcile = useCallback(() => {
    if (!visible || !viewport) return
    const state = reading.current
    const bottom = Math.max(0, viewport.scrollHeight - viewport.clientHeight)
    // 用户位移可能早于滚动事件到达；同时追加内容也不能丢失已回到底部的意图
    updateFollowing(state, viewport.scrollTop, bottom)
    if (state.live && state.following) {
      viewport.scrollTop = bottom
    }
    state.top = viewport.scrollTop
    state.height = viewport.scrollHeight
    state.viewportHeight = viewport.clientHeight
  }, [viewport, visible])

  useLayoutEffect(() => {
    const state = reading.current
    // 已参与实时阅读的字段继续接收最后一次布局，终止状态不重置用户的暂停选择
    if (visible && running) state.live = true
    if (visible && viewport && (!state.visible || state.viewport !== viewport)) {
      viewport.scrollTop = Math.min(state.top, Math.max(0, viewport.scrollHeight - viewport.clientHeight))
    }
    state.visible = visible
    state.viewport = viewport
    reconcile()
  }, [identity, value, running, visible, viewport, content, reconcile])

  useLayoutEffect(() => {
    if (!visible || !viewport) return
    let frame: number | null = null
    let touch: { x: number; y: number } | null = null

    const pause = () => {
      if (viewport.scrollTop > 0) reading.current.following = false
    }
    const onWheel = (event: WheelEvent) => {
      if (event.deltaY < 0 && Math.abs(event.deltaY) > Math.abs(event.deltaX)) pause()
    }
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.target !== viewport || event.defaultPrevented) return
      if (['ArrowUp', 'PageUp', 'Home'].includes(event.key) || (event.key === ' ' && event.shiftKey)) pause()
    }
    const onTouchStart = (event: TouchEvent) => {
      const point = event.touches[0]
      touch = point ? { x: point.clientX, y: point.clientY } : null
    }
    const onTouchMove = (event: TouchEvent) => {
      const point = event.touches[0]
      if (touch && point && point.clientY - touch.y > Math.abs(point.clientX - touch.x)) pause()
      onTouchStart(event)
    }
    const onTouchEnd = () => { touch = null }
    const onScroll = () => {
      const state = reading.current
      const top = viewport.scrollTop
      const height = viewport.scrollHeight
      const viewportHeight = viewport.clientHeight
      // 原生滚动条也通过实际位移识别；布局收缩及自身定位不改变阅读意图
      updateFollowing(state, top, Math.max(0, height - viewportHeight))
      state.top = top
      state.height = height
      state.viewportHeight = viewportHeight
    }
    const observer = new ResizeObserver(() => {
      if (frame !== null) return
      frame = window.requestAnimationFrame(() => {
        frame = null
        reconcile()
      })
    })
    observer.observe(viewport)
    if (content) observer.observe(content)
    viewport.addEventListener('scroll', onScroll)
    viewport.addEventListener('wheel', onWheel, { passive: true })
    viewport.addEventListener('keydown', onKeyDown)
    viewport.addEventListener('touchstart', onTouchStart, { passive: true })
    viewport.addEventListener('touchmove', onTouchMove, { passive: true })
    viewport.addEventListener('touchend', onTouchEnd)
    viewport.addEventListener('touchcancel', onTouchEnd)
    return () => {
      observer.disconnect()
      if (frame !== null) window.cancelAnimationFrame(frame)
      viewport.removeEventListener('scroll', onScroll)
      viewport.removeEventListener('wheel', onWheel)
      viewport.removeEventListener('keydown', onKeyDown)
      viewport.removeEventListener('touchstart', onTouchStart)
      viewport.removeEventListener('touchmove', onTouchMove)
      viewport.removeEventListener('touchend', onTouchEnd)
      viewport.removeEventListener('touchcancel', onTouchEnd)
    }
  }, [identity, visible, viewport, content, reconcile])

  return { viewportRef, contentRef }
}
