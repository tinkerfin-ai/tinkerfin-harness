import {
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent,
  type ReactNode,
} from 'react'
import { createPortal } from 'react-dom'

export interface ListboxPickerProps<T extends string> {
  value: T
  options: readonly T[]
  open: boolean
  onOpenChange: (open: boolean) => void
  onChange: (value: T) => void
  triggerLabel: string
  listboxLabel: string
  rootClassName: string
  triggerClassName: string
  listboxClassName: string
  optionClassName?: string
  listboxPortalTarget?: Element | null
  listboxStyle?: CSSProperties
  disabled?: boolean
  renderTrigger: (value: T) => ReactNode
  renderOption: (option: T, selected: boolean) => ReactNode
}

/** 为不同业务选择器提供一致的 ARIA listbox 键盘与焦点模型 */
export function ListboxPicker<T extends string>({
  value,
  options,
  open,
  onOpenChange,
  onChange,
  triggerLabel,
  listboxLabel,
  rootClassName,
  triggerClassName,
  listboxClassName,
  optionClassName,
  listboxPortalTarget,
  listboxStyle,
  disabled = false,
  renderTrigger,
  renderOption,
}: ListboxPickerProps<T>) {
  const generatedId = useId()
  const listboxId = `listbox-${generatedId}`
  const rootRef = useRef<HTMLDivElement>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const listboxRef = useRef<HTMLDivElement>(null)
  const selectedIndex = Math.max(0, options.indexOf(value))
  const [activeIndex, setActiveIndex] = useState(selectedIndex)

  const closeAndFocusTrigger = () => {
    onOpenChange(false)
    triggerRef.current?.focus({ preventScroll: true })
  }

  useEffect(() => {
    if (disabled && open) onOpenChange(false)
  }, [disabled, onOpenChange, open])

  useLayoutEffect(() => {
    if (!open) return
    setActiveIndex(selectedIndex)
    listboxRef.current?.focus({ preventScroll: true })
  }, [open, options.length, selectedIndex])

  useLayoutEffect(() => {
    if (!open) return
    const activeOption = document.getElementById(`${listboxId}-option-${activeIndex}`)
    const listbox = listboxRef.current
    if (activeOption && listbox?.contains(activeOption)) {
      // 只移动选项列表，避免打开下拉框时带动外层表单滚动
      const optionBounds = activeOption.getBoundingClientRect()
      const bounds = listbox.getBoundingClientRect()
      if (optionBounds.top < bounds.top) listbox.scrollTop += optionBounds.top - bounds.top
      else if (optionBounds.bottom > bounds.bottom) listbox.scrollTop += optionBounds.bottom - bounds.bottom
    }
  }, [activeIndex, listboxId, open])

  useEffect(() => {
    if (!open) return
    const handleOutsidePointer = (event: PointerEvent) => {
      if (
        event.target instanceof Node
        && !rootRef.current?.contains(event.target)
        && !listboxRef.current?.contains(event.target)
      ) {
        onOpenChange(false)
      }
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
  })

  const choose = (index: number) => {
    const option = options[index]
    if (!option) return
    onChange(option)
    closeAndFocusTrigger()
  }

  const handleListboxKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'ArrowDown') {
      event.preventDefault()
      setActiveIndex((current) => (current + 1) % options.length)
    } else if (event.key === 'ArrowUp') {
      event.preventDefault()
      setActiveIndex((current) => (current - 1 + options.length) % options.length)
    } else if (event.key === 'Home') {
      event.preventDefault()
      setActiveIndex(0)
    } else if (event.key === 'End') {
      event.preventDefault()
      setActiveIndex(options.length - 1)
    } else if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault()
      choose(activeIndex)
    } else if (event.key === 'Escape') {
      event.preventDefault()
      closeAndFocusTrigger()
    }
  }

  const listbox = open ? (
    <div
      ref={listboxRef}
      id={listboxId}
      className={listboxClassName}
      style={listboxStyle}
      role="listbox"
      aria-label={listboxLabel}
      aria-activedescendant={`${listboxId}-option-${activeIndex}`}
      tabIndex={-1}
      onKeyDown={handleListboxKeyDown}
      onWheel={event => event.stopPropagation()}
    >
      {options.map((option, index) => (
        <div
          key={option}
          id={`${listboxId}-option-${index}`}
          className={optionClassName}
          role="option"
          tabIndex={-1}
          aria-selected={value === option}
          data-active={activeIndex === index}
          onClick={() => choose(index)}
          onKeyDown={(event) => {
            if (event.key === 'Enter' || event.key === ' ') choose(index)
          }}
        >
          {renderOption(option, value === option)}
        </div>
      ))}
    </div>
  ) : null

  return (
    <div ref={rootRef} className={rootClassName}>
      <button
        ref={triggerRef}
        type="button"
        className={triggerClassName}
        aria-label={triggerLabel}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? listboxId : undefined}
        disabled={disabled}
        onClick={() => onOpenChange(!open)}
      >
        {renderTrigger(value)}
      </button>
      {listboxPortalTarget && listbox
        ? createPortal(listbox, listboxPortalTarget)
        : listbox}
    </div>
  )
}
