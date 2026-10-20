import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { CSSProperties } from 'react'

const MIN_SCROLL_DISTANCE_PX = 8
const TRAVEL_SPEED_PX_PER_SECOND = 32

const measureScrollDistance = (
  viewport: HTMLElement,
  content: HTMLElement,
  endRevealInset: number,
) => {
  const overflowDistance = Math.max(0, content.scrollWidth - viewport.clientWidth)
  if (overflowDistance === 0) return 0
  if (endRevealInset === 0 && overflowDistance <= MIN_SCROLL_DISTANCE_PX) return 0
  return overflowDistance + endRevealInset
}

const marqueeTimeline = (scrollDistance: number) => {
  // 固定时长会让长标题显著加速；按距离计算时长，保证模型与历史标题的像素速度一致
  const travelDurationMs = Math.round(scrollDistance * 1000 / TRAVEL_SPEED_PX_PER_SECOND)
  const cycleDurationMs = travelDurationMs * 2
  const endTransform = `translateX(-${scrollDistance}px)`

  return {
    cycleDurationMs,
    travelDurationMs,
    keyframes: [
      { transform: 'translateX(0)', offset: 0 },
      { transform: endTransform, offset: .5 },
      { transform: 'translateX(0)', offset: 1 },
    ] satisfies Keyframe[],
  }
}

export function OverflowMarquee({
  children,
  className,
  endRevealInset = 0,
}: {
  children: string
  className?: string
  endRevealInset?: number
}) {
  const viewportRef = useRef<HTMLSpanElement>(null)
  const contentRef = useRef<HTMLSpanElement>(null)
  const [scrollDistance, setScrollDistance] = useState(0)

  useLayoutEffect(() => {
    const measure = () => {
      const viewport = viewportRef.current
      const content = contentRef.current
      if (!viewport || !content) return
      const nextScrollDistance = measureScrollDistance(viewport, content, endRevealInset)
      setScrollDistance((current) => current === nextScrollDistance ? current : nextScrollDistance)
    }

    measure()
    if (typeof ResizeObserver === 'undefined') return undefined
    const observer = new ResizeObserver(measure)
    if (viewportRef.current) observer.observe(viewportRef.current)
    if (contentRef.current) observer.observe(contentRef.current)
    return () => observer.disconnect()
  }, [children, endRevealInset])

  const timeline = useMemo(() => marqueeTimeline(scrollDistance), [scrollDistance])

  useEffect(() => {
    const viewport = viewportRef.current
    const content = contentRef.current
    if (!viewport || !content) return undefined
    const trigger = viewport.closest<HTMLElement>('.overflow-marquee-trigger') ?? viewport
    let animation: Animation | null = null
    let startFrame: number | null = null

    const stop = () => {
      if (startFrame != null) {
        window.cancelAnimationFrame(startFrame)
        startFrame = null
      }
      animation?.cancel()
      animation = null
      content.style.removeProperty('transform')
    }
    const start = () => {
      stop()
      startFrame = window.requestAnimationFrame(() => {
        startFrame = null
        const nextScrollDistance = measureScrollDistance(viewport, content, endRevealInset)
        setScrollDistance((current) => current === nextScrollDistance ? current : nextScrollDistance)
        if (nextScrollDistance === 0) return

        const nextTimeline = marqueeTimeline(nextScrollDistance)
        const prefersReducedMotion = typeof window.matchMedia === 'function'
          && window.matchMedia('(prefers-reduced-motion: reduce)').matches
        if (prefersReducedMotion) {
          content.style.transform = `translateX(-${nextScrollDistance}px)`
          return
        }
        if (typeof content.animate !== 'function') return
        animation = content.animate(nextTimeline.keyframes, {
          duration: nextTimeline.cycleDurationMs,
          easing: 'linear',
          iterations: Infinity,
        })
      })
    }

    trigger.addEventListener('mouseenter', start)
    trigger.addEventListener('mouseleave', stop)
    if (trigger.matches(':hover')) start()
    return () => {
      trigger.removeEventListener('mouseenter', start)
      trigger.removeEventListener('mouseleave', stop)
      stop()
    }
  }, [endRevealInset])

  const marqueeStyle = {
    '--overflow-marquee-distance': `${scrollDistance}px`,
    '--overflow-marquee-travel-duration': `${timeline.travelDurationMs}ms`,
  } as CSSProperties

  return (
    <span
      ref={viewportRef}
      className={['overflow-marquee', scrollDistance > 0 ? 'is-overflowing' : '', className].filter(Boolean).join(' ')}
    >
      <span
        ref={contentRef}
        className={`overflow-marquee-content ${scrollDistance > 0 ? 'is-overflowing' : ''}`}
        style={marqueeStyle}
      >
        {children}
      </span>
    </span>
  )
}
