import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { saveAvatar } from '../../api/auth/client'
import { LocaleProvider } from '../../i18n'
import { testAuthSession } from '../../test/authSession'
import { UserProfileEditor } from './UserProfileEditor'
import { useUserProfile } from './useUserProfile'

vi.mock('../../api/auth/client', () => ({ saveAvatar: vi.fn() }))

function Editor() {
  const profile = useUserProfile(testAuthSession.user, true)
  return <UserProfileEditor username="tester" profile={profile} confirmClose={false} onCloseDecision={vi.fn()} />
}

beforeEach(() => {
  vi.stubGlobal('URL', class extends URL {
    static createObjectURL = vi.fn(() => 'blob:avatar-preview')
    static revokeObjectURL = vi.fn()
  })
  vi.stubGlobal('Image', class { src = ''; decode = vi.fn().mockResolvedValue(undefined) })
})
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); vi.mocked(saveAvatar).mockReset() })

async function selectImage() {
  const file = new File(['image'], 'avatar.png', { type: 'image/png' })
  fireEvent.change(screen.getByLabelText('选择头像图片'), { target: { files: [file] } })
  await screen.findByRole('button', { name: '保存' })
  return file
}

it('选择后才显示保存操作，取消释放图片且不写入账户', async () => {
  render(<LocaleProvider><Editor /></LocaleProvider>)
  expect(screen.queryByRole('button', { name: '保存' })).not.toBeInTheDocument()
  expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
  await selectImage()
  fireEvent.click(screen.getByRole('button', { name: '取消' }))
  expect(screen.queryByRole('button', { name: '保存' })).not.toBeInTheDocument()
  expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:avatar-preview')
  expect(saveAvatar).not.toHaveBeenCalled()
  expect(screen.getByRole('button', { name: '更换头像' })).toHaveFocus()
})

it('保存失败保留图片，重试同一文件后结束草稿', async () => {
  vi.mocked(saveAvatar).mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce(testAuthSession.user)
  render(<LocaleProvider><Editor /></LocaleProvider>)
  const file = await selectImage()
  fireEvent.click(screen.getByRole('button', { name: '保存' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('保存失败，请重试')
  fireEvent.click(screen.getByRole('button', { name: '保存' }))
  expect(await screen.findByRole('status')).toHaveTextContent('头像已保存')
  expect(saveAvatar).toHaveBeenNthCalledWith(1, file, expect.any(AbortSignal))
  expect(saveAvatar).toHaveBeenNthCalledWith(2, file, expect.any(AbortSignal))
  expect(screen.queryByRole('button', { name: '取消' })).not.toBeInTheDocument()
})

it('保存期间禁用重复写入，卸载取消当前请求并释放预览', async () => {
  let finish: (value: null) => void = () => undefined
  vi.mocked(saveAvatar).mockImplementation(() => new Promise(resolve => { finish = resolve }))
  const { unmount } = render(<LocaleProvider><Editor /></LocaleProvider>)
  await selectImage()
  fireEvent.click(screen.getByRole('button', { name: '保存' }))
  expect(screen.getByRole('button', { name: '保存中' })).toBeDisabled()
  expect(screen.getByRole('button', { name: '更换头像' })).toBeDisabled()
  const signal = vi.mocked(saveAvatar).mock.calls[0][1]
  unmount()
  expect(signal.aborted).toBe(true)
  finish(null)
  await waitFor(() => expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:avatar-preview'))
})

it('解码期间卸载也立即释放尚未交付的预览', async () => {
  let finish: () => void = () => undefined
  vi.stubGlobal('Image', class { src = ''; decode = () => new Promise<void>(resolve => { finish = resolve }) })
  const { unmount } = render(<LocaleProvider><Editor /></LocaleProvider>)
  fireEvent.change(screen.getByLabelText('选择头像图片'), { target: { files: [new File(['image'], 'avatar.png', { type: 'image/png' })] } })
  expect(screen.getByRole('button', { name: '更换头像' })).toBeDisabled()
  unmount()
  expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:avatar-preview')
  finish()
  expect(saveAvatar).not.toHaveBeenCalled()
})
