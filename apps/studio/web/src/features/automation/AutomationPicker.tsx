import { Check, ChevronDown } from 'lucide-react'
import { useState } from 'react'

import { ListboxPicker } from '../../components/ui'

/** 为自动化表单和筛选组合共享列表框，保留统一的键盘选择与焦点恢复 */
export function AutomationPicker<Value extends string>({ value, options, label, onChange, optionLabel }: {
  value: Value
  options: readonly Value[]
  label: string
  onChange: (value: Value) => void
  optionLabel: (value: Value) => string
}) {
  const [open, setOpen] = useState(false)
  return <ListboxPicker value={value} options={options} onChange={onChange} open={open} onOpenChange={setOpen}
    triggerLabel={label} listboxLabel={label} rootClassName="automation-picker" triggerClassName="automation-picker-trigger"
    listboxClassName="automation-picker-options" optionClassName="automation-picker-option"
    renderTrigger={(selected) => <><span>{optionLabel(selected)}</span><ChevronDown size={16} aria-hidden="true" /></>}
    renderOption={(option, selected) => <><span>{optionLabel(option)}</span>{selected && <Check size={16} aria-hidden="true" />}</>} />
}
