import { useRef, useState } from 'react'
import { ChevronRight, Cpu, KeyRound, Link2, MessageSquare, Monitor, Plus, RefreshCw, Search, Settings2, SlidersHorizontal, Star } from 'lucide-react'
import { Button, OverlayScrollbar, TextField } from '../../components/ui'
import type { ToastKind } from '../../components/ui/ToastViewport'
import { useI18n } from '../../i18n'
import { ModelConnectionForm } from './ModelConnectionForm'
import { ModelConfigurationForm } from './ModelConfigurationForm'
import { ModelDiscoveryPanel } from './ModelDiscoveryPanel'
import { ModelProviderSplit } from './ModelProviderSplit'
import { newModel, useModelSettings, type ModelSettings } from './useModelSettings'
import './model-settings.css'
import './model-connections.css'

export function ModelSettingsPanel({ onChanged, onToast }: { onChanged?: () => void; onToast: (kind: ToastKind, message: string) => void }) {
  const { t } = useI18n()
  const state = useModelSettings(onChanged)
  const [selected, setSelected] = useState<string>()
  const [search, setSearch] = useState('')
  const [view, setView] = useState<'list' | 'new' | 'connection' | 'model' | 'discovery'>('list')
  const [editing, setEditing] = useState<ModelSettings>()
  const modelViewport = useRef<HTMLDivElement>(null)
  const query = search.trim().toLowerCase()
  const providers = state.connections.map(connection => {
    const models = state.models.filter(model => model.connection_id === connection.connection_id)
    const providerMatches = connection.display_name.toLowerCase().includes(query)
    const matches = providerMatches ? models : models.filter(model => model.display_name.toLowerCase().includes(query) || model.model_name.toLowerCase().includes(query))
    return { connection, models, matches, visible: providerMatches || matches.length > 0 }
  }).filter(provider => provider.visible)
  const activeProvider = providers.find(provider => provider.connection.connection_id === selected) ?? providers[0]
  const connection = activeProvider?.connection
  const models = activeProvider?.models ?? []
  const matchingModels = activeProvider?.matches ?? []
  const close = () => { setView('list'); setEditing(undefined) }
  const editModel = (model: ModelSettings) => { setEditing(model); setView('model') }
  const defaultModel = state.models.find(model => model.purpose === 'chat' && model.is_default)
  return <section className="settings-section settings-models settings-model-connections">
    <div className="settings-models__toolbar">
      <nav className="settings-models__breadcrumbs" aria-label={t('模型配置导航')}>
        <ol>
          <li>{view === 'list' ? <span aria-current="page">{t('模型配置')}</span> : <Button type="button" size="sm" variant="text" disabled={state.saving} onClick={close}>{t('模型配置')}</Button>}</li>
          {view !== 'list' && <>
            <li aria-hidden="true"><ChevronRight size={12} /></li>
            {view === 'new' ? <li><span aria-current="page">{t('添加提供方')}</span></li>
              : view === 'connection' ? <li><span aria-current="page" title={connection?.display_name}>{connection?.display_name}</span></li>
                : <>
                  <li><Button type="button" size="sm" variant="text" title={connection?.display_name} disabled={state.saving} onClick={close}>{connection?.display_name}</Button></li>
                  <li aria-hidden="true"><ChevronRight size={12} /></li>
                  <li><span aria-current="page" title={view === 'model' ? editing?.display_name : undefined}>{view === 'discovery' ? t('获取模型') : editing?.display_name || t('添加模型')}</span></li>
                </>}
          </>}
        </ol>
      </nav>
      {view === 'list' && <Button type="button" size="xs" variant="primary" leadingIcon={<Plus size={14} />} disabled={state.loading || state.saving || state.loadFailed} onClick={() => setView('new')}>{t('添加提供方')}</Button>}
    </div>
    <div key={view} className={view === 'list' ? 'settings-models__overview' : 'settings-models__page'}>
      {view === 'new' || (view === 'connection' && connection) ? <ModelConnectionForm key={view === 'new' ? 'new' : connection?.connection_id} connection={view === 'new' ? undefined : connection} presets={state.presets} modelCount={models.length} saving={state.saving} onCancel={close} onSave={async value => { const saved = await state.saveConnection(value); if (saved) { setSelected(value.connection_id); setSearch('') }; return saved }} onRemove={state.removeConnection} />
        : view === 'model' && editing && connection ? <ModelConfigurationForm key={editing.model_id} model={editing} connection={connection} existing={state.models.some(model => model.model_id === editing.model_id)} saving={state.saving} onSave={state.save} onRemove={state.remove} onCancel={close} onToast={onToast} />
          : view === 'discovery' && connection ? <ModelDiscoveryPanel connection={connection} models={models} saving={state.saving} onAdd={state.addModels} onManual={() => editModel(newModel(connection.connection_id))} onCancel={close} />
            : <>
              {state.loadFailed ? <div role="status"><p>{t('模型加载失败，请先重试')}</p><Button type="button" onClick={state.reload}>{t('重新加载模型')}</Button></div> : state.loading ? <p role="status">{t('正在加载模型配置')}</p> : state.connections.length === 0 ? <div className="settings-models__empty"><Cpu size={28} /><h4>{t('连接你的第一个模型服务')}</h4><p>{t('添加云端提供方，或连接本地 Ollama')}</p><Button type="button" onClick={() => setView('new')}>{t('添加提供方')}</Button></div> : <>
                <div className="settings-models__default"><MessageSquare size={14} /><span>{t('默认对话模型')}</span><strong>{defaultModel?.display_name ?? t('未设置')}</strong></div>
                <ModelProviderSplit sidebar={<>
                  <TextField shape="standard" fieldSize="md" rootClassName="settings-models__search" label={<span className="visually-hidden">{t('搜索提供方或模型')}</span>} placeholder={t('搜索提供方或模型')} value={search} leadingContent={<Search size={14} />} onChange={event => setSearch(event.target.value)} />
                  <nav className="settings-models__providers" aria-label={t('提供方')}>
                  {providers.map(({ connection: item, matches }) => {
                    const Icon = item.api_type === 'ollama' ? Monitor : Cpu
                    return <button type="button" key={item.connection_id} title={item.display_name} className={`settings-models__provider${connection?.connection_id === item.connection_id ? ' is-selected' : ''}`} aria-current={connection?.connection_id === item.connection_id ? 'true' : undefined} onClick={() => setSelected(item.connection_id)}><Icon size={18} /><span><strong>{item.display_name}</strong><small>{t('{count} 个模型', { count: matches.length })}</small></span></button>
                  })}
                </nav></>}>{connection ? <section className="settings-models__detail" aria-label={connection.display_name}>
                  <div className="settings-models__provider-heading"><h4>{connection.display_name}</h4><Button type="button" size="xs" variant="text" leadingIcon={<Settings2 size={14} />} onClick={() => setView('connection')}>{t('连接设置')}</Button></div>
                  <div className="settings-models__connection-meta"><span><Link2 size={13} />{connection.base_url}</span><span><KeyRound size={13} />{t(connection.auth_type === 'none' ? '无需认证' : '已配置密钥')}</span></div>
                  <div className="settings-models__list-heading"><h4>{t('模型')}</h4><div className="settings-models__row-actions"><Button type="button" size="xs" leadingIcon={<RefreshCw size={13} />} onClick={() => setView('discovery')}>{t('获取模型')}</Button><Button type="button" size="xs" variant="text" leadingIcon={<Plus size={14} />} onClick={() => editModel(newModel(connection.connection_id))}>{t('手动添加')}</Button></div></div>
                  <div className="settings-models__list-shell"><div className="settings-models__list-scroll ui-scrollbar" role="region" aria-label={t('模型列表')} tabIndex={0} ref={modelViewport}>
                  {matchingModels.map(model => <div key={model.model_id} className="settings-models__row"><div className="settings-models__identity"><strong>{model.display_name}{model.is_default && <span className="settings-models__badge">{t('默认')}</span>}</strong><small>{model.model_name}</small><small>{t(model.purpose === 'image' ? '图片生成' : '对话模型')}{model.reasoning_enabled ? ` · ${t('推理')}` : ''}{model.image_support === 'supported' ? ` · ${t('图片输入')}` : ''}{!model.enabled ? ` · ${t('已停用')}` : ''}</small></div><div className="settings-models__row-actions"><Button type="button" variant="ghost" size="xs" aria-label={t('设为默认')} title={t('设为默认')} disabled={state.saving || model.is_default} onClick={() => { void state.makeDefault(model.model_id) }}><Star size={15} /></Button><Button type="button" variant="ghost" size="xs" aria-label={t('配置模型 {name}', { name: model.display_name })} title={t('模型设置')} disabled={state.saving} onClick={() => editModel(model)}><SlidersHorizontal size={15} /></Button><Button type="button" variant="ghost" size="xs" role="switch" aria-checked={model.enabled} aria-label={t('启用模型 {name}', { name: model.display_name })} disabled={state.saving || model.is_default} onClick={() => { void state.save({ ...model, enabled: !model.enabled }) }}><span className={`settings-models__switch${model.enabled ? ' is-enabled' : ''}`} /></Button></div></div>)}
                  {models.length === 0 && <div className="settings-models__empty"><p>{t('还没有添加模型')}</p><span>{t('获取可用模型，或手动填写 Model ID')}</span></div>}
                  </div><OverlayScrollbar viewportRef={modelViewport} /></div>
                </section> : <div className="settings-models__empty" role="status"><p>{t('没有匹配的提供方或模型')}</p><Button type="button" onClick={() => setSearch('')}>{t('清除搜索')}</Button></div>}</ModelProviderSplit>
              </>}
            </>}
    </div>
  </section>
}
