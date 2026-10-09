import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { SettingsDialog } from './SettingsDialog'
import { LocaleProvider } from '../../i18n'
import { saveAvatar } from '../../api/auth/client'
import { requestJson } from '../../api/shared/http'
import { emptyServices } from './serviceSettings'

vi.mock('../../api/auth/client', () => ({ saveAvatar: vi.fn() }))
vi.mock('../../api/shared/http', async importOriginal => ({ ...await importOriginal<typeof import('../../api/shared/http')>(), requestJson: vi.fn() }))
afterEach(() => { vi.unstubAllGlobals(); vi.mocked(saveAvatar).mockReset(); vi.mocked(requestJson).mockReset() })

const user = {
  user_id: 7,
  username: 'yunsan',
  avatar_url: 'https://cdn.example.test/avatar.webp',
  roles: [],
  disabled: false,
}

describe('SettingsDialog', () => {
  it.each([true, false])('服务放弃确认保留另一区的头像草稿或保存：saving=%s', async saving => {
    vi.mocked(requestJson).mockResolvedValue(emptyServices())
    vi.stubGlobal('URL', class extends URL {
      static createObjectURL = () => 'blob:profile'
      static revokeObjectURL = vi.fn()
    })
    vi.stubGlobal('Image', class { src = ''; decode = () => Promise.resolve() })
    let finish: (value: null) => void = () => undefined
    vi.mocked(saveAvatar).mockImplementation(() => new Promise(resolve => { finish = resolve }))
    const onClose = vi.fn()
    const view = render(<LocaleProvider><SettingsDialog open user={user} themePreference="light"
      onThemePreferenceChange={vi.fn()} onToast={vi.fn()} onClose={onClose} /></LocaleProvider>)
    try {
      fireEvent.click(screen.getByRole('button', { name: '服务连接' }))
      fireEvent.change(await screen.findByLabelText('API Key'), { target: { value: 'draft-key' } })
      fireEvent.click(screen.getByRole('button', { name: '关闭对话框' }))
      expect(screen.getByRole('button', { name: '放弃修改并关闭' })).toBeInTheDocument()
      fireEvent.click(screen.getByRole('button', { name: '账号管理' }))
      fireEvent.change(screen.getByLabelText('选择头像图片'), { target: { files: [new File(['photo'], 'avatar.png', { type: 'image/png' })] } })
      const save = await screen.findByRole('button', { name: '保存' })
      if (saving) fireEvent.click(save)
      fireEvent.click(screen.getByRole('button', { name: '服务连接' }))
      const discard = screen.getByRole('button', { name: '放弃修改并关闭' })
      if (saving) expect(discard).toBeDisabled()
      fireEvent.click(discard)
      expect(onClose).not.toHaveBeenCalled()
      if (saving) expect(vi.mocked(saveAvatar).mock.calls[0][1].aborted).toBe(false)
      else {
        expect(screen.getByRole('group', { name: '头像尚未保存' })).toBeVisible()
        expect(URL.revokeObjectURL).not.toHaveBeenCalled()
        fireEvent.click(screen.getByRole('button', { name: '放弃更改' }))
        expect(onClose).toHaveBeenCalledOnce()
      }
    } finally {
      view.unmount()
      finish(null)
    }
  })

  it('未保存确认遵守保存保护，并随保存或取消结束', async () => {
    vi.stubGlobal('URL', class extends URL {
      static createObjectURL = () => 'blob:profile'
      static revokeObjectURL = vi.fn()
    })
    vi.stubGlobal('Image', class { src = ''; decode = () => Promise.resolve() })
    let complete: (value: typeof user) => void = () => undefined
    vi.mocked(saveAvatar).mockImplementation(() => new Promise(resolve => { complete = resolve }))
    const onClose = vi.fn()
    render(<LocaleProvider><SettingsDialog open user={user} themePreference="light"
      onThemePreferenceChange={vi.fn()} onToast={vi.fn()} onClose={onClose} /></LocaleProvider>)
    const choose = async () => {
      fireEvent.change(screen.getByLabelText('选择头像图片'), { target: { files: [new File(['photo'], 'avatar.png', { type: 'image/png' })] } })
      await screen.findByRole('button', { name: '保存' })
      fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' })
      expect(screen.getByRole('group', { name: '头像尚未保存' })).toBeInTheDocument()
    }
    await choose()
    fireEvent.click(screen.getByRole('button', { name: '保存' }))
    expect(screen.getByRole('button', { name: '放弃更改' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: '放弃更改' }))
    expect(onClose).not.toHaveBeenCalled()
    complete(user)
    await screen.findByRole('status')
    expect(screen.queryByRole('group', { name: '头像尚未保存' })).not.toBeInTheDocument()
    await choose()
    fireEvent.click(screen.getByRole('button', { name: '取消' }))
    expect(screen.queryByRole('group', { name: '头像尚未保存' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '保存' })).not.toBeInTheDocument()
  })
  it('shows the supported user fields and changes the controlled appearance preference', async () => {
    const onThemePreferenceChange = vi.fn()
    render(<LocaleProvider>
      <SettingsDialog
        onToast={vi.fn()}
        open
        user={user}
        themePreference="system"
        onThemePreferenceChange={onThemePreferenceChange}
        onClose={vi.fn()}
      />
    </LocaleProvider>)

    expect(screen.getByRole('dialog', { name: '设置' })).toBeInTheDocument()
    const settingsContent = screen.getByRole('region', { name: '设置' })

    expect(settingsContent).toHaveAttribute('role', 'region')
    expect(settingsContent).toHaveAttribute('tabindex', '0')
    expect(screen.getByText('yunsan')).toBeInTheDocument()
    expect(screen.queryByRole('textbox', { name: '用户名' })).not.toBeInTheDocument()
    expect(screen.queryByText(/用户 ID|角色|禁用/)).not.toBeInTheDocument()
    expect(screen.queryByText(/后续|暂不|修改功能/)).not.toBeInTheDocument()
    const accountSection = screen.getByRole('button', { name: '账号管理' })
    expect(accountSection).toHaveAttribute('aria-current', 'page')
    expect(screen.queryByRole('radio', { name: '跟随系统' })).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: '通用' }))
    expect(screen.getByRole('button', { name: '通用' })).toHaveAttribute('aria-current', 'page')
    expect(screen.getByRole('radio', { name: '跟随系统' })).toBeChecked()

    await userEvent.click(screen.getByRole('radio', { name: '深色' }))

    expect(onThemePreferenceChange).toHaveBeenCalledWith('dark')

    await userEvent.click(screen.getByRole('button', { name: '界面语言' }))
    await userEvent.click(screen.getByRole('option', { name: 'English' }))
    expect(screen.getByRole('dialog', { name: 'Settings' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Account' })).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Account' }))
    expect(screen.getByText('yunsan')).toBeInTheDocument()
  })

  it('closes on Escape and restores focus to the account trigger', async () => {
    const trigger = document.createElement('button')
    document.body.append(trigger)
    trigger.focus()
    const onClose = vi.fn()
    const { unmount } = render(<LocaleProvider>
      <SettingsDialog
        onToast={vi.fn()}
        open
        user={{ ...user, avatar_url: null }}
        themePreference="light"
        restoreFocusTo={trigger}
        onThemePreferenceChange={vi.fn()}
        onClose={onClose}
      />
    </LocaleProvider>)

    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' })
    expect(onClose).toHaveBeenCalledOnce()
    unmount()
    await waitFor(() => expect(trigger).toHaveFocus())
    trigger.remove()
  })

  it('只让最上层语言列表处理 Escape，不连带关闭设置', async () => {
    const onClose = vi.fn()
    render(<LocaleProvider>
      <SettingsDialog
        onToast={vi.fn()}
        open
        user={user}
        themePreference="light"
        onThemePreferenceChange={vi.fn()}
        onClose={onClose}
      />
    </LocaleProvider>)

    await userEvent.click(screen.getByRole('button', { name: '通用' }))
    await userEvent.click(screen.getByRole('button', { name: '界面语言' }))
    const listbox = screen.getByRole('listbox', { name: '界面语言' })

    fireEvent.keyDown(listbox, { key: 'Escape' })

    expect(screen.queryByRole('listbox', { name: '界面语言' })).not.toBeInTheDocument()
    expect(screen.getByRole('dialog', { name: '设置' })).toBeInTheDocument()
    expect(onClose).not.toHaveBeenCalled()
  })
})
