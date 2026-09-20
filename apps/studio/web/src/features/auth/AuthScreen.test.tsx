import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import uiStyles from '../../components/ui/ui.css?raw'
import { AuthScreen } from './AuthScreen'
import authStyles from './auth.css?raw'

describe('AuthScreen', () => {
  it('submits only the real username and password login to the caller', async () => {
    const user = userEvent.setup()
    const onLogin = vi.fn()
    render(<AuthScreen onLogin={onLogin} />)

    await user.type(screen.getByLabelText('用户名'), '  yunsan  ')
    await user.type(screen.getByLabelText('密码'), 'secret')
    await user.click(screen.getByRole('button', { name: '登录' }))

    expect(onLogin).toHaveBeenCalledWith({ username: 'yunsan', password: 'secret' })
  })

  it('associates the login password with exactly one label', () => {
    render(<AuthScreen onLogin={vi.fn()} />)

    const passwordInput = screen.getByLabelText('密码')
    expect(document.querySelectorAll('label[for="login-password"]')).toHaveLength(1)
    expect(passwordInput.closest('label')).toBeNull()
  })

  it('uses the shared TextField contract with semibold labels and regular input text', () => {
    render(<AuthScreen onLogin={vi.fn()} />)

    const usernameInput = screen.getByLabelText('用户名')
    expect(usernameInput.closest('.ui-text-field')).toHaveClass('ui-text-field--capsule')
    expect(uiStyles).toMatch(/\.ui-text-field__label\s*{[^}]*font-weight:\s*var\(--weight-semibold\)/s)
    expect(uiStyles).toMatch(/\.ui-text-field__control input\s*{[^}]*font-weight:\s*var\(--weight-regular\)/s)
  })

  it('moves through login credentials and inactive actions in document order', async () => {
    const user = userEvent.setup()
    render(<AuthScreen onLogin={vi.fn()} />)

    screen.getByLabelText('用户名').focus()
    await user.tab()
    expect(screen.getByLabelText('密码')).toHaveFocus()
    await user.tab()
    expect(screen.getByRole('button', { name: '显示密码' })).toHaveFocus()
    await user.tab()
    expect(screen.getByRole('button', { name: '忘记密码？' })).toHaveFocus()
    await user.tab()
    expect(screen.getByRole('button', { name: '登录' })).toHaveFocus()
  })

  it('accepts existing short passwords without registration constraints', () => {
    render(<AuthScreen onLogin={vi.fn()} />)

    expect(screen.getByLabelText('密码')).not.toHaveAttribute('minlength')
    expect(screen.queryByLabelText('设置密码')).not.toBeInTheDocument()
  })

  it('replaces native login validation bubbles with inline field feedback', async () => {
    const user = userEvent.setup()
    render(<AuthScreen onLogin={vi.fn()} />)

    const usernameInput = screen.getByLabelText('用户名')
    const passwordInput = screen.getByLabelText('密码')
    const loginForm = screen.getByRole('button', { name: '登录' }).closest('form')

    expect(loginForm).toHaveAttribute('novalidate')
    await user.click(screen.getByRole('button', { name: '登录' }))

    expect(usernameInput).toHaveAttribute('aria-invalid', 'true')
    expect(passwordInput).toHaveAttribute('aria-invalid', 'true')
    expect(usernameInput).toHaveAccessibleDescription('请输入用户名')
    expect(passwordInput).toHaveAccessibleDescription('请输入密码')
    expect(usernameInput).toHaveFocus()

    await user.type(usernameInput, 'yunsan')
    expect(usernameInput).toHaveAttribute('aria-invalid', 'false')
    expect(screen.queryByText('请输入用户名')).not.toBeInTheDocument()
  })

  it('keeps short viewport forms inside a definite scroll container', () => {
    expect(authStyles).toMatch(/\.auth-page\s*{[^}]*min-height:\s*100dvh;[^}]*overflow:\s*hidden;/s)
    expect(authStyles).toMatch(/@media \(max-width:\s*1023px\)[\s\S]*\.auth-page\s*{[^}]*overflow-y:\s*auto;/s)
  })

  it('keeps the animated blue-violet ambient inside the desktop introduction pane', () => {
    render(<AuthScreen onLogin={vi.fn()} />)

    const ambient = document.querySelector('.auth-ambient')
    expect(ambient).toHaveAttribute('aria-hidden', 'true')
    expect(ambient?.querySelector('.auth-ambient-art')).not.toBeNull()
    expect(ambient?.querySelectorAll('.auth-ribbon')).toHaveLength(2)
    expect(ambient?.querySelector('.auth-ribbon__flow')).not.toBeInTheDocument()
    expect(ambient?.querySelector('.auth-ambient-mist')).not.toBeNull()
    expect(document.querySelector('.auth-signal-field')).not.toBeInTheDocument()
    expect(authStyles).toMatch(/\.auth-ambient\s*{[^}]*inset:\s*0 46% 0 0;[^}]*overflow:\s*hidden;[^}]*isolation:\s*isolate;/s)
    expect(authStyles).toMatch(/\.auth-ribbon\s*{[^}]*transform-box:\s*fill-box;[^}]*will-change:\s*transform;/s)
    expect(authStyles).not.toContain('.auth-ribbon__flow')
    expect(authStyles).toMatch(/@media \(max-width:\s*1023px\)[\s\S]*\.auth-ambient,[\s\S]*\.auth-intro\s*{[^}]*display:\s*none;/s)
    expect(authStyles).toMatch(/@media \(prefers-reduced-motion:\s*reduce\)[\s\S]*\.auth-ribbon,[\s\S]*\.auth-ambient-mist\s*{[^}]*will-change:\s*auto;/s)
  })

  it('uses the shared outlined field and lets the auth form own only its transparent surface', () => {
    render(<AuthScreen onLogin={vi.fn()} />)

    expect(screen.getByLabelText('密码').closest('.ui-text-field__control')).not.toBeNull()
    expect(uiStyles).toMatch(/\.ui-text-field__control,\s*\.ui-temporal-picker__trigger\s*{[^}]*border:\s*1px solid var\(--color-border\);[^}]*background:\s*var\(--color-layer-1\);/s)
    expect(uiStyles).toMatch(/\.ui-text-field__control:focus-within,\s*\.ui-temporal-picker__trigger:focus-visible\s*{[^}]*border-color:\s*var\(--color-border-strong\);[^}]*box-shadow:\s*none;/s)
    expect(authStyles).toMatch(/\.auth-field \.ui-text-field__control\s*{[^}]*background:\s*transparent;/s)
    expect(authStyles).not.toContain('.auth-field .ui-text-field__control:focus-within')
  })

  it('keeps auth autofill and the header theme surface transparent', () => {
    expect(authStyles).toMatch(/input:-webkit-autofill[\s\S]*-webkit-background-clip:\s*text;/s)
    expect(authStyles).toMatch(/input:-webkit-autofill[\s\S]*-webkit-text-fill-color:\s*var\(--color-text-primary\);/s)
    expect(authStyles).toMatch(/\.auth-header \.theme-switcher-circle,[\s\S]*\.auth-header \.theme-switcher-surface\s*{[^}]*border-color:\s*transparent;[^}]*background:\s*transparent;/s)
  })

  it('keeps auth buttons and text-button hover states shadowless', () => {
    expect(authStyles).toMatch(/\.auth-header \.theme-switcher-visual,[\s\S]*\.auth-page \.ui-button\s*{[^}]*box-shadow:\s*none;/s)
    expect(authStyles).toMatch(/\.auth-page \.ui-button--text:hover:not\(:disabled\),[\s\S]*\.auth-page \.ui-button--text:active:not\(:disabled\)\s*{[^}]*background:\s*transparent;[^}]*box-shadow:\s*none;/s)
  })

  it('renders the static brand asset without interactive hover states', () => {
    render(<AuthScreen onLogin={vi.fn()} />)

    expect(screen.queryByRole('button', { name: 'TinkerFin' })).not.toBeInTheDocument()
    const brand = document.querySelector('.auth-brand')
    expect(brand?.querySelector('.brand-logo')).toHaveClass('brand-logo--md')
    expect(brand?.querySelector('.brand-logo__mark')).toHaveAttribute('alt', '')
    expect(brand?.querySelector('.brand-logo__wordmark')).toHaveAttribute('alt', '')
    expect(brand).toHaveTextContent('')
    expect(authStyles).not.toMatch(/\.auth-brand:(hover|active|focus-visible|disabled)/)
  })

  it.each(['忘记密码？', '免费注册'])('keeps %s clickable without exposing a legacy form', async (name) => {
    const user = userEvent.setup()
    const onLogin = vi.fn()
    render(<AuthScreen onLogin={onLogin} />)

    await user.click(screen.getByRole('button', { name }))

    expect(screen.getByRole('heading', { name: '欢迎回来' })).toBeInTheDocument()
    expect(screen.queryByLabelText('邮箱地址')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('设置密码')).not.toBeInTheDocument()
    expect(onLogin).not.toHaveBeenCalled()
  })

  it.each(['Google', 'Microsoft', 'Apple'])('keeps the inactive %s login button without side effects', async (provider) => {
    const user = userEvent.setup()
    const onLogin = vi.fn()
    render(<AuthScreen onLogin={onLogin} />)

    await user.click(screen.getByRole('button', { name: `使用 ${provider} 登录` }))

    expect(screen.getByRole('heading', { name: '欢迎回来' })).toBeInTheDocument()
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    expect(onLogin).not.toHaveBeenCalled()
  })

  it('renders the official artwork for every third-party sign-in provider', () => {
    render(<AuthScreen onLogin={vi.fn()} />)

    for (const provider of ['Google', 'Microsoft', 'Apple']) {
      const button = screen.getByRole('button', { name: `使用 ${provider} 登录` })
      expect(button.querySelector(`[data-brand-logo="${provider.toLowerCase()}"]`)).not.toBeNull()
      expect(button.querySelector('svg.lucide')).toBeNull()
    }

    const appleBackground = screen
      .getByRole('button', { name: '使用 Apple 登录' })
      .querySelector('.auth-provider-apple-background')
    expect(appleBackground).not.toBeNull()
    expect(authStyles).toMatch(/\.auth-provider-apple-background\s*{\s*fill:\s*transparent;/)
  })

  it('renders unavailable legal destinations as visible non-interactive text', () => {
    render(<AuthScreen onLogin={vi.fn()} />)

    expect(screen.getByText('服务条款').tagName).toBe('SPAN')
    expect(screen.getByText('隐私政策').tagName).toBe('SPAN')
  })
})
