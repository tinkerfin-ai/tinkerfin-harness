import { useState } from 'react'
import { ChevronDown, Trash2 } from 'lucide-react'
import { Button, TextField, ValidatedForm } from '../../components/ui'
import { useI18n } from '../../i18n'
import { ImageInputCapabilityField } from './ImageInputCapability'
import { ModelChoice } from './ModelChoice'
import { ModelSettingsLayout } from './ModelSettingsLayout'
import type { ChatOptions, ModelConnection, ModelSettings } from './useModelSettings'

export function ModelConfigurationForm({ model, connection, saving, existing, onSave, onRemove, onCancel }: {
  model: ModelSettings; connection: ModelConnection; saving: boolean; existing: boolean
  onSave: (model: ModelSettings) => Promise<boolean>; onRemove: (id: string) => Promise<boolean>; onCancel: () => void
}) {
  const { t } = useI18n()
  const [draft, setDraft] = useState(model)
  const [requestedName, setRequestedName] = useState(model.model_name.trim())
  const [attempt, setAttempt] = useState(0)
  const [deleting, setDeleting] = useState(false)
  const errors: Partial<Record<'display_name' | 'model_name' | keyof ChatOptions, string>> = {}
  if (!draft.display_name.trim()) errors.display_name = t('请输入显示名称')
  else if ([...draft.display_name.trim()].length > 40) errors.display_name = t('显示名称最多 40 个字符')
  if (!draft.model_name.trim()) errors.model_name = t('请填写服务商提供的 Model ID')
    const limits = { max_tokens: [1, Infinity, true], temperature: [0, 2, false], top_p: [0, 1, false], context_window: [1, Infinity, true], keep_alive: [0, 86400, true] } as const
    for (const key of Object.keys(limits) as Array<keyof typeof limits>) {
      const value = draft.chat_options[key]
      const [minimum, maximum, integer] = limits[key]
      if (value !== null && (!Number.isFinite(value) || value < minimum || value > maximum || (integer && !Number.isInteger(value)))) errors[key] = t('参数超出允许范围')
    }
    if ((draft.chat_options.stop?.length ?? 0) > 4) errors.stop = t('停止序列最多四项')
  const update = (patch: Partial<ModelSettings>) => { setDraft(value => ({ ...value, ...patch })) }
  const changeOption = <K extends keyof ChatOptions>(key: K, value: ChatOptions[K]) => update({ chat_options: { ...draft.chat_options, [key]: value } })
  const write = (): ModelSettings => ({ ...draft, display_name: draft.display_name.trim(), model_name: draft.model_name.trim() })
  const validate = () => { setAttempt(value => value + 1); return Object.keys(errors).length === 0 }
  return <ValidatedForm className="settings-models__editor-frame" errors={attempt ? errors : {}} validationAttempt={attempt} onSubmit={event => { event.preventDefault(); if (deleting) return; if (validate()) { void onSave(write()).then(saved => { if (saved) onCancel() }) } }}>
    <ModelSettingsLayout actions={<>
      {deleting && <p className="settings-models__delete-message" role="alert">{t('删除此模型配置，历史记录保留')}</p>}
      <div className="settings-models__row-actions">
        {existing && <Button size="sm" type="button" variant="text" className="settings-models__delete-action" leadingIcon={<Trash2 size={14} />} disabled={saving} onClick={() => {
          if (!deleting) setDeleting(true)
          else void onRemove(draft.model_id).then(removed => { if (removed) onCancel() })
        }}>{t('删除模型')}</Button>}
        <Button size="sm" type="button" disabled={saving} onClick={deleting ? () => setDeleting(false) : onCancel}>{t('取消')}</Button>
        {!deleting && <Button size="sm" type="submit" variant="primary" loading={saving}>{t('保存')}</Button>}
      </div>
    </>}>
    <fieldset className="settings-models__form" disabled={saving}>
      <div className="settings-models__grid">
        <TextField shape="standard" fieldSize="md" label={t('显示名称')} name="display_name" value={draft.display_name} maxLength={40} error={attempt ? errors.display_name : undefined} onChange={event => update({ display_name: event.target.value })} />
        <TextField shape="standard" fieldSize="md" label="Model ID" name="model_name" onBlur={() => setRequestedName(draft.model_name.trim())} value={draft.model_name} maxLength={128} error={attempt ? errors.model_name : undefined} onChange={event => update({ model_name: event.target.value })} />
      </div>
        <ImageInputCapabilityField disabled={saving} connection={connection} modelName={draft.model_name} requestedName={requestedName} declaration={draft.image_support} onChange={image_support => update({ image_support })} />
        {(connection.api_type === 'ollama' || connection.provider_id === 'deepseek') && <label className="settings-models__check"><input type="checkbox" checked={draft.reasoning_enabled} onChange={event => update({ reasoning_enabled: event.target.checked })} />{t('启用推理')}</label>}
        <details className="settings-models__advanced"><summary>{t('生成参数')}<ChevronDown size={15} aria-hidden="true" /></summary><div className="settings-models__advanced-body settings-models__grid">
          {([{ key: 'max_tokens', label: '最大输出 Token', min: 1, step: 1 }, { key: 'temperature', label: '温度', min: 0, max: 2, step: .1 }, { key: 'top_p', label: 'Top P', min: 0, max: 1, step: .1 }] as const).map(item => <TextField key={item.key} name={item.key} error={attempt ? errors[item.key] : undefined} shape="standard" fieldSize="md" label={t(item.label)} type="number" min={item.min} max={'max' in item ? item.max : undefined} step={item.step} placeholder={t('使用模型默认值')} value={draft.chat_options[item.key] ?? ''} onChange={event => changeOption(item.key, event.target.value === '' ? null : Number(event.target.value))} />)}
          <ModelChoice disabled={saving} label={t('推理强度')} value={draft.chat_options.reasoning_effort ?? ''} options={['', 'low', 'medium', 'high']} text={value => value === '' ? t('使用模型默认值') : t(value === 'low' ? '低' : value === 'medium' ? '中' : '高')} onChange={value => changeOption('reasoning_effort', value || null)} />
          {connection.api_type === 'ollama' && <><TextField shape="standard" fieldSize="md" name="context_window" error={attempt ? errors.context_window : undefined} label={t('上下文 Token')} type="number" min={1} step={1} value={draft.chat_options.context_window ?? ''} placeholder={t('使用模型默认值')} onChange={event => changeOption('context_window', event.target.value ? Number(event.target.value) : null)} /><TextField shape="standard" fieldSize="md" name="keep_alive" error={attempt ? errors.keep_alive : undefined} label={t('保持加载时间（秒）')} type="number" min={0} max={86400} step={1} value={draft.chat_options.keep_alive ?? ''} placeholder={t('使用模型默认值')} onChange={event => changeOption('keep_alive', event.target.value ? Number(event.target.value) : null)} /></>}
          <TextField rootClassName="settings-models__wide" shape="standard" fieldSize="md" name="stop" error={attempt ? errors.stop : undefined} label={t('停止序列')} helperText={t('用逗号分隔，最多四项')} value={draft.chat_options.stop?.join(',') ?? ''} onChange={event => changeOption('stop', event.target.value ? event.target.value.split(',') : null)} />
        </div></details>
      <label className="settings-models__check"><input type="checkbox" checked={draft.enabled} disabled={draft.is_default} onChange={event => update({ enabled: event.target.checked })} />{t('启用模型')}</label>
    </fieldset>
    </ModelSettingsLayout>
  </ValidatedForm>
}
