import { parseDate, type CalendarDate } from '@internationalized/date'
import * as datePicker from '@zag-js/date-picker'
import { normalizeProps, Portal, useMachine } from '@zag-js/react'
import { CalendarDays, ChevronLeft, ChevronRight } from 'lucide-react'
import {
  forwardRef,
  useId,
  useContext,
  useRef,
  type ButtonHTMLAttributes,
} from 'react'

import { useI18n } from '../../i18n'
import { DialogContext } from './DialogContext'

export type DatePickerSize = 'xs' | 'md' | 'lg'

export interface DatePickerProps extends Omit<
  ButtonHTMLAttributes<HTMLButtonElement>,
  'children' | 'onChange' | 'onClick' | 'onKeyDown' | 'type' | 'value'
> {
  value: string
  label: string
  onChange: (value: string) => void
  controlSize?: DatePickerSize
  placeholder?: string
  min?: string
  max?: string
}

const parseCalendarDate = (value: string | undefined): CalendarDate | undefined => {
  if (!value) return undefined
  try {
    return parseDate(value)
  } catch {
    return undefined
  }
}

export const DatePicker = forwardRef<HTMLButtonElement, DatePickerProps>(function DatePicker({
  value,
  label,
  onChange,
  controlSize = 'md',
  placeholder,
  min,
  max,
  disabled,
  name,
  className,
  ...buttonProps
}, forwardedRef) {
  const { locale, t } = useI18n()
  const inDialog = useContext(DialogContext)
  const pickerId = useId()
  const triggerRef = useRef<HTMLButtonElement>(null)
  const parsedValue = parseCalendarDate(value)
  const service = useMachine(datePicker.machine, {
    id: pickerId,
    locale,
    selectionMode: 'single',
    value: parsedValue ? [parsedValue] : [],
    min: parseCalendarDate(min),
    max: parseCalendarDate(max),
    disabled,
    closeOnSelect: true,
    outsideDaySelectable: true,
    fixedWeeks: true,
    startOfWeek: 1,
    minView: 'day',
    maxView: 'year',
    positioning: {
      placement: 'top-end',
      strategy: 'fixed',
      gutter: 8,
      // 窄屏七列日期保留完整触控区域，浮层在视口边缘仍可贴近显示
      overflowPadding: 1,
    },
    translations: {
      content: t('选择日期'),
      trigger: () => label,
      nextTrigger: (view) => view === 'day' ? t('下个月') : view === 'month' ? t('下一年') : t('下一组年份'),
      prevTrigger: (view) => view === 'day' ? t('上个月') : view === 'month' ? t('上一年') : t('上一组年份'),
      viewTrigger: (view) => view === 'day'
        ? t('选择月份和年份')
        : view === 'month'
          ? t('选择年份')
          : t('返回月份选择'),
      monthSelect: t('选择月份'),
      yearSelect: t('选择年份'),
    },
    onValueChange: (details) => {
      const nextValue = details.value[0]?.toString()
      if (nextValue && nextValue !== value) onChange(nextValue)
    },
  })
  const api = datePicker.connect(service, normalizeProps)

  const setTriggerRef = (node: HTMLButtonElement | null) => {
    triggerRef.current = node
    if (typeof forwardedRef === 'function') forwardedRef(node)
    else if (forwardedRef) forwardedRef.current = node
  }

  const displayValue = api.value[0]
    ? api.format(api.value[0], { year: 'numeric', month: '2-digit', day: '2-digit' })
    : placeholder ?? t('年 / 月 / 日')
  const triggerClasses = [
    'ui-temporal-picker__trigger',
    `ui-temporal-picker__trigger--${controlSize}`,
    'ui-date-picker__trigger',
    api.value.length === 0 ? 'is-placeholder' : '',
    className,
  ].filter(Boolean).join(' ')
  const caption = api.view === 'day'
    ? api.format(api.visibleRange.start, { year: 'numeric', month: 'long' })
    : api.view === 'month'
      ? api.format(api.visibleRange.start, { year: 'numeric' })
      : (() => {
          const decade = api.getDecade()
          return `${decade.start ?? ''}–${decade.end ?? ''}`
        })()

  return (
    <span {...api.getRootProps()} className="ui-temporal-picker ui-date-picker">
      <span {...api.getControlProps()} className="ui-temporal-picker__control ui-date-picker__control">
        {name && <input type="hidden" name={name} value={value} />}
        <button
          type="button"
          {...buttonProps}
          {...api.getTriggerProps()}
          ref={setTriggerRef}
          className={triggerClasses}
          aria-label={label}
          disabled={disabled}
        >
          <span>{displayValue}</span>
          <CalendarDays size={16} aria-hidden="true" />
        </button>
      </span>
      <Portal>
        <div {...api.getPositionerProps()}>
          <div {...api.getContentProps()} className={`ui-temporal-picker__popover ui-date-picker__popover${inDialog ? ' is-in-dialog' : ''}`}>
              <header {...api.getViewControlProps({ view: api.view })} className="ui-date-picker__head">
                <button type="button" {...api.getPrevTriggerProps({ view: api.view })} className="ui-date-picker__nav">
                  <ChevronLeft size={17} aria-hidden="true" />
                </button>
                <button type="button" {...api.getViewTriggerProps({ view: api.view })} className="ui-date-picker__caption">
                  <span>{caption}</span>
                </button>
                <button type="button" {...api.getNextTriggerProps({ view: api.view })} className="ui-date-picker__nav">
                  <ChevronRight size={17} aria-hidden="true" />
                </button>
              </header>

              {api.view === 'day' && (
                <div {...api.getViewProps({ view: 'day' })}>
                  <div className="ui-date-picker__weekdays" aria-hidden="true">
                    {api.weekDays.map((weekday) => <span key={weekday.short}>{weekday.short}</span>)}
                  </div>
                  <div {...api.getTableProps({ view: 'day', columns: 7 })} className="ui-date-picker__grid">
                    {api.weeks.flat().map((date) => {
                      const cellProps = { value: date, visibleRange: api.visibleRange }
                      const state = api.getDayTableCellState(cellProps)
                      return (
                        <span {...api.getDayTableCellProps(cellProps)} key={date.toString()}>
                          <div
                            {...api.getDayTableCellTriggerProps(cellProps)}
                            className={`ui-date-picker__day${state.selected ? ' is-selected' : ''}${state.outsideRange ? ' is-outside' : ''}${state.today ? ' is-today' : ''}`}
                            data-date-value={date.toString()}
                          >
                            {date.day}
                          </div>
                        </span>
                      )
                    })}
                  </div>
                </div>
              )}

              {api.view === 'month' && (
                <div {...api.getViewProps({ view: 'month' })}>
                  <div {...api.getTableProps({ view: 'month', columns: 3 })} className="ui-date-picker__period-grid">
                    {api.getMonthsGrid({ columns: 3, format: 'short' }).flat().map((month) => {
                      const cellProps = { value: month.value, columns: 3 }
                      const state = api.getMonthTableCellState(cellProps)
                      return (
                        <span {...api.getMonthTableCellProps(cellProps)} key={month.value}>
                          <div
                            {...api.getMonthTableCellTriggerProps(cellProps)}
                            className={`ui-date-picker__period${state.selected ? ' is-selected' : ''}`}
                            data-month-value={month.value}
                          >
                            {month.label}
                          </div>
                        </span>
                      )
                    })}
                  </div>
                </div>
              )}

              {api.view === 'year' && (
                <div {...api.getViewProps({ view: 'year' })}>
                  <div {...api.getTableProps({ view: 'year', columns: 3 })} className="ui-date-picker__period-grid">
                    {api.getYearsGrid({ columns: 3 }).flat().map((year) => {
                      const cellProps = { value: year.value, columns: 3 }
                      const state = api.getYearTableCellState(cellProps)
                      return (
                        <span {...api.getYearTableCellProps(cellProps)} key={year.value}>
                          <div
                            {...api.getYearTableCellTriggerProps(cellProps)}
                            className={`ui-date-picker__period${state.selected ? ' is-selected' : ''}`}
                            data-year-value={year.value}
                          >
                            {year.label}
                          </div>
                        </span>
                      )
                    })}
                  </div>
                </div>
              )}
          </div>
        </div>
      </Portal>
    </span>
  )
})
