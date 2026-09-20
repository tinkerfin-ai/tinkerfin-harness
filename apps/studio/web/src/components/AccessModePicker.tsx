import { Check, ChevronDown, FilePenLine, KeyRound } from 'lucide-react'
import { useState } from 'react'

import type { AccessMode } from '../types'
import { useI18n } from '../i18n'
import { ListboxPicker } from './ui'

const options: readonly AccessMode[] = ['write_approval', 'full']
const labels = { write_approval: '写入需审批', full: '完全访问' } as const

/** 会话与自动化共用文件审批选择，视觉和键盘操作与模型选择一致 */
export function AccessModePicker({ value, onChange, disabled = false }: {
  value: AccessMode
  onChange: (value: AccessMode) => void
  disabled?: boolean
}) {
  const [open, setOpen] = useState(false)
  const { t } = useI18n()
  const icon = (option: AccessMode) => option === 'full' ? <KeyRound size={14} /> : <FilePenLine size={14} />
  return <ListboxPicker value={value} options={options} open={open} onOpenChange={setOpen}
    onChange={onChange} disabled={disabled} triggerLabel={t('选择访问权限')} triggerTooltip={t(labels[value])} listboxLabel={t('访问权限选项')}
    rootClassName="ui-compact-picker ui-compact-picker--access-mode" triggerClassName="ui-compact-picker-trigger" listboxClassName="ui-compact-picker-options"
    renderTrigger={selected => <>{icon(selected)}<span>{t(labels[selected])}</span><ChevronDown className="ui-compact-picker-chevron" size={14} /></>}
    renderOption={(option, selected) => <>{icon(option)}<span className="ui-compact-option-label">{t(labels[option])}</span><span className="ui-compact-option-check">{selected && <Check size={14} />}</span></>} />
}
