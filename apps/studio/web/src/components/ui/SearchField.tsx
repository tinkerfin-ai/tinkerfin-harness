import { Search, X } from 'lucide-react'
import { forwardRef } from 'react'

import { IconButton } from './IconButton'

export interface SearchFieldProps {
  id?: string
  className?: string
  appearance?: 'plain' | 'soft'
  label: string
  closeLabel: string
  placeholder: string
  value: string
  onChange: (value: string) => void
  onClose: () => void
}

/** 紧凑搜索输入框，清除按钮与 Escape 统一交给调用方关闭并恢复焦点 */
export const SearchField = forwardRef<HTMLInputElement, SearchFieldProps>(function SearchField({
  id, className, appearance = 'plain', label, closeLabel, placeholder, value, onChange, onClose,
}, ref) {
  return <label id={id} className={`ui-search-field ui-search-field--${appearance}${className ? ` ${className}` : ''}`}>
    <Search size={14} aria-hidden="true" />
    <input ref={ref} type="text" role="searchbox" aria-label={label} value={value} placeholder={placeholder}
      onChange={(event) => onChange(event.target.value)} onKeyDown={(event) => {
        if (event.key !== 'Escape') return
        event.preventDefault()
        onClose()
      }} />
    <IconButton type="button" size="xs" label={closeLabel} icon={<X size={13} />} onClick={onClose} />
  </label>
})
