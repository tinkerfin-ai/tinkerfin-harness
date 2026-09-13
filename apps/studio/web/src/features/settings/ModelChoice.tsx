import { useState } from 'react'
import { Check, ChevronDown } from 'lucide-react'
import { ListboxPicker } from '../../components/ui'

/** 复用设置页选择器的键盘操作和焦点恢复 */
export function ModelChoice<T extends string>({ label, value, options, onChange, text, disabled = false }: {
  label: string; value: T; options: readonly T[]; onChange: (value: T) => void; text: (value: T) => string; disabled?: boolean
}) {
  const [open, setOpen] = useState(false)
  return <div className="settings-models__choice"><span>{label}</span>
    <ListboxPicker value={value} options={options} onChange={onChange} open={open} onOpenChange={setOpen} disabled={disabled}
      triggerLabel={label} listboxLabel={label} rootClassName="settings-choice-picker" triggerClassName="settings-choice-trigger"
      listboxClassName="settings-choice-options" optionClassName="settings-choice-option"
      renderTrigger={option => <>{text(option)}<ChevronDown size={14} aria-hidden="true" /></>}
      renderOption={(option, selected) => <>{text(option)}<span className="settings-choice-check">{selected && <Check size={14} aria-hidden="true" />}</span></>} />
  </div>
}
