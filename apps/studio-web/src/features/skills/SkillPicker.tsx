import { Check, ChevronDown } from 'lucide-react'
import { useState } from 'react'
import { ListboxPicker } from '../../components/ui'

/** 与对话页模型选择器共用紧凑控件、阴影和键盘行为 */
export function SkillPicker<T extends string>({ value, options, label, onChange }: {
  value: T; options: readonly { value: T; label: string }[]; label: string; onChange: (value: T) => void
}) {
  const [open, setOpen] = useState(false)
  return <ListboxPicker value={value} options={options.map(option => option.value)} open={open} onOpenChange={setOpen}
    onChange={onChange} triggerLabel={label} listboxLabel={label} rootClassName="ui-compact-picker skills-picker"
    triggerClassName="ui-compact-picker-trigger" listboxClassName="ui-compact-picker-options skills-picker-options"
    renderTrigger={selected => <><span>{options.find(option => option.value === selected)?.label}</span><ChevronDown className="ui-compact-picker-chevron" size={14} aria-hidden="true" /></>}
    renderOption={(option, selected) => <><span className="ui-compact-option-label">{options.find(item => item.value === option)?.label}</span><span className="ui-compact-option-check" aria-hidden="true">{selected && <Check size={14} />}</span></>} />
}
