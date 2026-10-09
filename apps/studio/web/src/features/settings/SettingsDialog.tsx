import { Check, ChevronDown, Cpu, Link2, Monitor, MoonStar, Settings2, Sun, UserRound } from 'lucide-react'
import { useEffect, useId, useRef, useState } from 'react'

import type { AuthUser } from '../../api/auth/types'
import { Button, Dialog, ErrorBoundary, ListboxPicker, OverlayScrollbar } from '../../components/ui'
import { useI18n, type LanguagePreference } from '../../i18n'
import type { ThemePreference } from '../../theme'
import './settings.css'
import type { ToastKind } from '../../components/ui/ToastViewport'
import { ModelSettingsPanel } from './ModelSettingsPanel'
import { ServiceSettingsPanel } from './ServiceSettingsPanel'
import { useServiceSettings } from './useServiceSettings'
import { useUserProfile } from './useUserProfile'
import { UserProfileEditor } from './UserProfileEditor'

const APPEARANCE_OPTIONS = [
  { value: 'system', label: '跟随系统', icon: Monitor },
  { value: 'light', label: '浅色', icon: Sun },
  { value: 'dark', label: '深色', icon: MoonStar },
] as const satisfies ReadonlyArray<{
  value: ThemePreference
  label: string
  icon: typeof Monitor
}>

const LANGUAGE_OPTIONS = ['system', 'zh-CN', 'en'] as const satisfies readonly LanguagePreference[]

export interface SettingsDialogProps {
  open: boolean
  user: AuthUser
  themePreference: ThemePreference
  restoreFocusTo?: HTMLElement | null
  onThemePreferenceChange: (preference: ThemePreference) => void
  onModelsChanged?: () => void
  onToast: (kind: ToastKind, message: string) => void
  onClose: () => void
}

export function SettingsDialog({
  open,
  user,
  themePreference,
  restoreFocusTo,
  onThemePreferenceChange,
  onClose,
  onModelsChanged,
  onToast,
}: SettingsDialogProps) {
  const appearanceName = useId()
  const contentRef = useRef<HTMLDivElement>(null)
  const [activeSection, setActiveSection] = useState<'user' | 'general' | 'models' | 'services'>('user')
  const [servicesVisited, setServicesVisited] = useState(false)
  const [confirmClose, setConfirmClose] = useState(false)
  const [confirmProfileClose, setConfirmProfileClose] = useState(false)
  const profile = useUserProfile(user, open)
  useEffect(() => { if (!open || !profile.dirty) setConfirmProfileClose(false) }, [open, profile.dirty])
  const services = useServiceSettings(open && servicesVisited)
  const [languageOpen, setLanguageOpen] = useState(false)
  const { preference: languagePreference, setPreference: setLanguagePreference, t } = useI18n()
  const languageLabel = (value: LanguagePreference) => {
    if (value === 'system') return t('跟随系统')
    return value === 'zh-CN' ? t('简体中文') : 'English'
  }
  const closeDisabled = profile.saving || services.saving !== null
  const requestClose = (discard?: 'profile' | 'services') => {
    if (closeDisabled) return
    setConfirmProfileClose(false); setConfirmClose(false)
    if (discard === 'profile') profile.reset()
    if (discard === 'services') { services.reset('web_search'); services.reset('image_generation') }
    if (discard !== 'profile' && profile.dirty) { setActiveSection('user'); setConfirmProfileClose(true); return }
    if (discard !== 'services' && servicesVisited && services.dirty) { setActiveSection('services'); setConfirmClose(true); return }
    onClose()
  }

  return (
    <Dialog
      open={open}
      title={t('设置')}
      className="settings-dialog"
      closeDisabled={closeDisabled}
      restoreFocusTo={restoreFocusTo}
      onClose={() => requestClose()}
    >
      <div className="settings-layout">
        <nav className="settings-nav" aria-label={t('设置分类')}>
          <button
            type="button"
            className={`settings-nav__item${activeSection === 'user' ? ' is-selected' : ''}`}
            aria-current={activeSection === 'user' ? 'page' : undefined}
            onClick={() => setActiveSection('user')}
          >
            <UserRound size={18} aria-hidden="true" />
            {t('账号管理')}
          </button>
          <button
            type="button"
            className={`settings-nav__item${activeSection === 'general' ? ' is-selected' : ''}`}
            aria-current={activeSection === 'general' ? 'page' : undefined}
            onClick={() => setActiveSection('general')}
          >
            <Settings2 size={18} aria-hidden="true" />
            {t('通用')}
          </button>
          <button type="button" className={`settings-nav__item${activeSection === 'models' ? ' is-selected' : ''}`} aria-current={activeSection === 'models' ? 'page' : undefined} onClick={() => setActiveSection('models')}><Cpu size={18} aria-hidden="true" />{t('模型配置')}</button>
          <button type="button" className={`settings-nav__item${activeSection === 'services' ? ' is-selected' : ''}`} aria-current={activeSection === 'services' ? 'page' : undefined} onClick={() => { setServicesVisited(true); setActiveSection('services') }}><Link2 size={18} aria-hidden="true" />{t('服务连接')}</button>
        </nav>
        {activeSection === 'services' ? <div className="settings-models-host">
          <ErrorBoundary fallback={({ reset }) => <Button type="button" onClick={reset}>{t('重新加载服务配置')}</Button>}><ServiceSettingsPanel state={services} confirmClose={confirmClose} closeDisabled={closeDisabled} onCloseDecision={discard => { if (discard) requestClose('services'); else setConfirmClose(false) }} /></ErrorBoundary>
        </div> : activeSection === 'models' ? <div className="settings-models-host">
          <ErrorBoundary onError={() => onToast('error', t('模型加载失败，请先重试'))} fallback={({ reset }) => <Button type="button" onClick={reset}>{t('重新加载模型')}</Button>}><ModelSettingsPanel onChanged={onModelsChanged} /></ErrorBoundary>
        </div> : <div
          ref={contentRef}
          className="settings-content ui-scrollbar"
          role="region"
          aria-label={t('设置')}
          tabIndex={0}
        >
          {activeSection === 'user' && <section className="settings-section" aria-labelledby="settings-account-title">
            <h3 id="settings-account-title">{t('用户信息')}</h3>
            <UserProfileEditor username={user.username} profile={profile} confirmClose={confirmProfileClose && profile.dirty}
              closeDisabled={closeDisabled}
              onCloseDecision={discard => {
                if (discard) requestClose('profile')
                else setConfirmProfileClose(false)
              }} />
          </section>}
          {activeSection === 'general' && <>
          <section className="settings-section" aria-labelledby="settings-appearance-title">
            <div className="settings-section__heading">
              <h3 id="settings-appearance-title">{t('外观')}</h3>
              <p>{t('选择 TinkerFin 在当前浏览器中的显示主题')}</p>
            </div>
            <fieldset className="settings-theme-options">
              <legend className="visually-hidden">{t('外观主题')}</legend>
              {APPEARANCE_OPTIONS.map(option => {
                const OptionIcon = option.icon
                return <label key={option.value} className={`settings-theme-option${themePreference === option.value ? ' is-selected' : ''}`}>
                  <input type="radio" name={appearanceName} value={option.value} checked={themePreference === option.value} onChange={() => onThemePreferenceChange(option.value)} />
                  <OptionIcon size={18} aria-hidden="true" /><span>{t(option.label)}</span>
                </label>
              })}
            </fieldset>
          </section>
          <section className="settings-section" aria-labelledby="settings-choice-title">
            <div className="settings-section__heading">
              <h3 id="settings-choice-title">{t('语言')}</h3>
              <p>{t('选择 TinkerFin 在当前浏览器中的界面语言')}</p>
            </div>
            <ListboxPicker
              value={languagePreference}
              options={LANGUAGE_OPTIONS}
              open={languageOpen}
              onOpenChange={setLanguageOpen}
              onChange={setLanguagePreference}
              triggerLabel={t('界面语言')}
              listboxLabel={t('界面语言')}
              rootClassName="settings-choice-picker settings-preference-picker"
              triggerClassName="settings-choice-trigger"
              listboxClassName="settings-choice-options"
              optionClassName="settings-choice-option"
              renderTrigger={(value) => (
                <>
                  <span>{languageLabel(value)}</span>
                  <ChevronDown size={16} aria-hidden="true" />
                </>
              )}
              renderOption={(option, selected) => (
                <>
                  <span>{languageLabel(option)}</span>
                  <span className="settings-choice-check" aria-hidden="true">
                    {selected && <Check size={17} />}
                  </span>
                </>
              )}
            />
          </section>
          </>}
        </div>}
        {activeSection !== 'models' && activeSection !== 'services' && <OverlayScrollbar viewportRef={contentRef} />}
      </div>
    </Dialog>
  )
}
