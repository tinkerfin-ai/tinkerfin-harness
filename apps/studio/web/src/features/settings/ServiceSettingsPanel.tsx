import { lazy, Suspense, useEffect, useId, useRef, useState } from 'react'
import { CheckCircle2, ChevronDown, CircleAlert, Eye, EyeOff, Image, LoaderCircle, Search, Square, Trash2 } from 'lucide-react'
import { Button, ErrorBoundary, IconButton, TextField, ValidatedForm, ViewTabs } from '../../components/ui'
import { isTranslationKey, useI18n } from '../../i18n'
import { ImageOutputFormats } from './ImageOutputFormats'
import { ModelChoice } from './ModelChoice'
import { parseServiceOptions } from './modelOptions'
import { newServiceDraft, serviceCredentialChanged, serviceDirty, serviceDraftErrors, serviceLabels, servicePresetLabels, serviceReservedKeys, type ServiceDraft, type ServicePreset } from './serviceSettings'
import type { ServiceSettingsState } from './useServiceSettings'
import './model-settings.css'
import './service-settings.css'

const ModelOptionsEditor = lazy(() => import('./ModelOptionsEditor'))

function ServiceJsonField({ label, value, onChange, hint, error, reserved, disabled }: {
  label: string; value: string; onChange: (value: string) => void; hint: string; error?: string; reserved: string[]; disabled: boolean
}) {
  const { t } = useI18n()
  const id = useId()
  const parsed = parseServiceOptions(value, reserved)
  return <div className="settings-services__json">
    <span className="settings-services__label">{label}</span>
    <ErrorBoundary fallback={() => <>
      <p className="settings-services__hint" role="status">{t('高级编辑器不可用，可继续使用纯文本编辑')}</p>
      <textarea className="settings-services__json-input" aria-label={t('高级参数 JSON')} aria-describedby={id}
        aria-invalid={parsed.issues.length > 0} value={value} disabled={disabled} spellCheck={false}
        onChange={event => onChange(event.target.value)} />
    </>}>
      <Suspense fallback={<p role="status">{t('加载中')}</p>}>
        <ModelOptionsEditor disabled={disabled} value={value} onChange={onChange} issues={parsed.issues} issueMessage={() => t('请检查 JSON 格式和受控字段')} descriptionId={id} />
      </Suspense>
    </ErrorBoundary>
    <small id={id}>{hint}</small>
    {error && <p className="settings-models__error" role="alert" tabIndex={-1}>{error}</p>}
  </div>
}

const testFailureMessages: Record<string, string> = {
  authentication_failed: '密钥或服务权限不正确', rate_limited: '服务额度不足或请求过于频繁', timeout: '等待超时，服务可能已执行',
  response_too_large: '服务响应超过大小限制', invalid_response: '服务响应格式不正确', network_error: '连接失败，请检查地址和网络', service_error: '服务返回错误，请稍后重试',
}

function ServiceEditor({ state, confirmClose, onCloseDecision }: {
  state: ServiceSettingsState; confirmClose: boolean; onCloseDecision: (discard: boolean) => void
}) {
  const { t } = useI18n()
  const text = (value: string) => isTranslationKey(value) ? t(value) : value
  const capability = state.capability
  const draft = state.drafts[capability]
  const saved = state.saved[capability]
  const custom = draft.preset === 'custom'
  const dirty = serviceDirty(draft, saved)
  const credentialChanged = serviceCredentialChanged(draft, saved)
  const [attempt, setAttempt] = useState(0)
  const [showKey, setShowKey] = useState(false)
  const [confirmClear, setConfirmClear] = useState(false)
  const keepRef = useRef<HTMLButtonElement>(null)
  const continueRef = useRef<HTMLButtonElement>(null)
  const clearRef = useRef<HTMLButtonElement>(null)
  const wasConfirmingClear = useRef(false)
  const scrollRef = useRef<HTMLDivElement>(null)
  const formId = useId()
  const errors = serviceDraftErrors(draft, saved)
  const error = (key: string) => attempt ? text(errors[key] ?? '') || undefined : undefined
  const test = state.tests[capability]
  const testing = test?.outcome === 'running'
  const busy = state.saving !== null || testing
  const outcome = test?.outcome ?? saved?.test_status
  const testMessage = !saved ? '保存后可测试' : dirty ? '先保存修改' : !draft.enabled ? '启用并保存后可测试'
    : outcome === 'running' ? '等待服务响应' : outcome === 'stopped' ? '已停止等待，可能已消耗额度'
      : outcome === 'success' ? '测试通过' : outcome === 'failed' ? testFailureMessages[test?.code ?? saved?.test_code ?? ''] ?? '测试失败，请检查配置' : '测试可能消耗额度'
  useEffect(() => { if (confirmClose) continueRef.current?.focus({ preventScroll: true }) }, [confirmClose])
  useEffect(() => {
    if (confirmClear) keepRef.current?.focus({ preventScroll: true })
    else if (wasConfirmingClear.current) clearRef.current?.focus({ preventScroll: true })
    wasConfirmingClear.current = confirmClear
  }, [confirmClear])
  const change = <K extends keyof ServiceDraft>(key: K, value: ServiceDraft[K]) => state.change({ ...draft, [key]: value })
  const choosePreset = (preset: ServicePreset) => {
    if (preset === draft.preset) return
    const defaults = newServiceDraft(capability)
    state.change({ ...draft, preset, apiKey: '', endpoint: preset === 'custom' ? '' : preset === 'fal' ? 'https://fal.run/fal-ai/flux/schnell' : defaults.endpoint, auth: 'bearer', header: 'Authorization', prefix: 'Bearer ', method: 'POST' })
    setAttempt(0)
  }
  const positionMenu = (target: EventTarget | null) => {
    if (!(target instanceof Element)) return
    const trigger = target.closest<HTMLButtonElement>('button[aria-haspopup="listbox"]')
    const picker = trigger?.closest<HTMLElement>('.settings-choice-picker')
    if (!trigger || !picker || !scrollRef.current) return
    const bounds = trigger.getBoundingClientRect(), frame = scrollRef.current.getBoundingClientRect()
    const below = frame.bottom - bounds.bottom, above = bounds.top - frame.top
    const up = below < bounds.height * 4 && above > below
    picker.dataset.openDirection = up ? 'up' : 'down'
    picker.style.setProperty('--service-menu-room', `${Math.max(0, Math.floor((up ? above : below) - 8))}px`)
  }
  return <section className="settings-services__editor" aria-label={text(serviceLabels[capability])}>
    <div ref={scrollRef} className="settings-services__scroll ui-scrollbar" onPointerDownCapture={event => positionMenu(event.target)} onKeyDownCapture={event => { if (['Enter', ' ', 'ArrowDown', 'ArrowUp'].includes(event.key)) positionMenu(event.target) }}>
      {!saved && <p className="settings-services__hint">{t(capability === 'web_search' ? '配置后，Agent 可搜索实时网页资料' : '未配置专用服务，代码绘图仍可使用')}</p>}
      <ValidatedForm id={formId} className="settings-services__form" errors={attempt ? errors : {}} validationAttempt={attempt} onSubmit={event => { event.preventDefault(); setAttempt(value => value + 1); if (!Object.keys(errors).length) void state.save(capability) }}>
        <fieldset className="settings-models__form" disabled={busy}>
          <div className={capability === 'image_generation' && draft.preset === 'openai' ? 'settings-models__grid' : undefined}>
            <ModelChoice disabled={busy} label={t('接入方式')} value={draft.preset} options={capability === 'web_search' ? ['tavily', 'custom'] : ['openai', 'fal', 'custom']} text={value => text(servicePresetLabels[value])} onChange={choosePreset} />
            {capability === 'image_generation' && draft.preset === 'openai' && <TextField shape="standard" fieldSize="md" name="model" label={t('模型 ID')} maxLength={128} value={draft.model} onChange={event => change('model', event.target.value)} error={error('model')} spellCheck={false} />}
          </div>
          <TextField shape="standard" fieldSize="md" name="endpoint" label={t(custom || draft.preset === 'fal' ? '请求地址' : '服务地址')} value={draft.endpoint} maxLength={1024} onChange={event => change('endpoint', event.target.value)} error={error('endpoint')} spellCheck={false} placeholder="https://" />
          {custom && <div className="settings-models__grid"><ModelChoice disabled={busy} label={t('请求方式')} value={draft.method} options={['POST', 'GET']} text={value => value} onChange={value => change('method', value)} /><ModelChoice disabled={busy} label={t('认证方式')} value={draft.auth} options={['bearer', 'header', 'none']} text={value => text({ bearer: 'Bearer Token', header: '自定义认证头', none: '无需认证' }[value])} onChange={value => change('auth', value)} /></div>}
          {custom && draft.auth === 'header' && <div className="settings-models__grid"><TextField shape="standard" fieldSize="md" name="header" label={t('认证头名称')} value={draft.header} error={error('header')} onChange={event => change('header', event.target.value)} /><TextField shape="standard" fieldSize="md" label={t('凭证前缀')} value={draft.prefix} onChange={event => change('prefix', event.target.value)} helperText={t('例如 Key，末尾保留一个空格')} /></div>}
          {(!custom || draft.auth !== 'none') && <TextField shape="standard" fieldSize="md" name="apiKey" label="API Key" type={showKey ? 'text' : 'password'} autoComplete="off" placeholder={t(saved?.has_key && !credentialChanged ? '已保存密钥，留空保留' : '填写服务提供方的 API Key')} value={draft.apiKey} onChange={event => change('apiKey', event.target.value)} error={error('apiKey')} trailingContent={<IconButton type="button" size="xs" variant="ghost" label={t(showKey ? '隐藏新输入的密钥' : '显示新输入的密钥')} icon={showKey ? <EyeOff size={16} /> : <Eye size={16} />} onClick={() => setShowKey(!showKey)} />} />}
          {capability === 'image_generation' && !custom && <div className="settings-models__grid"><TextField shape="standard" fieldSize="md" label={t('图片尺寸')} placeholder={t('使用模型默认值')} value={draft.size} onChange={event => change('size', event.target.value)} /><ImageOutputFormats value={draft.formats} onChange={value => change('formats', value)} disabled={busy} /></div>}
          {capability === 'web_search' && <details className="settings-services__parameters" open={attempt && (errors.maxResults || errors.options) ? true : undefined}>
            <summary><span>{t('搜索参数')}</span><span className="settings-services__summary">{t(custom ? '自定义 HTTP' : draft.depth === 'basic' ? '标准检索' : '增强检索')} · {t('{count} 条', { count: draft.maxResults || '—' })}</span><ChevronDown size={15} aria-hidden="true" /></summary>
            <div className="settings-services__parameters-body"><div className={!custom ? 'settings-models__grid' : undefined}>{!custom && <ModelChoice disabled={busy} label={t('检索方式')} value={draft.depth} options={['basic', 'advanced']} text={value => t(value === 'basic' ? '标准检索' : '增强检索')} onChange={value => change('depth', value)} />}<TextField shape="standard" fieldSize="md" name="maxResults" label={t('最多返回结果数')} type="number" min={1} max={10} value={draft.maxResults} onChange={event => change('maxResults', event.target.value)} error={error('maxResults')} /></div>
              {!custom && <><p className="settings-services__hint">{t('标准检索每次 1 个 Tavily 积分，增强检索每次 2 个积分')}</p><details className="settings-services__parameters" open={attempt && errors.options ? true : undefined}><summary>{t('附加参数（JSON）')}<ChevronDown size={15} aria-hidden="true" /></summary><ServiceJsonField disabled={busy} label={t('附加参数')} value={draft.options} onChange={value => change('options', value)} hint={t('仅在服务提供方要求额外参数时填写')} error={error('options')} reserved={serviceReservedKeys(draft)} /></details></>}
            </div>
          </details>}
          {custom ? <div className="settings-services__custom"><h4>{t('请求与结果')}</h4><ServiceJsonField disabled={busy} label={t(draft.method === 'GET' ? '查询参数 JSON' : '请求正文 JSON')} value={draft.body} onChange={value => change('body', value)} hint={t(capability === 'image_generation' ? '用 ${prompt} 填入图片描述，凭证通过认证设置发送' : '用 ${query} 填入关键词，${max_results} 填入结果数量')} error={error('body')} reserved={serviceReservedKeys(draft)} />
            {capability === 'image_generation' && <ModelChoice disabled={busy} label={t('图片返回方式')} value={draft.responseType} options={['url', 'base64', 'binary']} text={value => text({ url: 'JSON 中的图片 URL', base64: 'JSON 中的 Base64', binary: '图片二进制' }[value])} onChange={value => change('responseType', value)} />}
            {(capability === 'web_search' || draft.responseType !== 'binary') && <><TextField shape="standard" fieldSize="md" name="itemsPointer" label={t('结果列表路径')} value={draft.itemsPointer} error={error('itemsPointer')} onChange={event => change('itemsPointer', event.target.value)} /><div className="settings-models__grid">
              {capability === 'web_search' && <><TextField shape="standard" fieldSize="md" name="titlePointer" label={t('标题路径')} value={draft.titlePointer} error={error('titlePointer')} onChange={event => change('titlePointer', event.target.value)} /><TextField shape="standard" fieldSize="md" name="urlPointer" label={t('来源 URL 路径')} value={draft.urlPointer} error={error('urlPointer')} onChange={event => change('urlPointer', event.target.value)} /></>}
              <TextField shape="standard" fieldSize="md" name="valuePointer" label={t(capability === 'web_search' ? '摘要路径' : '单张图片结果路径')} value={draft.valuePointer} error={error('valuePointer')} onChange={event => change('valuePointer', event.target.value)} />
            </div></>}
            {capability === 'image_generation' && <ImageOutputFormats value={draft.formats} onChange={value => change('formats', value)} disabled={busy} />}
          </div> : capability === 'image_generation' && <details className="settings-services__parameters" open={attempt && errors.options ? true : undefined}><summary>{t('生成参数')}<ChevronDown size={15} aria-hidden="true" /></summary><ServiceJsonField disabled={busy} label={t('附加参数')} value={draft.options} onChange={value => change('options', value)} hint={t('仅在服务提供方要求额外参数时填写')} error={error('options')} reserved={serviceReservedKeys(draft)} /></details>}
          <label className="settings-models__check"><input type="checkbox" checked={draft.enabled} onChange={event => change('enabled', event.target.checked)} />{t(saved ? '启用服务' : '保存后启用服务')}</label>
        </fieldset>
        <div className="settings-services__test"><span className={`settings-services__test-status${outcome === 'failed' ? ' is-error' : ''}`} role={outcome === 'failed' ? 'alert' : 'status'}>{testing ? <LoaderCircle className="settings-services__test-spinner" size={16} aria-hidden="true" /> : outcome === 'success' ? <CheckCircle2 size={16} aria-hidden="true" /> : outcome === 'failed' ? <CircleAlert size={16} aria-hidden="true" /> : null}{text(testMessage)}</span><Button type="button" size="md" disabled={!testing && (!saved || dirty || !draft.enabled || state.saving !== null)} tooltip={t('每次测试可能消耗服务额度')} onClick={() => { void state.test(capability) }} leadingIcon={testing ? <Square size={14} /> : capability === 'web_search' ? <Search size={15} /> : <Image size={15} />}>{t(testing ? '停止' : capability === 'web_search' ? '测试搜索' : '试生成')}</Button></div>
      </ValidatedForm>
    </div>
    <footer className="settings-services__footer">
      {state.errors[capability] && <p className="settings-models__error" role="alert">{text(state.errors[capability])}</p>}
      {confirmClose ? (
        <div className="settings-services__confirm">
          <p>{t('有尚未保存的修改，关闭后将丢失')}</p>
          <div className="settings-services__actions">
            <Button ref={continueRef} type="button" onClick={() => onCloseDecision(false)}>{t('继续编辑')}</Button>
            <Button type="button" onClick={() => onCloseDecision(true)}>{t('放弃修改并关闭')}</Button>
          </div>
        </div>
      ) : confirmClear ? (
        <div className="settings-services__confirm">
          <p>{t('清除配置及密钥？此操作无法恢复')}</p>
          <div className="settings-services__actions">
            <Button ref={keepRef} type="button" disabled={busy} onClick={() => setConfirmClear(false)}>{t('保留配置')}</Button>
            <Button type="button" variant="danger" disabled={busy} loading={state.saving === capability} onClick={() => {
              void state.clear(capability).then(ok => {
                if (ok) { setConfirmClear(false); setAttempt(0) }
              })
            }}>{t('清除配置')}</Button>
          </div>
        </div>
      ) : <>
        {!state.errors[capability] && state.feedback[capability] && <p className="settings-services__feedback" role="status">{text(state.feedback[capability])}</p>}
        <div className="settings-services__footer-row">
          {saved && <Button ref={clearRef} type="button" variant="text" disabled={busy} onClick={() => setConfirmClear(true)} leadingIcon={<Trash2 size={14} />}>{t('清除配置')}</Button>}
          <div className="settings-services__actions">
            <Button type="button" disabled={!dirty || busy} onClick={() => { state.reset(capability); setAttempt(0) }}>{t('取消修改')}</Button>
            <Button type="submit" form={formId} variant="primary" disabled={busy} loading={state.saving === capability}>{t('保存')}</Button>
          </div>
        </div>
      </>}
    </footer>
  </section>
}

export function ServiceSettingsPanel({ state, confirmClose, onCloseDecision }: {
  state: ServiceSettingsState; confirmClose: boolean; onCloseDecision: (discard: boolean) => void
}) {
  const { t } = useI18n()
  const text = (value: string) => isTranslationKey(value) ? t(value) : value
  return <section className="settings-models settings-services">
    <div className="settings-services__tabs"><ViewTabs value={state.capability} label={t('服务能力')} options={(['web_search', 'image_generation'] as const).map(value => ({ value, label: text(serviceLabels[value]) }))} onChange={state.setCapability} /></div>
    {state.loading ? <div className="settings-services__loading" role="status">{t('正在加载服务配置')}</div> : state.loadFailed ? <div className="settings-services__loading" role="alert"><p>{t('服务配置加载失败')}</p><Button type="button" onClick={state.reload}>{t('重新加载')}</Button></div> : <ServiceEditor key={state.capability} state={state} confirmClose={confirmClose} onCloseDecision={onCloseDecision} />}
  </section>
}
