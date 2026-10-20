import { useId } from 'react'
import type { KeyboardEvent } from 'react'

export type ViewTabsDensity = 'regular' | 'medium' | 'compact'

export interface ViewTabOption<Value extends string> {
  value: Value
  label: string
  controls?: string
  disabled?: boolean
}

export interface ViewTabsProps<Value extends string> {
  value: Value
  options: readonly ViewTabOption<Value>[]
  label: string
  onChange: (value: Value) => void
  density?: ViewTabsDensity
  className?: string
}

/** 在同一内容区域的互斥视图间切换，并统一键盘与焦点行为 */
export function ViewTabs<Value extends string>({
  value,
  options,
  label,
  onChange,
  density = 'regular',
  className,
}: ViewTabsProps<Value>) {
  const tabsId = useId()
  const classes = [
    'ui-view-tabs',
    `ui-view-tabs--${density}`,
    className,
  ].filter(Boolean).join(' ')

  const selectFromKeyboard = (
    event: KeyboardEvent<HTMLButtonElement>,
    optionIndex: number,
  ) => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
    event.preventDefault()
    const enabled = options
      .map((option, index) => ({ option, index }))
      .filter(({ option }) => !option.disabled)
    if (enabled.length === 0) return
    const enabledIndex = enabled.findIndex(({ index }) => index === optionIndex)
    const next = event.key === 'Home'
      ? enabled[0]
      : event.key === 'End'
        ? enabled.at(-1)
        : enabled[(enabledIndex + (event.key === 'ArrowRight' ? 1 : -1) + enabled.length) % enabled.length]
    if (!next) return
    onChange(next.option.value)
    event.currentTarget.parentElement
      ?.querySelectorAll<HTMLButtonElement>('[role="tab"]')
      .item(next.index)
      .focus()
  }

  return (
    <div className={classes} role="tablist" aria-label={label}>
      {options.map((option, index) => (
        <button
          key={option.value}
          id={`${tabsId}-${index}`}
          type="button"
          role="tab"
          aria-selected={option.value === value}
          aria-controls={option.controls}
          tabIndex={option.value === value ? 0 : -1}
          disabled={option.disabled}
          onClick={() => onChange(option.value)}
          onKeyDown={(event) => selectFromKeyboard(event, index)}
        >
          {option.label}
        </button>
      ))}
    </div>
  )
}
