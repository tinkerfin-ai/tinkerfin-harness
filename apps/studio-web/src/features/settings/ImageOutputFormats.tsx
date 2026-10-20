import { useState } from 'react'
import { Check, ChevronDown } from 'lucide-react'
import { ListboxPicker } from '../../components/ui'
import { useI18n } from '../../i18n'
import { IMAGE_OUTPUT_FORMATS, type ImageOutputFormat } from './modelOptions'

/** 一次生成结果可导出为多种格式；清空选择时保留服务返回的原格式 */
export function ImageOutputFormats({ value, onChange, disabled }: {
  value: readonly ImageOutputFormat[]; onChange: (value: ImageOutputFormat[]) => void; disabled: boolean
}) {
  const { t } = useI18n()
  const [open, setOpen] = useState(false)
  return <div className="settings-models__choice"><span>{t('输出格式')}</span>
    <ListboxPicker multiple value={value} options={IMAGE_OUTPUT_FORMATS} onChange={onChange} open={open} onOpenChange={setOpen} disabled={disabled}
      triggerLabel={t('输出格式')} listboxLabel={t('输出格式')} rootClassName="settings-choice-picker" triggerClassName="settings-choice-trigger" listboxClassName="settings-choice-options" optionClassName="settings-choice-option"
      renderTrigger={formats => <>{formats.length ? formats.map(item => item.toUpperCase()).join(' / ') : t('使用模型默认值')}<ChevronDown size={14} aria-hidden="true" /></>}
      renderOption={(format, selected) => <>{format.toUpperCase()}<span className="settings-choice-check">{selected && <Check size={14} aria-hidden="true" />}</span></>} />
  </div>
}
