import { Check, ChevronDown } from 'lucide-react'

import { Button, ListboxPicker } from '../../../components/ui'
import { OverflowMarquee } from './OverflowMarquee'
import { useI18n } from '../../../i18n'
import type { ModelCatalogStatus } from '../useModelCatalog'

function ModelOption({ label, selected }: { label: string; selected: boolean }) {
  return (
    <>
      <OverflowMarquee className="ui-compact-option-label">{label}</OverflowMarquee>
      <span className="ui-compact-option-check" aria-hidden="true">{selected && <Check size={14} />}</span>
    </>
  )
}

export function ComposerModelPicker({
  model,
  modelIds,
  defaultModelId,
  modelDisplayName,
  status,
  open,
  onOpenChange,
  onSelectModel,
  onRetry,
}: {
  model: string
  modelIds: string[]
  defaultModelId: string
  modelDisplayName: (modelId: string) => string
  status: ModelCatalogStatus
  open: boolean
  onOpenChange: (open: boolean) => void
  onSelectModel: (model: string) => void
  onRetry: () => void
}) {
  const { t } = useI18n()
  if (status === 'error') {
    return (
      <div className="composer-model-error">
        <Button type="button" size="sm" variant="text" onClick={onRetry}>{t('重新加载模型')}</Button>
      </div>
    )
  }

  if (status === 'empty') {
    return (
      <div className="composer-model-error" role="status">
        <span>{t('未配置可用模型')}</span>
        <Button size="sm" variant="text" onClick={onRetry}>{t('重试')}</Button>
      </div>
    )
  }

  const selectedModel = model || defaultModelId
  return (
    <ListboxPicker
      value={selectedModel}
      options={modelIds}
      open={open}
      onOpenChange={onOpenChange}
      onChange={onSelectModel}
      disabled={status !== 'ready' || modelIds.length === 0}
      triggerLabel={t('选择模型')}
      listboxLabel={t('模型选项')}
      rootClassName="ui-compact-picker"
      triggerClassName="ui-compact-picker-trigger"
      listboxClassName="ui-compact-picker-options"
      optionClassName="overflow-marquee-trigger"
      renderTrigger={(selected) => (
        <>
          <span>{modelDisplayName(selected) || t('加载模型…')}</span>
          <ChevronDown className="ui-compact-picker-chevron" size={14} />
        </>
      )}
      renderOption={(option, selected) => (
        <ModelOption label={modelDisplayName(option)} selected={selected} />
      )}
    />
  )
}
