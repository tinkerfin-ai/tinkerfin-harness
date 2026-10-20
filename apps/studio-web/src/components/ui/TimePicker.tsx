import { Portal } from '@zag-js/react'
import { Clock3 } from 'lucide-react'
import {
  forwardRef,
  useEffect,
  useContext,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type ButtonHTMLAttributes,
  type CSSProperties,
  type KeyboardEvent,
} from 'react'

import { useI18n } from '../../i18n'
import { DialogContext } from './DialogContext'

export type TimePickerSize = 'xs' | 'md' | 'lg'

/** 提供与日期选择器一致的分钟精度时间选择和范围约束 */
export interface TimePickerProps extends Omit<
  ButtonHTMLAttributes<HTMLButtonElement>,
  'children' | 'onChange' | 'onClick' | 'onKeyDown' | 'type' | 'value'
> {
  value: string
  label: string
  onChange: (value: string) => void
  controlSize?: TimePickerSize
  placeholder?: string
  min?: string
  max?: string
}

interface TimeParts {
  hour: number
  minute: number
}

const HOURS = Array.from({ length: 24 }, (_, hour) => hour)
const MINUTES = Array.from({ length: 60 }, (_, minute) => minute)
const VIEWPORT_PADDING = 12
const POPOVER_GUTTER = 8

const parseTime = (value: string | undefined): TimeParts | undefined => {
  const match = /^([01]\d|2[0-3]):([0-5]\d)$/.exec(value ?? '')
  const hour = Number(match?.[1])
  const minute = Number(match?.[2])
  return Number.isInteger(hour) && Number.isInteger(minute) ? { hour, minute } : undefined
}

const timeMinute = (parts: TimeParts) => parts.hour * 60 + parts.minute
const formatPart = (value: number) => String(value).padStart(2, '0')
const formatTime = (parts: TimeParts) => `${formatPart(parts.hour)}:${formatPart(parts.minute)}`

const clamp = (value: number, minimum: number, maximum: number) => (
  Math.min(maximum, Math.max(minimum, value))
)

const allowedMinutesForHour = (
  hour: number,
  minimum: number,
  maximum: number,
) => {
  const hourStart = hour * 60
  const first = Math.max(hourStart, minimum)
  const last = Math.min(hourStart + 59, maximum)
  return first <= last
    ? { first: first - hourStart, last: last - hourStart }
    : undefined
}

const focusOption = (
  refs: Array<HTMLButtonElement | null>,
  index: number,
) => {
  const target = refs[index]
  target?.focus()
  target?.scrollIntoView?.({ block: 'nearest' })
}

export const TimePicker = forwardRef<HTMLButtonElement, TimePickerProps>(function TimePicker({
  value,
  label,
  onChange,
  controlSize = 'md',
  placeholder = '--:--',
  min,
  max,
  disabled,
  name,
  className,
  ...buttonProps
}, forwardedRef) {
  const { t } = useI18n()
  const inDialog = useContext(DialogContext)
  const pickerId = useId()
  const panelId = `time-picker-${pickerId}`
  const hourLabelId = `${panelId}-hours-label`
  const minuteLabelId = `${panelId}-minutes-label`
  const triggerRef = useRef<HTMLButtonElement>(null)
  const panelRef = useRef<HTMLDivElement>(null)
  const hourRefs = useRef<Array<HTMLButtonElement | null>>([])
  const minuteRefs = useRef<Array<HTMLButtonElement | null>>([])
  const openingHourRef = useRef(0)
  const [open, setOpen] = useState(false)
  const [draftHour, setDraftHour] = useState(0)
  const [draftMinute, setDraftMinute] = useState(0)
  const [position, setPosition] = useState<{ top: number; left: number }>()
  const parsedValue = parseTime(value)
  const minimum = timeMinute(parseTime(min) ?? { hour: 0, minute: 0 })
  const maximum = timeMinute(parseTime(max) ?? { hour: 23, minute: 59 })
  const rangeIsValid = minimum <= maximum
  const pickerDisabled = disabled || !rangeIsValid

  const setTriggerRef = (node: HTMLButtonElement | null) => {
    triggerRef.current = node
    if (typeof forwardedRef === 'function') forwardedRef(node)
    else if (forwardedRef) forwardedRef.current = node
  }

  const initialDraft = () => {
    const currentMinute = parsedValue ? timeMinute(parsedValue) : minimum
    const bounded = clamp(currentMinute, minimum, maximum)
    return { hour: Math.floor(bounded / 60), minute: bounded % 60 }
  }

  const openPicker = () => {
    if (pickerDisabled) return
    const initial = initialDraft()
    openingHourRef.current = initial.hour
    setDraftHour(initial.hour)
    setDraftMinute(initial.minute)
    setOpen(true)
  }

  const closeAndFocusTrigger = () => {
    triggerRef.current?.focus()
    setOpen(false)
  }

  const chooseHour = (hour: number, focusMinute = true) => {
    const allowed = allowedMinutesForHour(hour, minimum, maximum)
    if (!allowed) return
    const minute = clamp(draftMinute, allowed.first, allowed.last)
    setDraftHour(hour)
    setDraftMinute(minute)
    if (focusMinute) {
      window.requestAnimationFrame(() => focusOption(minuteRefs.current, minute))
    }
  }

  const chooseMinute = (minute: number) => {
    const allowed = allowedMinutesForHour(draftHour, minimum, maximum)
    if (!allowed || minute < allowed.first || minute > allowed.last) return
    onChange(formatTime({ hour: draftHour, minute }))
    closeAndFocusTrigger()
  }

  const moveOption = (
    event: KeyboardEvent<HTMLButtonElement>,
    options: number[],
    current: number,
    choose: (value: number) => void,
    refs: Array<HTMLButtonElement | null>,
  ) => {
    const key = event.key
    if (key === 'Escape') {
      event.preventDefault()
      closeAndFocusTrigger()
      return
    }
    if (key === 'Tab') {
      event.preventDefault()
      // 按视觉顺序切换两列，离开选项时回到入口，由表单继续管理焦点
      const next = event.shiftKey ? hourRefs.current[draftHour] : minuteRefs.current[draftMinute]
      if (next === event.currentTarget) closeAndFocusTrigger()
      else next?.focus()
      return
    }
    if (!['ArrowDown', 'ArrowRight', 'ArrowUp', 'ArrowLeft', 'Home', 'End'].includes(key)) {
      return
    }
    event.preventDefault()
    const currentIndex = Math.max(0, options.indexOf(current))
    const nextIndex = key === 'Home'
      ? 0
      : key === 'End'
        ? options.length - 1
        : (currentIndex + (key === 'ArrowDown' || key === 'ArrowRight' ? 1 : -1) + options.length)
          % options.length
    const next = options[nextIndex]
    if (next == null) return
    choose(next)
    window.requestAnimationFrame(() => focusOption(refs, next))
  }

  useLayoutEffect(() => {
    if (!open) return
    const place = () => {
      const trigger = triggerRef.current
      const panel = panelRef.current
      if (!trigger || !panel) return
      const triggerRect = trigger.getBoundingClientRect()
      const panelRect = panel.getBoundingClientRect()
      const left = clamp(
        triggerRect.right - panelRect.width,
        VIEWPORT_PADDING,
        Math.max(VIEWPORT_PADDING, window.innerWidth - panelRect.width - VIEWPORT_PADDING),
      )
      const above = triggerRect.top - panelRect.height - POPOVER_GUTTER
      const below = triggerRect.bottom + POPOVER_GUTTER
      const top = above >= VIEWPORT_PADDING
        ? above
        : Math.min(
            below,
            Math.max(VIEWPORT_PADDING, window.innerHeight - panelRect.height - VIEWPORT_PADDING),
          )
      setPosition({ top, left })
    }
    place()
    const frame = window.requestAnimationFrame(place)
    window.addEventListener('resize', place)
    document.addEventListener('scroll', place, true)
    return () => {
      window.cancelAnimationFrame(frame)
      window.removeEventListener('resize', place)
      document.removeEventListener('scroll', place, true)
    }
  }, [open])

  useLayoutEffect(() => {
    if (!open) return
    const frame = window.requestAnimationFrame(() => (
      focusOption(hourRefs.current, openingHourRef.current)
    ))
    return () => window.cancelAnimationFrame(frame)
  }, [open])

  useEffect(() => {
    if (!open) return
    const handleOutsidePointer = (event: PointerEvent) => {
      if (!(event.target instanceof Node)) return
      if (triggerRef.current?.contains(event.target) || panelRef.current?.contains(event.target)) return
      setOpen(false)
    }
    const handleEscape = (event: globalThis.KeyboardEvent) => {
      if (event.defaultPrevented || event.key !== 'Escape') return
      event.preventDefault()
      closeAndFocusTrigger()
    }
    document.addEventListener('pointerdown', handleOutsidePointer)
    document.addEventListener('keydown', handleEscape)
    return () => {
      document.removeEventListener('pointerdown', handleOutsidePointer)
      document.removeEventListener('keydown', handleEscape)
    }
  }, [open])

  useEffect(() => {
    if (pickerDisabled && open) setOpen(false)
  }, [open, pickerDisabled])

  const displayValue = parsedValue ? formatTime(parsedValue) : placeholder
  const triggerClasses = [
    'ui-temporal-picker__trigger',
    `ui-temporal-picker__trigger--${controlSize}`,
    'ui-time-picker__trigger',
    parsedValue ? '' : 'is-placeholder',
    className,
  ].filter(Boolean).join(' ')
  const allowedHours = HOURS.filter((hour) => (
    Boolean(allowedMinutesForHour(hour, minimum, maximum))
  ))
  const allowedMinutes = MINUTES.filter((minute) => {
    const allowed = allowedMinutesForHour(draftHour, minimum, maximum)
    return Boolean(allowed && minute >= allowed.first && minute <= allowed.last)
  })
  const positionStyle: CSSProperties = position
    ? { top: position.top, left: position.left }
    : { top: 0, left: 0, visibility: 'hidden' }

  return (
    <span className="ui-temporal-picker ui-time-picker">
      <span className="ui-temporal-picker__control ui-time-picker__control">
        {name && <input type="hidden" name={name} value={value} />}
        <button
          type="button"
          {...buttonProps}
          ref={setTriggerRef}
          className={triggerClasses}
          aria-label={label}
          aria-haspopup="dialog"
          aria-expanded={open}
          aria-controls={open ? panelId : undefined}
          disabled={pickerDisabled}
          onClick={() => open ? closeAndFocusTrigger() : openPicker()}
          onKeyDown={(event) => {
            if (open && event.key === 'Escape') {
              event.preventDefault()
              closeAndFocusTrigger()
            }
          }}
        >
          <span>{displayValue}</span>
          <Clock3 size={16} aria-hidden="true" />
        </button>
      </span>
      {open && (
        <Portal>
          <div className={`ui-time-picker__positioner${inDialog ? ' is-in-dialog' : ''}`} style={positionStyle}>
            <div
              ref={panelRef}
              id={panelId}
              className={`ui-temporal-picker__popover ui-time-picker__popover${inDialog ? ' is-in-dialog' : ''}`}
              role="dialog"
              aria-label={t('选择时间')}
            >
              <div className="ui-time-picker__columns">
                <section className="ui-time-picker__column" aria-labelledby={hourLabelId}>
                  <h3 id={hourLabelId}>{t('小时')}</h3>
                  <div
                    className="ui-time-picker__list ui-scrollbar"
                    role="listbox"
                    aria-labelledby={hourLabelId}
                  >
                    {HOURS.map((hour) => {
                      const allowed = allowedHours.includes(hour)
                      return (
                        <button
                          key={hour}
                          ref={(node) => { hourRefs.current[hour] = node }}
                          type="button"
                          role="option"
                          className={`ui-time-picker__option${draftHour === hour ? ' is-selected' : ''}`}
                          aria-selected={draftHour === hour}
                          tabIndex={draftHour === hour ? 0 : -1}
                          disabled={!allowed}
                          onClick={() => chooseHour(hour)}
                          onKeyDown={(event) => moveOption(
                            event,
                            allowedHours,
                            hour,
                            (next) => chooseHour(next, false),
                            hourRefs.current,
                          )}
                        >
                          {formatPart(hour)}
                        </button>
                      )
                    })}
                  </div>
                </section>
                <section className="ui-time-picker__column" aria-labelledby={minuteLabelId}>
                  <h3 id={minuteLabelId}>{t('分钟')}</h3>
                  <div
                    className="ui-time-picker__list ui-scrollbar"
                    role="listbox"
                    aria-labelledby={minuteLabelId}
                  >
                    {MINUTES.map((minute) => {
                      const allowed = allowedMinutes.includes(minute)
                      return (
                        <button
                          key={minute}
                          ref={(node) => { minuteRefs.current[minute] = node }}
                          type="button"
                          role="option"
                          className={`ui-time-picker__option${draftMinute === minute ? ' is-selected' : ''}`}
                          aria-selected={draftMinute === minute}
                          tabIndex={draftMinute === minute ? 0 : -1}
                          disabled={!allowed}
                          onClick={() => chooseMinute(minute)}
                          onKeyDown={(event) => moveOption(
                            event,
                            allowedMinutes,
                            minute,
                            setDraftMinute,
                            minuteRefs.current,
                          )}
                        >
                          {formatPart(minute)}
                        </button>
                      )
                    })}
                  </div>
                </section>
              </div>
            </div>
          </div>
        </Portal>
      )}
    </span>
  )
})
