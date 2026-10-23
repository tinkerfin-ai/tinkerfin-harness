import { Check, CircleAlert, LogOut, Pencil, RotateCw } from 'lucide-react'
import { useLayoutEffect, useRef, useState } from 'react'
import { Button } from '../components/ui/Button'
import { IconButton } from '../components/ui/IconButton'
import { TextField } from '../components/ui/TextField'
import { Tooltip } from '../components/ui/Tooltip'
import { ValidatedForm } from '../components/ui/ValidatedForm'
import { send } from '../bridge'
import { useI18n } from '../i18n'
import type { DesktopState, ViewState } from '../state'
import { LanguagePicker } from './LanguagePicker'
import { useAction } from './useAction'

interface Props { state: DesktopState; view: ViewState; change: (patch: Partial<ViewState>) => void }

export function Account({ state, view, change }: Props) {
  const { t } = useI18n()
  const nameAction = useAction(), logout = useAction()
  const [error, setError] = useState('')
  const [attempt, setAttempt] = useState(0)
  const nameInput = useRef<HTMLInputElement>(null)
  const editButton = useRef<HTMLButtonElement>(null)
  const wasEditing = useRef(false)
  const composing = useRef(false)
  const busy = logout.pending || state.busy === 'logout'
  const saving = nameAction.pending || state.busy === 'name'
  useLayoutEffect(() => {
    if (view.page !== 'account' || !state.connected) { wasEditing.current = false; return }
    if (view.editingName) nameInput.current?.focus()
    else if (wasEditing.current) editButton.current?.focus()
    wasEditing.current = view.editingName
  }, [view.page, view.editingName, state.connected])
  const save = () => {
    if (!view.name.trim() || Array.from(view.name.trim()).length > 128) { setError('请输入设备名称'); setAttempt(value => value + 1); return }
    void nameAction.run({ type: 'save-name', name: view.name.trim() }, result => {
      if (result.ok) { change({ editingName: false, name: view.name.trim() }); setError('') }
      else { setError('保存失败，请重试'); setAttempt(value => value + 1) }
    })
  }
  return <>
    <main className="client-body" aria-label={t('我的')}><div className="settings-list">
      <div className="settings-row account-row"><span className="row-label">{t('账号')}</span><span className="row-value truncate" title={state.account}>{state.account}</span></div>
      <div className="settings-row"><span className="row-label">{t('设备名称')}</span><div className="row-value">
        {view.editingName && !busy ? <ValidatedForm errors={{ name: error }} validationAttempt={attempt} className="name-editor" onSubmit={event => { event.preventDefault(); if (!composing.current) save() }} onKeyDown={event => {
          if (event.nativeEvent.isComposing) return
          if (event.key === 'Escape' && !saving) { event.preventDefault(); event.stopPropagation(); change({ name: state.name, editingName: false }); setError('') }
        }}>
          <TextField ref={nameInput} id="device-name" name="name" label={t('设备名称')} value={view.name} onChange={event => { change({ name: event.target.value }); setError('') }}
            onCompositionStart={() => { composing.current = true }} onCompositionEnd={() => { composing.current = false }}
            maxLength={128} disabled={saving} error={error && t(error)} rootClassName="desktop-name-field" fieldSize="md" shape="standard"
            trailingContent={error && <Tooltip content={t(error)} touchToggle><button type="button" className="name-error-icon" aria-label={t(error)}><CircleAlert size={14} aria-hidden="true" /></button></Tooltip>} />
          <IconButton type="submit" label={t('保存设备名称')} tooltip={t('保存设备名称')} icon={<Check size={16} />} variant="ghost" loading={saving} />
        </ValidatedForm> : <div className="name-display"><span className="truncate" title={state.name}>{state.name}</span><IconButton ref={editButton} label={t('编辑设备名称')} tooltip={t('编辑设备名称')} icon={<Pencil size={16} />} variant="ghost" disabled={busy || saving} onClick={() => { change({ name: state.name, editingName: true }); setError('') }} /></div>}
      </div></div>
      <div className="settings-row language-row"><span className="row-label">{t('语言')}</span><LanguagePicker value={state.language} disabled={busy || !state.connected || view.page !== 'account' || state.update.phase !== 'idle'} /></div>
      <div className="settings-row version-row"><span className="row-label">{t('当前版本')}</span><div className="row-value version-value"><span className="app-version">{state.version}</span><IconButton label={t('检查更新')} tooltip={t('检查更新')} icon={<RotateCw size={16} />} variant="ghost" disabled={busy || saving} onClick={() => void send({ type: 'check-update', view })} /></div></div>
    </div></main>
    <footer className="account-footer"><Button variant="ghost" leadingIcon={<LogOut size={16} />} loading={busy} disabled={saving} onClick={() => void logout.run({ type: 'logout' }, result => {
      if (result.ok) {
        change({ page: 'connection', connectionForm: true, editingName: false, name: state.name, service: state.service, api: state.api === state.service ? '' : state.api, manualAPI: state.api !== state.service })
        setError('')
      }
    })}>{t('退出登录')}</Button></footer>
  </>
}
