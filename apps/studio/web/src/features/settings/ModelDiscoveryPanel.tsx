import { useEffect, useState } from 'react'
import { RefreshCw, Search } from 'lucide-react'
import { Button, TextField } from '../../components/ui'
import { requestJson } from '../../api/shared/http'
import { useI18n } from '../../i18n'
import { ModelSettingsLayout } from './ModelSettingsLayout'
import { modelTestMessages } from './modelTestMessages'
import { newModel, type ModelConnection, type ModelSettings } from './useModelSettings'
interface DiscoveredModel { model_name: string; display_name: string; image_support: ModelSettings['image_support'] }
interface DiscoveryResult { outcome: 'success' | 'inconclusive' | 'failed'; code: string; items: DiscoveredModel[] }

export function ModelDiscoveryPanel({ connection, models, saving, onAdd, onManual, onCancel }: {
  connection: ModelConnection; models: ModelSettings[]; saving: boolean
  onAdd: (models: ModelSettings[]) => Promise<boolean>; onManual: () => void; onCancel: () => void
}) {
  const { t } = useI18n()
  const [result, setResult] = useState<DiscoveryResult>()
  const [loading, setLoading] = useState(true)
  const [revision, setRevision] = useState(0)
  const [selected, setSelected] = useState<string[]>([])
  const [search, setSearch] = useState('')
  useEffect(() => {
    const controller = new AbortController()
    setLoading(true); setResult(undefined)
    void requestJson<DiscoveryResult>(`/api/models/connections/${encodeURIComponent(connection.connection_id)}/models`, { method: 'POST', signal: controller.signal })
      .then(value => { if (!controller.signal.aborted) setResult(value) })
      .catch(() => { if (!controller.signal.aborted) setResult({ outcome: 'failed', code: 'network_error', items: [] }) })
      .finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [connection.connection_id, revision])
  const existing = new Set(models.map(model => model.model_name))
  const candidates = (result?.items ?? []).filter(item => selected.includes(item.model_name) && !existing.has(item.model_name))
  const query = search.trim().toLowerCase()
  const matches = (result?.items ?? []).filter(item => item.model_name.toLowerCase().includes(query) || item.display_name.toLowerCase().includes(query))
  return <div className="settings-models__editor-frame"><ModelSettingsLayout actions={<div className="settings-models__row-actions">
    <span className="settings-models__muted" role="status">{t('已选 {count} 项', { count: candidates.length })}</span>
    <Button size="sm" type="button" variant="text" disabled={saving} onClick={onManual}>{t('手动添加')}</Button>
    <Button size="sm" type="button" disabled={saving} onClick={onCancel}>{t('取消')}</Button><Button size="sm" type="button" variant="primary" loading={saving} disabled={candidates.length === 0 || candidates.length > 200} onClick={() => {
      void onAdd(candidates.map(item => ({ ...newModel(connection.connection_id), model_name: item.model_name, display_name: item.display_name, image_support: item.image_support }))).then(saved => { if (saved) onCancel() })
    }}>{t('添加所选模型')}</Button></div>}>
    {loading ? <p role="status">{t('正在获取模型')}</p> : result?.outcome === 'success' ? <>
      {result.items.length > 0 && <TextField shape="standard" fieldSize="md" label={t('搜索模型名称或 Model ID')} value={search} disabled={saving} leadingContent={<Search size={14} />} onChange={event => setSearch(event.target.value)} />}
      <div className="settings-models__discovered">
      {result.items.length === 0 && <p>{t('服务没有返回模型，可手动添加')}</p>}
      {result.items.length > 0 && matches.length === 0 && <div className="settings-models__empty" role="status"><p>{t('没有匹配的模型')}</p><Button type="button" disabled={saving} onClick={() => setSearch('')}>{t('清除搜索')}</Button></div>}
      {matches.map(item => <label key={item.model_name}><input type="checkbox" disabled={saving || existing.has(item.model_name)} checked={existing.has(item.model_name) || selected.includes(item.model_name)} onChange={event => setSelected(values => event.target.checked ? [...values, item.model_name] : values.filter(name => name !== item.model_name))} /><span>{item.model_name}</span>{existing.has(item.model_name) && <small>{t('已添加')}</small>}</label>)}
    </div></> : <div role="status"><p>{t(modelTestMessages[result?.code ?? ''] ?? '无法获取模型列表，请重试或手动添加')}</p><Button type="button" leadingIcon={<RefreshCw size={14} />} onClick={() => setRevision(value => value + 1)}>{t('重试')}</Button></div>}

  </ModelSettingsLayout></div>
}
