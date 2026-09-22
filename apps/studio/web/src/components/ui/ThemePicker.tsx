import { useGSAP } from '@gsap/react'
import gsap from 'gsap'
import { Monitor, MoonStar, Sun } from 'lucide-react'
import { useCallback, useEffect, useId, useRef, useState } from 'react'

import type { ThemePreference } from '../../theme'
import { useI18n } from '../../i18n'
import { MOTION_DURATION_MS, MOTION_DURATION_SECONDS } from './motion'
import { useThemePreference } from './useThemePreference'

gsap.registerPlugin(useGSAP)

const THEME_OPTIONS = [
  { value: 'system', label: '跟随系统', icon: Monitor },
  { value: 'light', label: '浅色', icon: Sun },
  { value: 'dark', label: '深色', icon: MoonStar },
] as const satisfies ReadonlyArray<{
  value: ThemePreference
  label: string
  icon: typeof Monitor
}>

export function ThemePicker() {
  const { t } = useI18n()
  const { preference, selectPreference } = useThemePreference()
  const [isExpanded, setIsExpanded] = useState(false)
  const [isExpansionSettled, setExpansionSettled] = useState(false)
  const optionName = useId()
  const switcherRef = useRef<HTMLFieldSetElement>(null)
  const expansionAnimationsRef = useRef<gsap.core.Animation[]>([])
  const isExpandedRef = useRef(false)
  const expansionSettleTimerRef = useRef<number | null>(null)
  const lastPointerTypeRef = useRef<'keyboard' | 'mouse' | 'pen' | 'touch'>('keyboard')

  const updateExpansion = useCallback((nextExpanded: boolean) => {
    if (expansionSettleTimerRef.current != null) {
      window.clearTimeout(expansionSettleTimerRef.current)
      expansionSettleTimerRef.current = null
    }
    isExpandedRef.current = nextExpanded
    setIsExpanded(nextExpanded)
    if (!nextExpanded) {
      setExpansionSettled(false)
    } else if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      setExpansionSettled(true)
    } else {
      setExpansionSettled(false)
      expansionSettleTimerRef.current = window.setTimeout(() => {
        if (isExpandedRef.current) setExpansionSettled(true)
        expansionSettleTimerRef.current = null
      }, MOTION_DURATION_MS.slow)
    }
    expansionAnimationsRef.current.forEach((animation) => {
      if (nextExpanded) animation.play()
      else animation.reverse()
    })
  }, [])

  useEffect(() => () => {
    if (expansionSettleTimerRef.current != null) {
      window.clearTimeout(expansionSettleTimerRef.current)
    }
  }, [])

  useEffect(() => {
    if (!isExpanded) return
    const handleOutsidePointer = (event: PointerEvent) => {
      if (event.target instanceof Node && switcherRef.current?.contains(event.target)) return
      updateExpansion(false)
    }
    const handleEscape = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.key !== 'Escape') return
      event.preventDefault()
      updateExpansion(false)
    }
    document.addEventListener('pointerdown', handleOutsidePointer)
    document.addEventListener('keydown', handleEscape)
    return () => {
      document.removeEventListener('pointerdown', handleOutsidePointer)
      document.removeEventListener('keydown', handleEscape)
    }
  }, [isExpanded, updateExpansion])

  useGSAP(() => {
    const switcher = switcherRef.current
    const surface = switcher?.querySelector<HTMLElement>('.theme-switcher-surface')
    const options = switcher
      ? [...switcher.querySelectorAll<HTMLElement>('.theme-switcher-option')]
      : []
    if (!surface || options.length === 0) return

    const media = gsap.matchMedia()
    media.add({
      allowMotion: '(prefers-reduced-motion: no-preference)',
      reduceMotion: '(prefers-reduced-motion: reduce)',
    }, (context) => {
      // 减少动态效果时由样式直接表达展开状态，零时长 tween 无法区分首尾进度
      if (context.conditions?.reduceMotion) {
        if (expansionSettleTimerRef.current != null) {
          window.clearTimeout(expansionSettleTimerRef.current)
          expansionSettleTimerRef.current = null
        }
        setExpansionSettled(isExpandedRef.current)
        return
      }
      const duration = MOTION_DURATION_SECONDS.normal
      const animations = [
        gsap.to(surface, {
          scaleX: 1,
          autoAlpha: 1,
          duration,
          ease: 'power2.out',
          overwrite: 'auto',
          paused: true,
        }),
        gsap.to(options, {
          x: 0,
          autoAlpha: 1,
          duration,
          ease: 'power2.out',
          stagger: { each: MOTION_DURATION_SECONDS.fast / 2, from: 'end' },
          overwrite: 'auto',
          paused: true,
        }),
      ]
      const initialProgress = isExpandedRef.current ? 1 : 0
      animations.forEach((animation) => animation.progress(initialProgress))
      expansionAnimationsRef.current = animations

      return () => {
        expansionAnimationsRef.current = []
      }
    })

    return () => media.revert()
  }, {
    dependencies: [preference],
    scope: switcherRef,
    revertOnUpdate: true,
  })

  return (
    <fieldset
      ref={switcherRef}
      className={`theme-switcher${isExpanded ? ' is-expanded' : ''}${isExpansionSettled ? ' is-settled' : ''}`}
      data-expanded={isExpanded}
      data-settled={isExpansionSettled}
      onPointerEnter={() => {
        if (!isExpandedRef.current) updateExpansion(true)
      }}
      onPointerLeave={(event) => {
        if (event.pointerType === 'touch') return
        const activeElement = document.activeElement
        if (activeElement instanceof Node && event.currentTarget.contains(activeElement)) return
        updateExpansion(false)
      }}
      onPointerDownCapture={(event) => {
        const pointerType = event.pointerType || 'mouse'
        lastPointerTypeRef.current = pointerType as 'mouse' | 'pen' | 'touch'
        if (pointerType === 'touch' && !isExpandedRef.current) updateExpansion(true)
      }}
      onKeyDownCapture={() => {
        lastPointerTypeRef.current = 'keyboard'
      }}
      onFocusCapture={() => {
        if (!isExpandedRef.current) updateExpansion(true)
      }}
      onBlurCapture={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget as Node | null)) updateExpansion(false)
      }}
    >
      <legend className="theme-switcher-legend">{t('主题')}</legend>
      <span className="theme-switcher-panel">
        <span className="theme-switcher-circle" aria-hidden="true" />
        <span className="theme-switcher-surface" aria-hidden="true" />
        {THEME_OPTIONS.map((option) => {
          const OptionIcon = option.icon
          const optionId = `${optionName}-${option.value}`
          const isSelected = preference === option.value
          return (
            <label
              key={option.value}
              className={`theme-switcher-option${isSelected ? ' is-selected' : ''}`}
              data-theme-option={option.value}
              htmlFor={optionId}
              title={t(option.label)}
            >
              <input
                id={optionId}
                className="theme-switcher-input"
                type="radio"
                name={optionName}
                value={option.value}
                aria-label={t(option.label)}
                checked={isSelected}
                onChange={() => {
                  selectPreference(option.value)
                  updateExpansion(false)
                }}
                onClick={(event) => {
                  if (lastPointerTypeRef.current === 'touch' && !isExpansionSettled) {
                    event.preventDefault()
                  }
                }}
              />
              <span className="theme-switcher-visual" aria-hidden="true">
                <OptionIcon size={16} />
              </span>
            </label>
          )
        })}
      </span>
    </fieldset>
  )
}
