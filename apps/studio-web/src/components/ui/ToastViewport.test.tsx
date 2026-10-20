import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ToastViewport } from './ToastViewport'

describe('ToastViewport', () => {
  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  const useReducedMotion = () => {
    vi.stubGlobal('matchMedia', vi.fn((query: string) => ({
      matches: query === '(prefers-reduced-motion: reduce)',
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })))
  }

  it('dismisses each toast by its stable id and respects kind durations', () => {
    vi.useFakeTimers()
    useReducedMotion()
    const onDismiss = vi.fn()
    render(
      <ToastViewport
        toasts={[
          { id: 'saved', kind: 'success', message: '已保存' },
          { id: 'failed', kind: 'error', message: '删除失败' },
          { id: 'warning', kind: 'warning', message: '请注意' },
        ]}
        onDismiss={onDismiss}
      />,
    )

    act(() => vi.advanceTimersByTime(3000))
    expect(onDismiss).not.toHaveBeenCalledWith('saved')
    expect(onDismiss).not.toHaveBeenCalledWith('failed')
    expect(onDismiss).not.toHaveBeenCalledWith('warning')

    fireEvent.click(screen.getByRole('button', { name: '关闭提示：删除失败' }))
    expect(onDismiss).toHaveBeenCalledWith('failed')
    act(() => vi.advanceTimersByTime(2999))
    expect(onDismiss).not.toHaveBeenCalledWith('warning')
    act(() => vi.advanceTimersByTime(1))
    expect(onDismiss).toHaveBeenCalledWith('saved')
    expect(onDismiss).toHaveBeenCalledWith('warning')
  })

  it('pauses the remaining timeout while hovered', () => {
    vi.useFakeTimers()
    useReducedMotion()
    const onDismiss = vi.fn()
    render(
      <ToastViewport
        toasts={[{ id: 'info', kind: 'info', message: '正在同步' }]}
        onDismiss={onDismiss}
      />,
    )

    const toast = screen.getByText('正在同步').closest('li') as HTMLElement
    act(() => vi.advanceTimersByTime(2000))
    fireEvent.mouseEnter(toast)
    act(() => vi.advanceTimersByTime(5000))
    expect(onDismiss).not.toHaveBeenCalled()

    fireEvent.mouseLeave(toast)
    act(() => vi.advanceTimersByTime(3999))
    expect(onDismiss).not.toHaveBeenCalled()
    act(() => vi.advanceTimersByTime(1))
    expect(onDismiss).toHaveBeenCalledWith('info')
  })

  it('pauses the remaining timeout while keyboard focus stays inside', () => {
    vi.useFakeTimers()
    useReducedMotion()
    const onDismiss = vi.fn()
    render(
      <ToastViewport
        toasts={[{ id: 'info-focus', kind: 'info', message: '等待用户确认' }]}
        onDismiss={onDismiss}
      />,
    )

    const close = screen.getByRole('button', { name: '关闭提示：等待用户确认' })
    fireEvent.focus(close)
    act(() => vi.advanceTimersByTime(5000))
    expect(onDismiss).not.toHaveBeenCalled()

    fireEvent.blur(close, { relatedTarget: document.body })
    act(() => vi.advanceTimersByTime(5999))
    expect(onDismiss).not.toHaveBeenCalled()
    act(() => vi.advanceTimersByTime(1))
    expect(onDismiss).toHaveBeenCalledWith('info-focus')
  })

  it('requests dismissal only once when a manual close races the timeout', () => {
    vi.useFakeTimers()
    useReducedMotion()
    const onDismiss = vi.fn()
    render(
      <ToastViewport
        toasts={[{ id: 'failed', kind: 'error', message: '服务暂不可用，请稍后重试。' }]}
        onDismiss={onDismiss}
      />,
    )

    expect(screen.getByRole('status')).toHaveTextContent(/^服务暂不可用，请稍后重试$/)
    expect(screen.getByRole('status')).not.toHaveTextContent('。')
    fireEvent.click(screen.getByRole('button', { name: '关闭提示：服务暂不可用，请稍后重试' }))
    act(() => vi.advanceTimersByTime(6000))

    expect(onDismiss).toHaveBeenCalledTimes(1)
    expect(onDismiss).toHaveBeenCalledWith('failed')
  })
})
