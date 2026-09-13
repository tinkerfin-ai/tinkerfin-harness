import { Check, Filter } from 'lucide-react'
import { useState } from 'react'

import { ListboxPicker } from '../../components/ui'
import type { RunStatus } from './model'
import { useI18n } from '../../i18n'

export type AutomationFilter = 'all' | RunStatus | 'enabled' | 'paused'

export function AutomationStatusFilter({ value, options, onChange }: {
  value: AutomationFilter
  options: readonly { value: AutomationFilter; label: string; count: number }[]
  onChange: (value: AutomationFilter) => void
}) {
  const { t } = useI18n()
  const [open, setOpen] = useState(false)
  return <ListboxPicker value={value} options={options.map((option) => option.value)} open={open}
    onOpenChange={setOpen} onChange={onChange} triggerLabel={t('筛选状态')} listboxLabel={t('筛选状态')}
    rootClassName="automation-status-filter" triggerClassName={`automation-filter-trigger${value !== 'all' ? ' is-selected' : ''}`}
    listboxClassName="automation-filter-options ui-scrollbar" optionClassName="automation-filter-option"
    renderTrigger={() => <Filter size={18} aria-hidden="true" />}
    renderOption={(key, selected) => {
      const option = options.find((item) => item.value === key)!
      return <><span>{option.label}</span><span className="automation-filter-count">{option.count}</span>
        <span className="automation-filter-check">{selected && <Check size={16} aria-hidden="true" />}</span></>
    }} />
}
