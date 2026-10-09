import { Check, Filter } from 'lucide-react'
import { useState } from 'react'

import { ListboxPicker } from '../../components/ui'
import type { RunStatus } from './model'
import { useI18n } from '../../i18n'

export type AutomationFilter = 'all' | RunStatus | 'enabled' | 'paused'

export function AutomationStatusFilter({ value, options, onChange }: {
  value: AutomationFilter
  options: readonly { value: AutomationFilter; label: string }[]
  onChange: (value: AutomationFilter) => void
}) {
  const { t } = useI18n()
  const [open, setOpen] = useState(false)
  return <ListboxPicker value={value} options={options.map((option) => option.value)} open={open}
    onOpenChange={setOpen} onChange={onChange} triggerLabel={t('筛选状态')} listboxLabel={t('筛选状态')}
    rootClassName="ui-compact-picker automation-status-filter" triggerClassName={`automation-filter-trigger${value !== 'all' ? ' is-selected' : ''}`}
    listboxClassName="ui-compact-picker-options ui-compact-picker-options--down automation-filter-options ui-scrollbar"
    renderTrigger={() => <Filter size={18} aria-hidden="true" />}
    renderOption={(key, selected) => {
      const option = options.find((item) => item.value === key)!
      return <><span className="ui-compact-option-label">{option.label}</span>
        <span className="ui-compact-option-check" aria-hidden="true">{selected && <Check size={14} />}</span></>
    }} />
}
