import { useGSAP } from '@gsap/react'
import gsap from 'gsap'
import { X } from 'lucide-react'
import { useCallback, useEffect, useRef } from 'react'

import { MOTION_DURATION_SECONDS } from './motion'
import { useI18n } from '../../i18n'
import type { TranslationKey } from '../../i18n'
import { FeedbackIcon } from './FeedbackState'

gsap.registerPlugin(useGSAP)

export type ToastKind = 'success' | 'info' | 'error' | 'warning'

export interface ToastItem {
  id: string
  kind: ToastKind
  message: string
}

export type ToastHandler = (kind: ToastKind, message: string) => void

const TOAST_TITLE: TranslationKey = '提示'

const TOAST_DURATION_MS: Record<ToastItem['kind'], number> = {
  success: 6000,
  info: 6000,
  error: 6000,
  warning: 6000,
}

function ToastCard({
  toast,
  onDismiss,
}: {
  toast: ToastItem
  onDismiss: (id: string) => void
}) {
  const { t } = useI18n()
  const duration = TOAST_DURATION_MS[toast.kind]
  const title = t(TOAST_TITLE)
  const cardRef = useRef<HTMLLIElement>(null)
  const timerRef = useRef<number | null>(null)
  const startedAtRef = useRef(0)
  const remainingRef = useRef(duration)
  const isDismissingRef = useRef(false)
  const isPointerInsideRef = useRef(false)
  const hasFocusWithinRef = useRef(false)
  const requestDismissRef = useRef<() => void>(() => undefined)
  const dismissRef = useRef(onDismiss)
  dismissRef.current = onDismiss

  const clearTimer = useCallback(() => {
    if (timerRef.current === null) return
    window.clearTimeout(timerRef.current)
    timerRef.current = null
  }, [])

  const { contextSafe } = useGSAP(() => {
    const card = cardRef.current
    if (!card) return

    const media = gsap.matchMedia()
    media.add({
      allowMotion: '(prefers-reduced-motion: no-preference)',
      reduceMotion: '(prefers-reduced-motion: reduce)',
    }, (context) => {
      if (context.conditions?.reduceMotion) {
        gsap.set(card, { autoAlpha: 1, x: 0, scale: 1 })
        return
      }

      gsap.fromTo(card, {
        autoAlpha: 0,
        xPercent: 100,
        x: 12,
        scale: 0.985,
      }, {
        autoAlpha: 1,
        xPercent: 0,
        x: 0,
        scale: 1,
        duration: MOTION_DURATION_SECONDS.normal,
        ease: 'power3.out',
        clearProps: 'transform,opacity,visibility',
      })
    })

    return () => media.revert()
  }, { scope: cardRef })

  const requestDismiss = contextSafe(() => {
    if (isDismissingRef.current) return
    isDismissingRef.current = true
    clearTimer()

    const finishDismiss = () => dismissRef.current(toast.id)
    const card = cardRef.current
    if (!card || window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      finishDismiss()
      return
    }

    gsap.to(card, {
      autoAlpha: 0,
      y: -6,
      scale: 0.985,
      duration: MOTION_DURATION_SECONDS.fast,
      ease: 'power2.in',
      overwrite: 'auto',
      onComplete: finishDismiss,
    })
  })
  requestDismissRef.current = requestDismiss

  const scheduleDismiss = useCallback(() => {
    if (remainingRef.current <= 0) {
      requestDismissRef.current()
      return
    }
    startedAtRef.current = Date.now()
    timerRef.current = window.setTimeout(() => {
      timerRef.current = null
      remainingRef.current = 0
      requestDismissRef.current()
    }, remainingRef.current)
  }, [])

  const pauseDismiss = useCallback(() => {
    if (timerRef.current === null) return
    remainingRef.current = Math.max(
      0,
      remainingRef.current - (Date.now() - startedAtRef.current),
    )
    clearTimer()
  }, [clearTimer])

  const resumeDismiss = useCallback(() => {
    if (
      isPointerInsideRef.current
      || hasFocusWithinRef.current
      || timerRef.current !== null
      || isDismissingRef.current
    ) return
    scheduleDismiss()
  }, [scheduleDismiss])

  useEffect(() => {
    isDismissingRef.current = false
    remainingRef.current = duration
    scheduleDismiss()
    return clearTimer
  }, [clearTimer, duration, scheduleDismiss])

  const message = toast.message.replace(/。$/, '')

  return (
    <li
      ref={cardRef}
      className={`toast-card is-${toast.kind}`}
      onMouseEnter={() => {
        isPointerInsideRef.current = true
        pauseDismiss()
      }}
      onMouseLeave={() => {
        isPointerInsideRef.current = false
        resumeDismiss()
      }}
      onFocusCapture={() => {
        hasFocusWithinRef.current = true
        pauseDismiss()
      }}
      onBlurCapture={(event) => {
        if (event.relatedTarget instanceof Node && event.currentTarget.contains(event.relatedTarget)) {
          return
        }
        hasFocusWithinRef.current = false
        resumeDismiss()
      }}
    >
      <FeedbackIcon kind={toast.kind} />
      <div className="toast-card__copy">
        <p className="toast-card__title">{title}</p>
        <p className="toast-card__message" role="status">{message}</p>
      </div>
      <button type="button" aria-label={t('关闭提示：{message}', { message })} onClick={() => requestDismissRef.current()}>
        <X size={16} />
      </button>
    </li>
  )
}

export function ToastViewport({
  toasts,
  onDismiss,
}: {
  toasts: ToastItem[]
  onDismiss: (id: string) => void
}) {
  const { t } = useI18n()
  if (toasts.length === 0) return null

  return (
    <ol className="toast-viewport" aria-live="polite" aria-label={t('系统提示')}>
      {toasts.map((toast) => <ToastCard key={toast.id} toast={toast} onDismiss={onDismiss} />)}
    </ol>
  )
}
