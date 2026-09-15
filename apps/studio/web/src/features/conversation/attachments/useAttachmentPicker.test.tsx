import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { IconButton } from '../../../components/ui'
import { useAttachmentPicker } from './useAttachmentPicker'

function Picker({ onError = vi.fn() }: { onError?: () => void }) {
  const picker = useAttachmentPicker(onError)
  return (
    <>
      <input ref={picker.inputRef} type="file" aria-label="选择附件" />
      <IconButton ref={picker.buttonRef} label="添加附件" icon={null} loading={picker.pending} onClick={picker.open} />
    </>
  )
}

beforeEach(() => vi.useFakeTimers())
afterEach(() => {
  vi.restoreAllMocks()
  vi.useRealTimers()
})

describe('附件选择等待反馈', () => {
  it.each(['cancel', 'change'])('打开前先显示加载反馈，%s后恢复入口', (event) => {
    const open = vi.spyOn(HTMLInputElement.prototype, 'click').mockImplementation(() => {
      expect(screen.getByRole('button', { name: '添加附件' })).toHaveAttribute('aria-busy', 'true')
    })
    render(<Picker />)
    const button = screen.getByRole('button', { name: '添加附件' })
    fireEvent.click(button)
    expect(button).toHaveAttribute('aria-busy', 'true')
    expect(button).toBeDisabled()
    expect(open).not.toHaveBeenCalled()
    fireEvent.click(button)
    act(() => vi.advanceTimersByTime(40))
    expect(open).toHaveBeenCalledOnce()
    fireEvent(screen.getByLabelText('选择附件'), new Event(event, { bubbles: true }))
    expect(button).toBeEnabled()
    expect(button).not.toHaveAttribute('aria-busy')
    act(() => vi.advanceTimersByTime(20))
    expect(button).toHaveFocus()
  })

  it('打开失败时清除等待并允许重试', () => {
    const open = vi.spyOn(HTMLInputElement.prototype, 'click').mockImplementation(() => { throw new Error('unavailable') })
    const onError = vi.fn()
    render(<Picker onError={onError} />)
    const button = screen.getByRole('button', { name: '添加附件' })
    fireEvent.click(button)
    act(() => vi.advanceTimersByTime(40))
    expect(button).toBeEnabled()
    expect(onError).toHaveBeenCalledOnce()
    open.mockImplementation(() => undefined)
    fireEvent.click(button)
    act(() => vi.advanceTimersByTime(40))
    expect(open).toHaveBeenCalledTimes(2)
  })

  it('输入区卸载后不再打开系统选择器', () => {
    const open = vi.spyOn(HTMLInputElement.prototype, 'click').mockImplementation(() => undefined)
    const { unmount } = render(<Picker />)
    fireEvent.click(screen.getByRole('button', { name: '添加附件' }))
    unmount()
    act(() => vi.advanceTimersByTime(40))
    expect(open).not.toHaveBeenCalled()
  })
})
