import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type KeyboardEvent,
  type PointerEvent,
  type RefObject,
} from 'react'

import { useI18n } from '../../../i18n'

/* eslint-disable jsx-a11y/no-noninteractive-element-interactions, jsx-a11y/no-noninteractive-tabindex -- 可调整 separator 是 WAI-ARIA range widget，插件仍按非交互角色检查 */

const CARD_HEIGHT_PROPERTY = '--interaction-card-height'
const CARD_MIN_HEIGHT = 'var(--layout-interaction-card-min-height)'
const CARD_MAX_HEIGHT = 'var(--layout-interaction-card-max-height)'
const POINTER_DRAG_THRESHOLD_PX = 3
const KEYBOARD_STEP_PX = 16
const KEYBOARD_LARGE_STEP_PX = 48

interface ResizeMetrics {
  min: number
  max: number
  current: number
}

interface DragSession extends ResizeMetrics {
  pointerId: number
  startY: number
  startHeight: number
  started: boolean
}

const EMPTY_METRICS: ResizeMetrics = { min: 0, max: 0, current: 0 }

const clamp = (value: number, minimum: number, maximum: number) => (
  Math.min(maximum, Math.max(minimum, value))
)

const sameMetrics = (left: ResizeMetrics, right: ResizeMetrics) => (
  left.min === right.min && left.max === right.max && left.current === right.current
)

const measureCard = (card: HTMLElement): ResizeMetrics => {
  const previousHeight = card.style.getPropertyValue(CARD_HEIGHT_PROPERTY)
  const measureAt = (height: string) => {
    card.style.setProperty(CARD_HEIGHT_PROPERTY, height)
    return card.getBoundingClientRect().height
  }
  const min = measureAt(CARD_MIN_HEIGHT)
  const max = Math.max(min, measureAt(CARD_MAX_HEIGHT))
  if (previousHeight) card.style.setProperty(CARD_HEIGHT_PROPERTY, previousHeight)
  else card.style.removeProperty(CARD_HEIGHT_PROPERTY)
  const current = clamp(card.getBoundingClientRect().height, min, max)
  if (previousHeight) card.style.setProperty(CARD_HEIGHT_PROPERTY, `${current}px`)
  return { min, max, current }
}

export function InteractionCardResizeHandle({
  cardRef,
  controls,
}: {
  cardRef: RefObject<HTMLElement | null>
  controls: string
}) {
  const { t } = useI18n()
  const [metrics, setMetrics] = useState<ResizeMetrics>(EMPTY_METRICS)
  const metricsRef = useRef(metrics)
  const dragRef = useRef<DragSession | null>(null)
  const frameRef = useRef<number | null>(null)
  const pendingHeightRef = useRef<number | null>(null)

  const publishMetrics = useCallback((next: ResizeMetrics) => {
    metricsRef.current = next
    setMetrics((current) => sameMetrics(current, next) ? current : next)
  }, [])

  const applyHeight = useCallback((height: number, bounds = metricsRef.current) => {
    const card = cardRef.current
    if (!card || bounds.max <= 0) return bounds.current
    const current = clamp(height, bounds.min, bounds.max)
    card.style.setProperty(CARD_HEIGHT_PROPERTY, `${current}px`)
    publishMetrics({ min: bounds.min, max: bounds.max, current })
    return current
  }, [cardRef, publishMetrics])

  const syncBounds = useCallback(() => {
    const card = cardRef.current
    if (!card) return EMPTY_METRICS
    const next = measureCard(card)
    publishMetrics(next)
    return next
  }, [cardRef, publishMetrics])

  const clearResizeState = useCallback(() => {
    cardRef.current?.classList.remove('is-resizing')
    document.documentElement.classList.remove('is-resizing-interaction-card')
  }, [cardRef])

  const flushPendingHeight = useCallback(() => {
    if (frameRef.current != null) {
      window.cancelAnimationFrame(frameRef.current)
      frameRef.current = null
    }
    const pendingHeight = pendingHeightRef.current
    pendingHeightRef.current = null
    const drag = dragRef.current
    if (pendingHeight == null || !drag) return drag?.current ?? metricsRef.current.current
    return applyHeight(pendingHeight, drag)
  }, [applyHeight])

  const scheduleHeight = useCallback((height: number) => {
    pendingHeightRef.current = height
    if (frameRef.current != null) return
    frameRef.current = window.requestAnimationFrame(() => {
      frameRef.current = null
      const pendingHeight = pendingHeightRef.current
      pendingHeightRef.current = null
      const drag = dragRef.current
      if (pendingHeight == null || !drag) return
      applyHeight(pendingHeight, drag)
    })
  }, [applyHeight])

  const finishPointerResize = useCallback(() => {
    const drag = dragRef.current
    if (!drag) return
    flushPendingHeight()
    dragRef.current = null
    clearResizeState()
  }, [clearResizeState, flushPendingHeight])

  useEffect(() => {
    syncBounds()
  }, [syncBounds])

  useEffect(() => {
    const onResize = () => syncBounds()
    window.addEventListener('resize', onResize)
    window.visualViewport?.addEventListener('resize', onResize)
    return () => {
      window.removeEventListener('resize', onResize)
      window.visualViewport?.removeEventListener('resize', onResize)
    }
  }, [syncBounds])

  useEffect(() => () => {
    if (frameRef.current != null) window.cancelAnimationFrame(frameRef.current)
    clearResizeState()
  }, [clearResizeState])

  const onPointerDown = (event: PointerEvent<HTMLDivElement>) => {
    if (event.button !== 0 || (event.pointerType && event.pointerType !== 'mouse')) return
    const card = cardRef.current
    if (!card) return
    event.preventDefault()
    event.stopPropagation()
    const bounds = syncBounds()
    dragRef.current = {
      ...bounds,
      pointerId: event.pointerId,
      startY: event.clientY,
      startHeight: bounds.current,
      started: false,
    }
    event.currentTarget.setPointerCapture?.(event.pointerId)
  }

  const onPointerMove = (event: PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current
    if (!drag || drag.pointerId !== event.pointerId) return
    const delta = drag.startY - event.clientY
    if (!drag.started) {
      if (Math.abs(delta) < POINTER_DRAG_THRESHOLD_PX) return
      drag.started = true
      cardRef.current?.classList.add('is-resizing')
      document.documentElement.classList.add('is-resizing-interaction-card')
    }
    drag.current = clamp(drag.startHeight + delta, drag.min, drag.max)
    scheduleHeight(drag.current)
  }

  const onPointerUp = (event: PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current
    if (!drag || drag.pointerId !== event.pointerId) return
    if (drag.started && Number.isFinite(event.clientY)) {
      drag.current = clamp(
        drag.startHeight + drag.startY - event.clientY,
        drag.min,
        drag.max,
      )
      pendingHeightRef.current = drag.current
    }
    finishPointerResize()
    if (event.currentTarget.hasPointerCapture?.(event.pointerId)) {
      event.currentTarget.releasePointerCapture?.(event.pointerId)
    }
  }

  const onPointerCancel = (event: PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current
    if (!drag || drag.pointerId !== event.pointerId) return
    pendingHeightRef.current = null
    if (frameRef.current != null) {
      window.cancelAnimationFrame(frameRef.current)
      frameRef.current = null
    }
    applyHeight(drag.startHeight, drag)
    dragRef.current = null
    clearResizeState()
  }

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (!['ArrowUp', 'ArrowDown', 'Home', 'End'].includes(event.key)) return
    event.preventDefault()
    event.stopPropagation()
    const bounds = syncBounds()
    const step = event.shiftKey ? KEYBOARD_LARGE_STEP_PX : KEYBOARD_STEP_PX
    const next = event.key === 'Home'
      ? bounds.min
      : event.key === 'End'
        ? bounds.max
        : bounds.current + (event.key === 'ArrowUp' ? step : -step)
    applyHeight(next, bounds)
  }

  return (
    <div
      className="interaction-card-resize-handle"
      role="separator"
      tabIndex={0}
      aria-label={t('调整交互卡片高度')}
      aria-controls={controls}
      aria-orientation="horizontal"
      aria-valuemin={metrics.min > 0 ? Math.round(metrics.min) : undefined}
      aria-valuemax={metrics.max > 0 ? Math.round(metrics.max) : undefined}
      aria-valuenow={metrics.current > 0 ? Math.round(metrics.current) : undefined}
      onFocus={syncBounds}
      onKeyDown={onKeyDown}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerCancel}
      onLostPointerCapture={finishPointerResize}
    />
  )
}

/* eslint-enable jsx-a11y/no-noninteractive-element-interactions, jsx-a11y/no-noninteractive-tabindex */
