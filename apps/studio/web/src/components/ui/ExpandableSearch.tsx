import { Search } from 'lucide-react'
import { useId, useLayoutEffect, useRef } from 'react'

import { IconButton } from './IconButton'
import { SearchField } from './SearchField'
import type { SearchFieldProps } from './SearchField'

export interface ExpandableSearchProps extends Pick<SearchFieldProps, 'label' | 'closeLabel' | 'placeholder' | 'value' | 'onChange'> {
  open: boolean
  onOpenChange: (open: boolean) => void
}

/** 顶栏搜索展开后聚焦输入，关闭时清空查询并把焦点交回入口 */
export function ExpandableSearch({ open, onOpenChange, value, onChange, label, closeLabel, placeholder }: ExpandableSearchProps) {
  const id = useId()
  const input = useRef<HTMLInputElement>(null)
  const trigger = useRef<HTMLButtonElement>(null)
  const wasOpen = useRef(false)
  useLayoutEffect(() => {
    if (open) input.current?.focus()
    else if (wasOpen.current) trigger.current?.focus()
    wasOpen.current = open
  }, [open])
  const close = () => { onChange(''); onOpenChange(false) }
  return <div className={`ui-expandable-search${open ? ' is-open' : ''}`}>
    <IconButton ref={trigger} label={label} icon={<Search size={17} />}
      aria-hidden={open || undefined} tabIndex={open ? -1 : undefined} aria-expanded={open} aria-controls={open ? id : undefined}
      onClick={() => onOpenChange(true)} />
    {open && <SearchField ref={input} id={id} appearance="plain" showIcon={false} label={label} closeLabel={closeLabel}
      placeholder={placeholder} value={value} onChange={onChange} onClose={close} />}
  </div>
}
