import { useEffect, useRef } from 'react'

import { Button, UserAvatar } from '../../components/ui'
import { useI18n } from '../../i18n'
import type { useUserProfile } from './useUserProfile'

interface UserProfileEditorProps {
  username: string
  profile: ReturnType<typeof useUserProfile>
  confirmClose: boolean
  closeDisabled?: boolean
  onCloseDecision: (discard: boolean) => void
}

export function UserProfileEditor({ username, profile, confirmClose, closeDisabled = false, onCloseDecision }: UserProfileEditorProps) {
  const { t } = useI18n()
  const input = useRef<HTMLInputElement>(null)
  const chooseButton = useRef<HTMLButtonElement>(null)
  const continueButton = useRef<HTMLButtonElement>(null)
  const wasDirty = useRef(profile.dirty)
  useEffect(() => {
    if (wasDirty.current && !profile.dirty) chooseButton.current?.focus()
    wasDirty.current = profile.dirty
  }, [profile.dirty])
  useEffect(() => { if (confirmClose) continueButton.current?.focus() }, [confirmClose])
  const reset = () => { profile.reset(); chooseButton.current?.focus() }
  return <div className="settings-profile-editor">
    <div className="settings-profile">
      <UserAvatar avatarUrl={profile.avatarUrl} size="lg" />
      <div className="settings-profile__identity"><span>{t('用户名')}</span><strong>{username}</strong></div>
      <Button ref={chooseButton} type="button" size="sm" className="settings-profile__choose"
        disabled={profile.saving} loading={profile.selecting} onClick={() => input.current?.click()}>{t('更换头像')}</Button>
      <input ref={input} type="file" hidden accept="image/png,image/jpeg,image/webp,image/gif" aria-label={t('选择头像图片')}
        onChange={event => { const file = event.currentTarget.files?.[0]; event.currentTarget.value = ''; if (file) void profile.choose(file) }} />
    </div>
    {profile.error && <p className="settings-profile-editor__error" role="alert">{profile.error}</p>}
    {profile.dirty ? <div className="settings-profile-editor__actions">
      <Button type="button" variant="ghost" size="sm" disabled={profile.saving} onClick={reset}>{t('取消')}</Button>
      <Button type="button" variant="solid" size="sm" loading={profile.saving} disabled={profile.selecting}
        onClick={() => void profile.save()}>{t(profile.saving ? '保存中' : '保存')}</Button>
    </div> : null}
    {profile.notice && <p className="settings-profile-editor__notice" role="status">{profile.notice}</p>}
    {confirmClose && <div className="settings-profile-editor__confirm" role="group" aria-label={t('头像尚未保存')}>
      <span>{t('头像尚未保存')}</span>
      <Button ref={continueButton} type="button" size="sm" disabled={profile.saving || closeDisabled} onClick={() => { onCloseDecision(false); chooseButton.current?.focus() }}>{t('继续编辑')}</Button>
      <Button type="button" size="sm" variant="ghost" disabled={profile.saving || closeDisabled} onClick={() => onCloseDecision(true)}>{t('放弃更改')}</Button>
    </div>}
  </div>
}
