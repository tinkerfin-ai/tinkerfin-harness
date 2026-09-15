import { ChevronDown, Paperclip, X } from 'lucide-react'
import { useEffect, useId, useRef, useState, type ReactNode } from 'react'

import { Button, DatePicker, Dialog, IconButton, TextField, TimePicker, ValidatedForm, ViewTabs } from '../../components/ui'
import { AccessModePicker } from '../../components/AccessModePicker'
import { ApiError } from '../../api/shared/http'
import { uploadAttachment } from '../conversation/attachments/client'
import { saveTask } from './api'
import { useI18n } from '../../i18n'
import { todayInBeijing, emptyDraft, shiftDate, validateDraft, type AutomationDraft, type AutomationSchedule, type AutomationTask, type DraftErrors } from './model'
import { formatDate, formatSchedule } from './presentation'
import { AutomationPicker } from './AutomationPicker'

const repeatKinds = ['once', 'daily', 'workdays', 'weekly', 'monthly'] as const
const repeatLabels = { once: '单次', daily: '每天', workdays: '工作日', weekly: '每周', monthly: '每月' } as const
const units = ['minutes', 'hours', 'days'] as const
const unitLabels = { minutes: '分钟', hours: '小时', days: '天' } as const

/** 保留编辑内容直到后端确认保存；失败重试使用同一请求标识 */
export function AutomationEditor({ task, trigger, onSave, onClose, defaultModelId, renderModelChoice }: {
  task?: AutomationTask
  trigger: HTMLElement | null
  onSave: () => void
  defaultModelId: string
  renderModelChoice: (value: string, onChange: (value: string) => void) => ReactNode
  onClose: () => void
}) {
  const { locale, t } = useI18n()
  const id = useId()
  const nameRef = useRef<HTMLInputElement>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const [draft, setDraft] = useState<AutomationDraft>(() => task ? {
    ...task, name: task.name, prompt: task.prompt,
    schedule: { ...task.schedule }, attachments: [...task.attachments],
  } : { ...emptyDraft(), modelId: defaultModelId })
  const [saving, setSaving] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [failure, setFailure] = useState('')
  const [uncertain, setUncertain] = useState(false)
  const owned = useRef(new Set<AbortController>())
  const mounted = useRef(true)
  const saveIdentity = useRef<{ body: string; id: string } | null>(null)
  useEffect(() => { mounted.current = true; const requests = owned.current; return () => { mounted.current = false; for (const request of requests) request.abort() } }, [])
  useEffect(() => { if (defaultModelId) setDraft(current => current.modelId ? current : { ...current, modelId: defaultModelId }) }, [defaultModelId])
  const submit = async () => {
    if (saving || uploading) return
    const nextErrors = validateDraft(draft)
    setErrors(nextErrors)
    setValidationAttempt(value => value + 1)
    if (Object.keys(nextErrors).length) return
    const body = JSON.stringify(draft)
    if (!saveIdentity.current || saveIdentity.current.body !== body) saveIdentity.current = { body, id: crypto.randomUUID() }
    const controller = new AbortController()
    owned.current.add(controller)
    setSaving(true); setFailure('')
    try {
      await saveTask(draft, saveIdentity.current.id, task, controller.signal)
      if (!controller.signal.aborted && mounted.current) onSave()
    } catch (error) {
      if (controller.signal.aborted || !mounted.current) return
      const unknown = !(error instanceof ApiError) || error.status === 0 || error.status >= 500
      setUncertain(unknown)
      setFailure(unknown ? t('保存结果尚未确认，请重试保存') : error.message)
    } finally {
      owned.current.delete(controller)
      if (mounted.current) setSaving(false)
    }
  }
  const upload = async (files: File[]) => {
    if (uploading || saving) return
    if (files.length + draft.attachments.length > 5) { setFailure(t('最多添加5个参考文件')); return }
    const controller = new AbortController()
    owned.current.add(controller)
    setUploading(true); setFailure('')
    try {
      for (const file of files) {
        const attachment = await uploadAttachment(file, controller.signal, () => {})
        if (controller.signal.aborted || !mounted.current) return
        setDraft(current => ({ ...current, attachments: [...current.attachments, attachment.id], files: [...current.files, attachment] }))
      }
    } catch {
      if (!controller.signal.aborted && mounted.current) setFailure(t('参考文件上传失败，请重试'))
    } finally { owned.current.delete(controller); if (mounted.current) setUploading(false) }
  }
  const [panel, setPanel] = useState<'schedule' | 'validity' | null>(null)
  const [errors, setErrors] = useState<DraftErrors>({})
  const [validationAttempt, setValidationAttempt] = useState(0)
  const schedule = draft.schedule
  const update = <Key extends keyof AutomationDraft>(key: Key, value: AutomationDraft[Key]) => setDraft((current) => ({ ...current, [key]: value }))
  const changeKind = (kind: AutomationSchedule['kind']) => {
    const time = schedule.kind === 'interval' ? '09:00' : schedule.time
    switch (kind) {
      case 'once': update('schedule', { kind, date: shiftDate(todayInBeijing(), 1), time }); break
      case 'weekly': update('schedule', { kind, weekdays: [4], time }); break
      case 'monthly': update('schedule', { kind, day: 1, time }); break
      case 'interval': update('schedule', { kind, every: 2, unit: 'hours' }); break
      default: update('schedule', { kind, time })
    }
  }
  const validity = draft.startsOn && draft.endsOn ? t('{start}至{end}', { start: draft.startsOn, end: draft.endsOn })
    : draft.startsOn ? t('{date}起', { date: draft.startsOn })
      : draft.endsOn ? t('截至{date}', { date: draft.endsOn }) : t('长期有效')

  return <Dialog open title={t(task ? '编辑自动化' : '新建自动化')} className="automation-editor"
    initialFocusRef={nameRef} restoreFocusTo={trigger} closeDisabled={saving} onClose={onClose}>
    <ValidatedForm errors={errors} validationAttempt={validationAttempt} className="automation-form" onSubmit={(event) => {
      event.preventDefault()
      void submit()
    }}>
      {failure && <p role="alert">{failure}</p>}
      <fieldset className="automation-form-fields" disabled={saving || uncertain}>
      <TextField ref={nameRef} label={t('任务名称')} name="name" value={draft.name} onChange={(event) => update('name', event.target.value)}
        placeholder={t('例如：每日 AI 新闻简报')} autoComplete="off" maxLength={60} shape="standard" fieldSize="md"
        error={errors.name && t(errors.name)} />
      <div className="automation-instructions" data-validation-field="prompt">
        <label htmlFor={`${id}-prompt`}>{t('任务指令')}</label>
        <div className="automation-prompt-surface" data-validation-feedback={errors.prompt ? 'invalid' : undefined}>
          <textarea id={`${id}-prompt`} name="prompt" value={draft.prompt} placeholder={t('你希望自动完成什么')}
            aria-invalid={Boolean(errors.prompt)} aria-describedby={errors.prompt ? `${id}-prompt-error` : undefined}
            onChange={(event) => update('prompt', event.target.value)} />
          {draft.attachments.length > 0 && <div className="automation-attachments">
            {draft.files.map(file => <span key={file.id}>{file.name}<IconButton size="sm" label={t('移除参考文件：{name}', { name: file.name })}
              icon={<X size={14} />} disabled={uploading} onClick={() => setDraft(current => ({ ...current, attachments: current.attachments.filter(id => id !== file.id), files: current.files.filter(item => item.id !== file.id) }))} /></span>)}
          </div>}
          <div className="automation-prompt-tools">
            <IconButton size="sm" label={t('添加参考文件')} loading={uploading} icon={<Paperclip size={18} />} onClick={() => fileRef.current?.click()} />
            <input ref={fileRef} type="file" multiple hidden accept=".png,.jpg,.jpeg,.webp,.gif,.pdf,.docx,.xlsx,.pptx,.md,.markdown" onChange={event => {
              void upload(Array.from(event.currentTarget.files ?? [])); event.currentTarget.value = ''
            }} />
            {renderModelChoice(draft.modelId, value => update('modelId', value))}
          </div>
        </div>
        {errors.prompt && <small id={`${id}-prompt-error`} className="automation-field-error" role="alert">{t(errors.prompt)}</small>}
      </div>
      <div className="automation-schedule-summary">
        <Button type="button" variant="ghost" className="automation-config-button" aria-expanded={panel === 'schedule'}
          aria-invalid={Boolean(errors.schedule)} aria-controls={`${id}-schedule`} onClick={() => setPanel(panel === 'schedule' ? null : 'schedule')}>
          <span className="automation-config-content"><span className="automation-config-copy"><small>{t('执行频率')}</small>
            <strong>{formatSchedule(schedule, locale, t)}</strong></span><ChevronDown size={16} aria-hidden="true" /></span>
        </Button>
        <Button type="button" variant="ghost" className="automation-config-button" aria-expanded={panel === 'validity'}
          aria-invalid={Boolean(errors.validity)} aria-controls={`${id}-validity`} onClick={() => setPanel(panel === 'validity' ? null : 'validity')}>
          <span className="automation-config-content"><span className="automation-config-copy"><small>{t('有效期')}</small><strong>{validity}</strong></span><ChevronDown size={16} aria-hidden="true" /></span>
        </Button>
      </div>
      {errors.schedule && <p className="automation-field-error" role="alert">{t(errors.schedule)}</p>}
      {errors.validity && <p className="automation-field-error" role="alert">{t(errors.validity)}</p>}
      {errors.model && <p role="alert">{t(errors.model)}</p>}
      {panel === 'schedule' && <fieldset id={`${id}-schedule`} className="automation-config-panel">
        <legend className="visually-hidden">{t('执行频率')}</legend>
        <ViewTabs value={schedule.kind === 'interval' ? 'interval' : 'cycle'} label={t('执行频率')} density="compact"
          options={[{ value: 'cycle', label: t('周期') }, { value: 'interval', label: t('间隔') }]}
          onChange={(value) => changeKind(value === 'interval' ? 'interval' : 'daily')} />
        <div className="automation-config-fields">
          {schedule.kind === 'interval' ? <>
            <TextField label={t('每隔')} type="number" name="interval" min={1} max={999} value={Number.isFinite(schedule.every) ? schedule.every : ''}
              shape="standard" fieldSize="md" onChange={(event) => update('schedule', { ...schedule, every: event.currentTarget.valueAsNumber })} />
            <div className="automation-labeled-control"><span>{t('间隔单位')}</span><AutomationPicker value={schedule.unit} options={units} label={t('间隔单位')}
              onChange={(unit) => update('schedule', { ...schedule, unit })} optionLabel={(unit) => t(unitLabels[unit])} /></div>
          </> : <>
            <div className="automation-labeled-control"><span>{t('重复')}</span><AutomationPicker value={schedule.kind} options={repeatKinds} label={t('重复')}
              onChange={changeKind} optionLabel={(kind) => t(repeatLabels[kind])} /></div>
            {schedule.kind === 'once' && <div className="automation-labeled-control"><span>{t('执行日期')}</span><DatePicker value={schedule.date} label={t('执行日期')} onChange={(date) => update('schedule', { ...schedule, date })} /></div>}
            {schedule.kind === 'monthly' && <TextField label={t('每月日期')} name="monthDay" type="number" min={1} max={31} fieldSize="md" shape="standard"
              value={Number.isFinite(schedule.day) ? schedule.day : ''} onChange={(event) => update('schedule', { ...schedule, day: event.currentTarget.valueAsNumber })} />}
            <div className="automation-labeled-control"><span>{t('执行时间')}</span><TimePicker value={schedule.time} label={t('执行时间')} onChange={(time) => update('schedule', { ...schedule, time })} /></div>
          </>}
        </div>
        {schedule.kind === 'weekly' && <div className="automation-weekdays" role="group" aria-label={t('每周')}>
          {Array.from({ length: 7 }, (_, day) => <Button type="button" variant="ghost" key={day} selected={schedule.weekdays.includes(day)} aria-pressed={schedule.weekdays.includes(day)}
            onClick={() => update('schedule', { ...schedule, weekdays: schedule.weekdays.includes(day) ? schedule.weekdays.filter((value) => value !== day) : [...schedule.weekdays, day] })}>
            {formatDate(shiftDate('2026-09-07', day), locale, true)}</Button>)}
        </div>}
        <div className="automation-config-footer"><small>{t(schedule.kind === 'monthly' ? '当月无该日期时跳过' : '北京时间')}</small><Button type="button" variant="ghost" onClick={() => setPanel(null)}>{t('完成')}</Button></div>
      </fieldset>}
      {panel === 'validity' && <fieldset id={`${id}-validity`} className="automation-config-panel">
        <legend className="visually-hidden">{t('有效期')}</legend>
        <div className="automation-config-fields">
          <div className="automation-labeled-control"><span>{t('开始日期')}</span><DatePicker value={draft.startsOn} label={t('开始日期')} onChange={(date) => update('startsOn', date)} /></div>
          <div className="automation-labeled-control"><span>{t('结束日期')}</span><DatePicker value={draft.endsOn} label={t('结束日期')} onChange={(date) => update('endsOn', date)} /></div>
        </div>
        <div className="automation-config-footer"><Button type="button" variant="ghost" onClick={() => setDraft((current) => ({ ...current, startsOn: '', endsOn: '' }))}>{t('清除日期')}</Button><Button type="button" variant="ghost" onClick={() => setPanel(null)}>{t('完成')}</Button></div>
      </fieldset>}
      <details className="automation-advanced"><summary><span>{t('更多设置')}</span><ChevronDown size={14} aria-hidden="true" /></summary>
        <div className="automation-permission"><span>{t('访问权限')}</span><AccessModePicker value={draft.accessMode} onChange={value => update('accessMode', value)} disabled={saving || uncertain} /></div>
        <p className="automation-caption">{t('手动运行不受计划有效期限制；需要人工处理时自动化将结束')}</p>
      </details>
      </fieldset>
      <div className="automation-form-footer">
        <Button type="button" variant="ghost" disabled={saving} onClick={onClose}>{t('取消')}</Button><Button type="submit" variant="primary" loading={saving} disabled={uploading}>{t(task ? '保存修改' : '创建任务')}</Button>
      </div>
    </ValidatedForm>
  </Dialog>
}
