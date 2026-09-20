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

function revealOption(listbox: HTMLElement, option: HTMLElement | null) {
  if (!option) return
  const optionBounds = option.getBoundingClientRect()
  const bounds = listbox.getBoundingClientRect()
  if (optionBounds.top < bounds.top) listbox.scrollTop += optionBounds.top - bounds.top
  else if (optionBounds.bottom > bounds.bottom) listbox.scrollTop += optionBounds.bottom - bounds.bottom
}

export interface ListboxPickerProps<T extends string> {
  value: T
  options: readonly T[]
  open: boolean
  onOpenChange: (open: boolean) => void
  onChange: (value: T) => void
  triggerLabel: string
  triggerTooltip?: string
  listboxLabel: string
  rootClassName: string
  triggerClassName: string
  listboxClassName: string
  optionClassName?: string
  /** 按稳定标识合并分组，组及组内选项保留首次出现顺序，标题不参与选择 */
  getOptionGroup?: (option: T) => { id: string; label: string }
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
  triggerTooltip,
  listboxLabel,
  rootClassName,
  triggerClassName,
  listboxClassName,
  optionClassName,
  getOptionGroup,
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
  const groups = new Map<string, { label: string; options: T[] }>()
  if (getOptionGroup) {
    for (const option of options) {
      const { id, label } = getOptionGroup(option)
      const group = groups.get(id)
      if (group) group.options.push(option)
      else groups.set(id, { label, options: [option] })
    }
  }
  const orderedOptions = getOptionGroup
    ? Array.from(groups.values()).flatMap(group => group.options)
    : options
  const selectedIndex = Math.max(0, orderedOptions.indexOf(value))
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
    const listbox = listboxRef.current
    if (!open || !listbox) return
    const authoredMaxHeight = listbox.style.maxHeight
    const authoredOverflow = listbox.style.overflowY
    const ancestors: HTMLElement[] = []
    for (let parent = listbox.parentElement; parent; parent = parent.parentElement) ancestors.push(parent)
    const fitMenu = () => {
      const scrollTop = listbox.scrollTop
      listbox.style.maxHeight = authoredMaxHeight
      let top = 0
      let bottom = window.innerHeight
      for (const ancestor of ancestors) {
        if (!/(auto|scroll|hidden|clip)/.test(getComputedStyle(ancestor).overflowY)) continue
        const bounds = ancestor.getBoundingClientRect()
        top = Math.max(top, bounds.top + ancestor.clientTop)
        bottom = Math.min(bottom, bounds.top + ancestor.clientTop + ancestor.clientHeight)
      }
      const style = getComputedStyle(listbox)
      const bounds = listbox.getBoundingClientRect()
      const available = style.bottom !== 'auto' && style.bottom !== '' ? bounds.bottom - top : bottom - bounds.top
      const limit = parseFloat(style.maxHeight)
      // 菜单在弹窗滚动区域内收缩，避免提供方标题和首项被父容器裁掉
      listbox.style.maxHeight = `${Math.max(0, Math.min(available, Number.isFinite(limit) ? limit : Infinity))}px`
      listbox.style.overflowY = 'auto'
      listbox.scrollTop = scrollTop
      revealOption(listbox, listbox.querySelector<HTMLElement>('[data-active="true"]'))
    }
    const onScroll = (event: Event) => { if (event.target !== listbox) fitMenu() }
    const observer = new ResizeObserver(fitMenu)
    ancestors.forEach(ancestor => observer.observe(ancestor))
    window.addEventListener('resize', fitMenu)
    window.addEventListener('scroll', onScroll, true)
    fitMenu()
    return () => {
      observer.disconnect()
      window.removeEventListener('resize', fitMenu)
      window.removeEventListener('scroll', onScroll, true)
      listbox.style.maxHeight = authoredMaxHeight
      listbox.style.overflowY = authoredOverflow
    }
  }, [open, listboxPortalTarget])

  useLayoutEffect(() => {
    if (!open) return
    const activeOption = document.getElementById(`${listboxId}-option-${activeIndex}`)
    const listbox = listboxRef.current
    if (activeOption && listbox?.contains(activeOption)) {
      // 只移动选项列表，避免打开下拉框时带动外层表单滚动
      revealOption(listbox, activeOption)
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
    const option = orderedOptions[index]
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

  const optionElement = (option: T, index: number) => (
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
  )
  let optionIndex = 0
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
      {getOptionGroup
        ? Array.from(groups, ([id, group], index) => (
          <div key={id} role="group" className="ui-listbox-group" aria-labelledby={`${listboxId}-group-${index}`}>
            <div id={`${listboxId}-group-${index}`} className="ui-listbox-group-label" title={group.label}>{group.label}</div>
            {group.options.map(option => optionElement(option, optionIndex++))}
          </div>
        ))
        : orderedOptions.map(optionElement)}
    </div>
  ) : null

  return (
    <div ref={rootRef} className={rootClassName}>
      <button
        ref={triggerRef}
        type="button"
        className={triggerClassName}
        aria-label={triggerLabel}
        aria-describedby={triggerTooltip ? `${listboxId}-tooltip` : undefined}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? listboxId : undefined}
        disabled={disabled}
        onClick={() => onOpenChange(!open)}
      >
        {renderTrigger(value)}
      </button>
      {triggerTooltip && <span id={`${listboxId}-tooltip`} className="ui-tooltip" role="tooltip">{triggerTooltip}</span>}
      {listboxPortalTarget && listbox
        ? createPortal(listbox, listboxPortalTarget)
        : listbox}
    </div>
  )
}
