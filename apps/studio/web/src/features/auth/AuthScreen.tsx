import { ArrowRight, ChevronDown, Eye, EyeOff, SlidersHorizontal } from 'lucide-react'
import { useRef, useState } from 'react'
import type { FormEvent } from 'react'

import { BrandLogo, Button, IconButton, TextField } from '../../components/ui'
import { ThemePicker } from '../../components/ui/ThemePicker'
import { ValidatedForm } from '../../components/ui/ValidatedForm'
import { DEFAULT_SERVER_ADDRESS } from '../../api/shared/config'
import { useI18n } from '../../i18n'
import { useLoginMotion } from './useLoginMotion'
import { useServerAddress } from './useServerAddress'
import './auth.css'

interface Credentials { username: string; password: string }
interface AuthValidationErrors { loginUsername?: string; loginPassword?: string }
interface AuthScreenProps {
  onLogin: (credentials: Credentials) => Promise<void> | void
  onError: (message: string) => void
  pending?: boolean
}

export function AuthScreen({ onLogin, onError, pending = false }: AuthScreenProps) {
  const { t } = useI18n()
  const rootRef = useRef<HTMLElement>(null)
  const serverDetails = useRef<HTMLDetailsElement>(null)
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [isPasswordVisible, setPasswordVisible] = useState(false)
  const [validationErrors, setValidationErrors] = useState<AuthValidationErrors>({})
  const [validationAttempt, setValidationAttempt] = useState(0)
  const server = useServerAddress(onError)
  useLoginMotion(rootRef)

  const submitLogin = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (pending) return
    const errors = {
      loginUsername: username.trim() ? undefined : t('请输入用户名'),
      loginPassword: password ? undefined : t('请输入密码'),
    }
    setValidationErrors(errors)
    const serverValid = server.validate()
    if (!serverValid && serverDetails.current) serverDetails.current.open = true
    if (Object.values(errors).some(Boolean) || !serverValid) {
      setValidationAttempt((current) => current + 1)
      return
    }
    await onLogin({ username: username.trim(), password })
  }

  return (
    <main id="main-content" ref={rootRef} className="auth-page" aria-label={t('TinkerFin 账户登录')}>
      <div className="auth-ambient" aria-hidden="true">
        <div className="auth-ambient-art">
          <img className="auth-ribbon auth-ribbon--blue" src="/auth/ribbon-blue.svg" alt="" draggable={false} />
          <img className="auth-ribbon auth-ribbon--violet" src="/auth/ribbon-violet.svg" alt="" draggable={false} />
        </div>
        <span className="auth-ambient-mist" />
      </div>
      <header className="auth-header">
        <div className="auth-brand" aria-label="TinkerFin"><BrandLogo size="md" /></div>
        <div className="auth-header-actions">
          <ThemePicker />
          <a className="auth-github" href="https://github.com/tinkerfin-ai/tinkerfin-harness" target="_blank" rel="noopener noreferrer" aria-label={t('在 GitHub 查看 TinkerFin（新标签页）')} title="GitHub">
            <img src="/auth/github-mark.svg" width="18" height="18" alt="" />
          </a>
        </div>
      </header>
      <section className="auth-panel" aria-labelledby="login-heading">
        <h1 id="login-heading">{t('欢迎回来')}</h1>
        <ValidatedForm className="auth-form" errors={{ ...validationErrors, serverAddress: server.error }} validationAttempt={validationAttempt} onSubmit={submitLogin}>
          <TextField rootClassName="auth-field" id="login-username" name="loginUsername" label={t('用户名')} error={validationErrors.loginUsername}
            autoComplete="username" autoCapitalize="none" spellCheck={false} value={username} placeholder={t('输入用户名')} disabled={pending} required
            onChange={(event) => { setUsername(event.target.value); setValidationErrors((current) => ({ ...current, loginUsername: undefined })) }} />
          <TextField rootClassName="auth-field" id="login-password" name="loginPassword" label={t('密码')} error={validationErrors.loginPassword}
            type={isPasswordVisible ? 'text' : 'password'} autoComplete="current-password" value={password} placeholder={t('输入密码')} disabled={pending} required
            onChange={(event) => { setPassword(event.target.value); setValidationErrors((current) => ({ ...current, loginPassword: undefined })) }}
            trailingContent={<IconButton className="auth-password-toggle" size="lg" variant="ghost" label={isPasswordVisible ? t('隐藏密码') : t('显示密码')} aria-pressed={isPasswordVisible} disabled={pending} icon={isPasswordVisible ? <EyeOff size={17} /> : <Eye size={17} />} onClick={() => setPasswordVisible((current) => !current)} />} />
          <Button className="auth-primary" type="submit" variant="primary" size="xl" loading={pending} trailingIcon={!pending ? <ArrowRight size={17} /> : undefined}>
            {pending ? t('登录中…') : t('登录')}
          </Button>
          <details ref={serverDetails} className="auth-server" open>
            <summary><SlidersHorizontal size={16} /><span>{t('服务器地址')}</span><ChevronDown size={16} /></summary>
            <TextField rootClassName="auth-field auth-server-field" id="server-address" name="serverAddress" label={t('服务器地址')} error={server.error}
              type="url" autoComplete="url" autoCapitalize="none" spellCheck={false} value={server.draft} placeholder={DEFAULT_SERVER_ADDRESS} disabled={pending}
              onChange={(event) => server.change(event.target.value)} onBlur={() => { server.validate() }} />
          </details>
        </ValidatedForm>
      </section>
    </main>
  )
}
