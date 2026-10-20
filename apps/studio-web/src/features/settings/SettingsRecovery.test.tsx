import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { SettingsDialog } from './SettingsDialog'

const state = vi.hoisted(() => ({fail: true}))
vi.mock('./ModelSettingsPanel', () => ({ModelSettingsPanel: () => {
  if (state.fail) throw new Error('render failure')
  return <p>模型配置内容</p>
}}))

describe('模型设置区域恢复', () => {
  afterEach(() => { vi.restoreAllMocks(); state.fail = true })
  it('渲染故障通知一次，重试按钮恢复区域且不重复错误正文', () => {
    vi.spyOn(console, 'error').mockImplementation(() => undefined)
    const onToast = vi.fn()
    render(<SettingsDialog open user={{user_id: 1, username: 'test', avatar_url: null, roles: [], disabled: false}} themePreference="light" onThemePreferenceChange={vi.fn()} onClose={vi.fn()} onToast={onToast} />)
    fireEvent.click(screen.getByRole('button', {name: '模型配置'}))
    expect(onToast).toHaveBeenCalledExactlyOnceWith('error', '模型加载失败，请先重试')
    expect(screen.queryByText('模型加载失败，请先重试')).not.toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    state.fail = false
    fireEvent.click(screen.getByRole('button', {name: '重新加载模型'}))
    expect(screen.getByText('模型配置内容')).toBeInTheDocument()
    expect(onToast).toHaveBeenCalledOnce()
  })
})
