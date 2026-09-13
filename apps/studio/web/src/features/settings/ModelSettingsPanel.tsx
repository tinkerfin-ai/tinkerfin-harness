import { type SyntheticEvent, lazy, Suspense, useCallback, useId, useMemo, useRef, useState } from 'react'
import { ArrowLeft, Check, ChevronDown, Code2, FlaskConical, Image, KeyRound, Link2, MessageSquare, Settings2 } from 'lucide-react'
import { Button, ErrorBoundary, ListboxPicker, OverlayScrollbar, TextField, ValidatedForm } from '../../components/ui'
import { useI18n, type TranslationKey } from '../../i18n'
import { newModel, useModelSettings, type ModelSettings } from './useModelSettings'
import { combineModelOptions, MAX_MODEL_OPTIONS_BYTES, parseModelOptions, type OptionsIssue } from './modelOptions'
import { ModelTestPanel } from './ModelTestPanel'
import type { ToastKind } from '../../components/ui/ToastViewport'
import { useModelTest, type ModelTestKind } from './useModelTest'
import { useSettingsDisclosureScroll } from './useSettingsDisclosureScroll'
import { useModelFormValidation } from './useModelFormValidation'
import './model-settings.css'

const ModelOptionsEditor = lazy(() => import('./ModelOptionsEditor'))
const issueLabels: Record<OptionsIssue['code'], TranslationKey> = {
  too_deep: 'JSON 嵌套不能超过 64 层', too_large: '高级参数不能超过 64 KiB', syntax: 'JSON 语法不正确，请检查引号、逗号和括号',
  object_required: '高级参数必须是 JSON 对象', duplicate: '参数 {key} 重复，请只保留一处',
  reserved: '参数 {key} 由系统管理，不能在此覆盖', common: '参数 {key} 请在上方常用设置中填写',
  nonfinite: '参数值必须是有限数值',
}

/** 复用设置页选择器的键盘导航、选中状态和焦点恢复 */
function ModelChoice<T extends string>({ label, value, options, onChange, text, disabled = false }: {
  label: string; value: T; options: readonly T[]; onChange: (value: T) => void;
  text: (value: T) => string; disabled?: boolean
}) {
  const [open, setOpen] = useState(false)
  return <div className="settings-models__choice">
    <span>{label}</span>
    <ListboxPicker value={value} options={options} onChange={onChange} open={open} onOpenChange={setOpen} disabled={disabled}
      triggerLabel={label} listboxLabel={label} rootClassName="settings-choice-picker" triggerClassName="settings-choice-trigger"
      listboxClassName="settings-choice-options" optionClassName="settings-choice-option"
      renderTrigger={(option) => <>{text(option)}<ChevronDown size={14} aria-hidden="true" /></>}
      renderOption={(option, selected) => <>{text(option)}<span className="settings-choice-check">{selected && <Check size={14} aria-hidden="true" />}</span></>}
    />
  </div>
}

export function ModelSettingsPanel({ onChanged, onToast }: { onChanged?: () => void; onToast: (kind: ToastKind, message: string) => void }) {
  const { t } = useI18n()
  const id = useId()
  const scrollViewport = useRef<HTMLDivElement>(null)
  const testSection = useRef<HTMLDetailsElement>(null)
  const nameInput = useRef<HTMLInputElement>(null)
  const chatProvider = useRef<ModelSettings['provider']>('openai')
  const [advancedOpen, setAdvancedOpen] = useState(false)
  const [testOpen, setTestOpen] = useState(false)
  const state = useModelSettings(onChanged)
  const test = useModelTest(onToast)
  const revealDisclosure = useSettingsDisclosureScroll()
  const { models, loading, loadFailed, editing, key, options, saving, deleting, imageSize, imageFormat, canReuseKey } = state
  const validation = useModelFormValidation(editing, key, canReuseKey)
  const parsed = useMemo(() => parseModelOptions(options), [options])
  const merged = parsed.value ? combineModelOptions(parsed.value, imageSize, imageFormat) : null
  const combinedTooLarge = merged !== null && new TextEncoder().encode(JSON.stringify(merged)).length > MAX_MODEL_OPTIONS_BYTES
  const invalidOptions = editing?.purpose === 'image' && (!merged || combinedTooLarge)
  const issueMessage = useCallback((issue: OptionsIssue) => {
    const prefix = options.slice(0, issue.from).split('\n')
    return t('第 {line} 行，第 {column} 列：{message}', { line: prefix.length, column: (prefix.at(-1)?.length ?? 0) + 1, message: t(issueLabels[issue.code], { key: issue.key ?? '' }) })
  }, [options, t])
  const knownFormats = ['', 'png', 'jpeg', 'webp']
  const formatOptions = knownFormats.includes(imageFormat) ? knownFormats : [...knownFormats, imageFormat]
  const update = (patch: Partial<ModelSettings>) => { test.invalidate(); state.setEditing((current) => current ? { ...current, ...patch } : current) }
  const startEditing = (model: ModelSettings, testing = false) => {
    chatProvider.current = model.provider
    state.edit(model); test.reset(); validation.reset(); setAdvancedOpen(false); setTestOpen(testing)
    requestAnimationFrame(() => nameInput.current?.focus({ preventScroll: true }))
  }
  const leaveEditing = () => { test.reset(); validation.reset(); state.cancelEdit() }
  const runTest = (kind: ModelTestKind) => {
    if (!editing || !validation.validate() || invalidOptions) return
    if (testSection.current) revealDisclosure(testSection.current)
    const { has_key: _hasKey, ...configuration } = editing
    void _hasKey
    void test.run(kind, { ...configuration, api_key: key, generation_options: editing.purpose === 'image' ? merged ?? {} : {} })
  }
  const handleDisclosureToggle = (event: SyntheticEvent<HTMLDetailsElement>, setOpen?: (open: boolean) => void) => {
    if (event.target !== event.currentTarget) return
    setOpen?.(event.currentTarget.open)
    revealDisclosure(event.currentTarget)
  }
  const optionsError = combinedTooLarge ? t('高级参数不能超过 64 KiB') : parsed.issues[0] ? issueMessage(parsed.issues[0]) : undefined
  const optionsCount = parsed.value ? Object.keys(parsed.value).length : 0

  return <section className="settings-section settings-models">
    {editing && <div className="settings-models__navigation">
      <Button type="button" className="settings-models__back" variant="text" size="sm" leadingIcon={<ArrowLeft size={16} />} disabled={saving} onClick={leaveEditing}>{t('返回模型列表')}</Button>
    </div>}
    <div ref={scrollViewport} className="settings-models__scroll ui-scrollbar" role="region" aria-label={t('设置')} tabIndex={0}>
    {!editing && <div className="settings-models__heading">
      <h3>{t('模型配置')}</h3>
      <Button type="button" variant="primary" size="xs" disabled={loading || saving} onClick={() => startEditing(newModel())}>{t('添加模型')}</Button>
    </div>}
    {loadFailed && !editing && <div className="settings-models__error-row"><Button type="button" size="xs" disabled={saving} onClick={() => state.setRevision((value) => value + 1)}>{t('重新加载模型')}</Button></div>}
    {editing ? <>
      <ValidatedForm id={`${id}-form`} errors={validation.errors} validationAttempt={validation.attempt} onSubmit={(event) => { event.preventDefault(); if (validation.validate() && !invalidOptions) { test.invalidate(); void state.save(editing.purpose === 'image' ? merged ?? {} : {}) } }}>
        <fieldset className="settings-models__form" disabled={saving}>
          <div className="settings-models__section-title"><Settings2 size={16} aria-hidden="true" /><h4>{t('基本信息')}</h4></div>
          <div className="settings-models__grid">
            <div className="settings-models__wide"><span className="settings-models__label">{t('用途')}</span>
              <div className="settings-models__purpose" role="radiogroup" aria-label={t('用途')}>
                {(['chat', 'image'] as const).map((purpose) => <label key={purpose} className={editing.purpose === purpose ? 'is-selected' : undefined}>
                  <input type="radio" name={`${id}-purpose`} value={purpose} checked={editing.purpose === purpose} onChange={() => { if (editing.purpose === 'chat') chatProvider.current = editing.provider; update({ purpose, provider: purpose === 'image' ? 'openai' : chatProvider.current }) }} />
                  {purpose === 'chat' ? <MessageSquare size={16} aria-hidden="true" /> : <Image size={16} aria-hidden="true" />}{t(purpose === 'chat' ? '对话模型' : '图片生成')}
                </label>)}
              </div>
            </div>
            <TextField ref={nameInput} shape="standard" fieldSize="md" label={t('显示名称')} name="display_name" error={validation.errors.display_name} value={editing.display_name} required maxLength={128} placeholder={t('请输入显示名称')} onChange={(event) => update({ display_name: event.target.value })} />
            <TextField shape="standard" fieldSize="md" label="Model ID" name="model_name" error={validation.errors.model_name} value={editing.model_name} required maxLength={128} placeholder={t('填写服务商提供的模型名称')} onChange={(event) => update({ model_name: event.target.value })} />
          </div>
          <div className="settings-models__section-title"><Link2 size={16} aria-hidden="true" /><h4>{t('连接信息')}</h4></div>
          <div className="settings-models__grid">
            {editing.purpose === 'chat' && <ModelChoice label={t('接口类型')} value={editing.provider} options={['openai', 'deepseek']} text={(value) => value === 'openai' ? `OpenAI ${t('兼容接口')}` : 'DeepSeek'} disabled={saving} onChange={(provider) => update({ provider })} />}
            <TextField rootClassName="settings-models__wide" shape="standard" fieldSize="md" label="Base URL" name="base_url" error={validation.errors.base_url} value={editing.base_url} required type="url" maxLength={1024} placeholder={t('请输入接口地址，如 https://api.openai.com/v1')} onChange={(event) => update({ base_url: event.target.value })} />
            <TextField rootClassName="settings-models__wide" shape="standard" fieldSize="md" label="API Key" name="api_key" error={validation.errors.api_key} leadingContent={<KeyRound size={15} />} type="password" autoComplete="new-password" value={key} required={!canReuseKey} placeholder={t(canReuseKey ? '留空保留已保存的密钥' : editing.has_key ? '服务地址已更改，请重新输入 API Key' : '请输入 API Key')} onChange={(event) => { test.invalidate(); state.setKey(event.target.value) }} />
          </div>
          <div className="settings-models__section-title">{editing.purpose === 'image' ? <Image size={16} aria-hidden="true" /> : <Settings2 size={16} aria-hidden="true" />}<h4>{t(editing.purpose === 'image' ? '图片设置' : '模型能力')}</h4></div>
          {editing.purpose === 'chat' ? <div className="settings-models__grid">
            <ModelChoice label={t('图片输入能力')} value={editing.image_support} options={['unknown', 'supported', 'unsupported']} disabled={saving} text={(value) => t(value === 'unknown' ? '未确认' : value === 'supported' ? '支持图片' : '仅文字')} onChange={(image_support) => update({ image_support })} />
            {editing.provider === 'deepseek' && <label className="settings-models__check"><span>{t('启用推理')}</span><input type="checkbox" checked={editing.reasoning_enabled} onChange={(event) => update({ reasoning_enabled: event.target.checked })} /></label>}
          </div> : <>
            <div className="settings-models__grid">
              <TextField shape="standard" fieldSize="md" label={t('图片尺寸')} value={imageSize} placeholder={t('使用服务默认')} maxLength={128} list={`${id}-sizes`} helperText={t('例如 2K 或 1024x1024，可用尺寸由服务商决定')} onChange={(event) => { test.invalidate(); state.setImageSize(event.target.value) }} />
              <datalist id={`${id}-sizes`}><option value="2K" /><option value="4K" /><option value="1024x1024" /><option value="1536x1024" /><option value="1024x1536" /></datalist>
              <ModelChoice label={t('图片格式')} value={imageFormat} options={formatOptions} disabled={saving} text={(value) => value ? value.toUpperCase() : t('使用服务默认')} onChange={(value) => { test.invalidate(); state.setImageFormat(value) }} />
            </div>
            <details className="settings-models__advanced" open={advancedOpen} onToggle={(event) => handleDisclosureToggle(event, setAdvancedOpen)}>
              <summary><Code2 size={15} aria-hidden="true" /><span>{t('高级参数')}</span><small>{optionsCount ? t('{count} 项', { count: optionsCount }) : t('可选')}</small>{optionsError && <span className="settings-models__invalid-dot">{t('需要修正')}</span>}</summary>
              <div className="settings-models__advanced-body">
                <div className="settings-models__editor-toolbar"><span>JSON</span><Button type="button" variant="text" disabled={saving || !parsed.value} onClick={() => state.setOptions(JSON.stringify(parsed.value, null, 2))}>{t('格式化')}</Button></div>
                {advancedOpen && <ErrorBoundary onError={() => onToast('error', t('高级编辑器不可用，可继续使用纯文本编辑'))} fallback={() => <>
                  <textarea className="model-options-plain" aria-label={t('高级参数 JSON')} aria-describedby={`${id}-options-help`} aria-invalid={Boolean(optionsError)} value={options} disabled={saving} spellCheck={false} onChange={(event) => { test.invalidate(); state.setOptions(event.target.value) }} />
                </>}>
                  <Suspense fallback={<p role="status">{t('正在加载编辑器…')}</p>}><ModelOptionsEditor descriptionId={`${id}-options-help`} value={options} onChange={(value) => { test.invalidate(); state.setOptions(value) }} disabled={saving} issues={parsed.issues} issueMessage={issueMessage} /></Suspense>
                </ErrorBoundary>}
                {optionsError && <p className="settings-models__error" role="alert">{optionsError}</p>}
                <p id={`${id}-options-help`} className="settings-models__hint">{t('仅填写服务商的额外参数；图片尺寸和格式在上方设置')}</p>
              </div>
            </details>
          </>}
          <div className="settings-models__flags">
            <label className="settings-models__check"><span>{t('启用')}</span><input type="checkbox" checked={editing.enabled} onChange={(event) => update({ enabled: event.target.checked, is_default: event.target.checked ? editing.is_default : false })} /></label>
            <label className="settings-models__check"><span>{t('设为此用途的默认模型')}</span><input type="checkbox" checked={editing.is_default} onChange={(event) => update({ is_default: event.target.checked, enabled: event.target.checked || editing.enabled })} /></label>
          </div>
        </fieldset>
        <details ref={testSection} className="settings-models__test-disclosure" open={testOpen} onToggle={(event) => handleDisclosureToggle(event, setTestOpen)}>
          <summary><FlaskConical size={16} aria-hidden="true" />{t('测试当前配置')}<ChevronDown size={14} aria-hidden="true" /></summary>
          <ModelTestPanel purpose={editing.purpose} disabled={saving || Boolean(invalidOptions)} {...test} onRun={runTest} onCancel={test.cancel} />
        </details>
      </ValidatedForm>
    </> : loading ? <p className="settings-models__empty" role="status">{t('加载中')}</p> : loadFailed && models.length === 0 ? null : models.length === 0 ? <div className="settings-models__empty"><Settings2 size={26} aria-hidden="true" /><p>{t('还没有模型配置，请先添加')}</p></div> : (['chat', 'image'] as const).map((purpose) => {
      const group = models.filter((model) => model.purpose === purpose)
      if (!group.length) return null
      return <section className="settings-models__group" key={purpose} aria-label={t(purpose === 'chat' ? '对话模型' : '图片生成')}>
        <h4>{purpose === 'chat' ? <MessageSquare size={15} aria-hidden="true" /> : <Image size={15} aria-hidden="true" />}{t(purpose === 'chat' ? '对话模型' : '图片生成')}</h4>
        {group.map((model) => <div key={model.model_id} className="settings-models__row">
          <div className="settings-models__identity"><div className="settings-models__name"><strong title={model.display_name}>{model.display_name}</strong>{model.is_default && <span className="settings-models__default">{t('默认')}</span>}</div><small><span>{t(model.enabled ? '已启用' : '已停用')}</span><span aria-hidden="true">·</span><span>{t(model.has_key ? '已配置密钥' : '未配置密钥')}</span></small></div>
          <div className="settings-models__row-actions">
            {deleting === model.model_id ? <><span className="settings-models__delete-label">{t('删除 {name}？', { name: model.display_name })}</span><Button type="button" variant="danger" loading={saving} onClick={() => void state.remove(model.model_id)}>{t('确认删除')}</Button><Button type="button" disabled={saving} onClick={() => state.setDeleting(undefined)}>{t('取消')}</Button></> : <>
              {!model.is_default && <Button type="button" className="settings-models__default-action" variant="ghost" disabled={saving || !model.has_key} title={!model.has_key ? t('请先配置密钥') : undefined} onClick={() => void state.makeDefault(model.model_id)}>{t(model.enabled ? '设为默认' : '启用并设为默认')}</Button>}
              <Button type="button" variant="ghost" disabled={saving} onClick={() => startEditing(model, true)}>{t('测试')}</Button>
              <Button type="button" variant="ghost" aria-label={t('编辑')} title={t('编辑')} disabled={saving} onClick={() => startEditing(model)}>{t('编辑')}</Button>
              <Button type="button" variant="ghost" aria-label={t('删除')} title={t('删除')} disabled={saving} onClick={() => state.setDeleting(model.model_id)}>{t('删除')}</Button>
            </>}
          </div>
        </div>)}
      </section>
    })}
    </div>
    {editing && <div className="settings-models__actions"><Button type="button" size="xs" disabled={saving} onClick={leaveEditing}>{t('取消')}</Button><Button type="submit" form={`${id}-form`} variant="primary" size="xs" loading={saving} disabled={Boolean(invalidOptions)}>{t('保存')}</Button></div>}
    <OverlayScrollbar viewportRef={scrollViewport} />
  </section>
}
