import { useState } from 'react'
import { KeyRound, Trash2 } from 'lucide-react'
import { Button, TextField, ValidatedForm } from '../../components/ui'
import { useI18n } from '../../i18n'
import { ModelChoice } from './ModelChoice'
import { ModelSettingsLayout } from './ModelSettingsLayout'
import type { ConnectionWrite, ModelConnection, ProviderPreset } from './useModelSettings'

export function ModelConnectionForm({ connection, presets, saving, modelCount, onSave, onRemove, onCancel }: {
  connection?: ModelConnection; presets: ProviderPreset[]; saving: boolean; modelCount: number
  onSave: (value: ConnectionWrite) => Promise<boolean>; onRemove: (id: string) => Promise<boolean>; onCancel: () => void
}) {
  const { t } = useI18n()
  const [draft, setDraft] = useState<ModelConnection>(() => connection ?? {
    connection_id: crypto.randomUUID(), display_name: '', provider_id: 'custom', api_type: 'openai_chat_completions', base_url: '', auth_type: 'api_key', has_key: false,
  })
  const [key, setKey] = useState('')
  const [attempt, setAttempt] = useState(0)
  const [deleting, setDeleting] = useState(false)
  const errors: Partial<Record<'display_name' | 'base_url' | 'api_key', string>> = {}
  if (!draft.display_name.trim()) errors.display_name = t('请输入显示名称')
  try { const url = new URL(draft.base_url); if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) throw Error() } catch { errors.base_url = t('请输入有效的 HTTP 或 HTTPS 接口地址') }
  const canReuseKey = connection?.has_key && connection.base_url.replace(/\/+$/, '') === draft.base_url.replace(/\/+$/, '') && connection.api_type === draft.api_type
  if (draft.auth_type === 'api_key' && !key.trim() && !canReuseKey) errors.api_key = t('请输入 API Key')
  const selectPreset = (id: string) => {
    const preset = presets.find(item => item.provider_id === id)
    if (preset) { setDraft({ ...draft, provider_id: preset.provider_id, api_type: preset.api_type, base_url: preset.base_url, auth_type: preset.auth_type, display_name: id === 'custom' ? '' : preset.display_name }); setKey('') }
  }
  return <ValidatedForm className="settings-models__editor-frame" errors={attempt ? errors : {}} validationAttempt={attempt} onSubmit={event => {
    event.preventDefault(); if (deleting) return; setAttempt(value => value + 1)
    if (Object.keys(errors).length) return
    const { has_key: _, ...value } = draft; void _
    void onSave({ ...value, display_name: value.display_name.trim(), api_key: value.auth_type === 'none' ? '' : key.trim() || null }).then(saved => { if (saved) onCancel() })
  }}>
    <ModelSettingsLayout actions={<>
      {deleting && <p className="settings-models__delete-message" role="alert">{t('删除提供方及其 {count} 个模型配置，历史记录保留，此操作不可撤销', { count: modelCount })}</p>}
      <div className="settings-models__row-actions">
        {connection && <Button size="sm" type="button" variant="text" className="settings-models__delete-action" leadingIcon={<Trash2 size={14} />} disabled={saving} onClick={() => {
          if (!deleting) setDeleting(true)
          else void onRemove(connection.connection_id).then(removed => { if (removed) onCancel() })
        }}>{t('删除提供方')}</Button>}
        <Button size="sm" type="button" disabled={saving} onClick={deleting ? () => setDeleting(false) : onCancel}>{t('取消')}</Button>
        {!deleting && <Button size="sm" type="submit" variant="primary" loading={saving}>{t('保存')}</Button>}
      </div>
    </>}>
    <fieldset className="settings-models__form" disabled={saving}>
      {!connection && <ModelChoice label={t('提供方')} value={draft.provider_id} options={['custom', ...presets.filter(p => p.provider_id !== 'custom').map(p => p.provider_id)]} onChange={selectPreset} text={id => presets.find(p => p.provider_id === id)?.display_name ?? id} />}
      <TextField shape="standard" fieldSize="md" label={t('显示名称')} name="display_name" value={draft.display_name} error={attempt ? errors.display_name : undefined} maxLength={128} onChange={event => setDraft({ ...draft, display_name: event.target.value })} />
      <ModelChoice label={t('API 类型')} value={draft.api_type} options={['openai_chat_completions', 'ollama']} onChange={api_type => setDraft({ ...draft, api_type })} text={value => value === 'ollama' ? t('Ollama 原生 API') : 'OpenAI Chat Completions'} />
      <TextField shape="standard" fieldSize="md" label={t('服务地址')} name="base_url" type="url" value={draft.base_url} error={attempt ? errors.base_url : undefined} maxLength={1024} helperText={draft.api_type === 'ollama' ? t('填写 Studio 服务端可以访问的 Ollama 地址') : undefined} onChange={event => setDraft({ ...draft, base_url: event.target.value })} />
      <div className="settings-models__auth"><span>{t('认证方式')}</span><div role="radiogroup" aria-label={t('认证方式')}>
        {(draft.provider_id === 'deepseek' ? ['api_key'] as const : ['api_key', 'none'] as const).map(value => <label key={value}><input type="radio" name={`auth-${draft.connection_id}`} checked={draft.auth_type === value} onChange={() => setDraft({ ...draft, auth_type: value })} />{t(value === 'api_key' ? 'API 密钥' : '无需认证')}</label>)}
      </div></div>
      {draft.auth_type === 'api_key' && <TextField shape="standard" fieldSize="md" label="API Key" name="api_key" type="password" autoComplete="new-password" value={key} error={attempt ? errors.api_key : undefined} leadingContent={<KeyRound size={15} />} placeholder={t(canReuseKey ? '留空保留已保存的密钥' : '请输入 API Key')} onChange={event => setKey(event.target.value)} />}
    </fieldset>
    </ModelSettingsLayout>
  </ValidatedForm>
}
