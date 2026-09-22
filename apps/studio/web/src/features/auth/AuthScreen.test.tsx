import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { DEFAULT_SERVER_ADDRESS, getServerAddress, setServerAddress } from '../../api/shared/config'
import { AuthScreen } from './AuthScreen'

const renderLogin = (options = {}) => {
  const onLogin = vi.fn()
  const onError = vi.fn()
  render(<AuthScreen onLogin={onLogin} onError={onError} {...options} />)
  return { onLogin, onError }
}

describe('登录页', () => {
  it('shows the core login controls with the server address expanded and default as placeholder', () => {
    renderLogin()
    expect(screen.getByRole('heading', { name: '欢迎回来' })).toBeInTheDocument()
    const server = screen.getByLabelText('服务器地址')
    expect(server).toBeVisible()
    expect(server).toHaveValue('')
    expect(server).toHaveAttribute('placeholder', DEFAULT_SERVER_ADDRESS)
    expect(screen.getByRole('link', { name: '在 GitHub 查看 TinkerFin（新标签页）' })).toHaveAttribute('href', 'https://github.com/tinkerfin-ai/tinkerfin-harness')
  })

  it('marks invalid credentials and focuses the first field without submitting', async () => {
    const user = userEvent.setup()
    const { onLogin } = renderLogin()
    await user.click(screen.getByRole('button', { name: '登录' }))
    expect(onLogin).not.toHaveBeenCalled()
    expect(screen.getByLabelText('用户名')).toHaveAttribute('aria-invalid', 'true')
    expect(screen.getByLabelText('密码')).toHaveAttribute('aria-invalid', 'true')
    expect(screen.getByLabelText('用户名')).toHaveAccessibleDescription('请输入用户名')
    expect(screen.getByLabelText('用户名')).toHaveFocus()
    await user.type(screen.getByLabelText('用户名'), 'tinkerfin')
    expect(screen.getByLabelText('用户名')).toHaveAttribute('aria-invalid', 'false')
  })

  it('submits trimmed username and unchanged password through the real login callback', async () => {
    const user = userEvent.setup()
    const { onLogin } = renderLogin()
    await user.type(screen.getByLabelText('用户名'), ' user ')
    await user.type(screen.getByLabelText('密码'), ' p ')
    await user.click(screen.getByRole('button', { name: '显示密码' }))
    expect(screen.getByLabelText('密码')).toHaveAttribute('type', 'text')
    await user.click(screen.getByRole('button', { name: '隐藏密码' }))
    await user.click(screen.getByRole('button', { name: '登录' }))
    expect(onLogin).toHaveBeenCalledExactlyOnceWith({ username: 'user', password: ' p ' })
  })

  it('加载已保存地址，并自动保存输入的有效地址', async () => {
    setServerAddress('https://server.example/studio')
    const user = userEvent.setup()
    renderLogin()
    const address = screen.getByLabelText('服务器地址')
    expect(address).toHaveValue('https://server.example/studio')
    await user.clear(address)
    expect(getServerAddress()).toBe(DEFAULT_SERVER_ADDRESS)
    await user.type(address, 'https://another.example/service')
    await user.tab()
    expect(getServerAddress()).toBe('https://another.example/service')
  })

  it('blocks login for an invalid address and keeps its last saved value', async () => {
    const user = userEvent.setup()
    const { onLogin } = renderLogin()
    await user.type(screen.getByLabelText('用户名'), 'user')
    await user.type(screen.getByLabelText('密码'), 'secret')
    await user.type(screen.getByLabelText('服务器地址'), 'invalid')
    await user.click(screen.getByRole('button', { name: '登录' }))
    expect(screen.getByLabelText('服务器地址')).toHaveAttribute('aria-invalid', 'true')
    expect(getServerAddress()).toBe(DEFAULT_SERVER_ADDRESS)
    expect(onLogin).not.toHaveBeenCalled()
  })

  it('reports storage failure through the global feedback callback', async () => {
    const user = userEvent.setup()
    const { onError } = renderLogin()
    const spy = vi.spyOn(localStorage, 'setItem').mockImplementation(() => { throw new Error('blocked') })
    try {
      await user.type(screen.getByLabelText('服务器地址'), 'https://server.example')
      expect(onError).toHaveBeenCalledWith('浏览器无法保存服务器地址，请检查存储设置后重试')
    } finally { spy.mockRestore() }
  })

  it('disables all editable login controls while the request is pending', () => {
    renderLogin({ pending: true })
    for (const name of ['用户名', '密码', '服务器地址']) expect(screen.getByLabelText(name)).toBeDisabled()
    expect(screen.getByRole('button', { name: '显示密码' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '登录中…' })).toBeDisabled()
  })
})
