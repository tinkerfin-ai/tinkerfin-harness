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

  it('keeps each list item semantic while the message carries the live role', () => {
    render(
      <ToastViewport
        toasts={[
          { id: 'failed', kind: 'error', message: '保存失败' },
          { id: 'saved', kind: 'success', message: '保存成功' },
        ]}
        onDismiss={vi.fn()}
      />,
    )

    expect(screen.getByRole('list', { name: '系统提示' })).toBeInTheDocument()
    expect(screen.getAllByRole('status')).toHaveLength(2)
    const errorToast = screen.getByText('保存失败').closest('li')
    expect(errorToast).toHaveClass('toast-card')
    expect(errorToast?.querySelector('.ui-feedback-icon__mark'))
      .toHaveTextContent('!')
    expect(errorToast?.querySelector('circle')).toBeNull()
  })

  it('removes only one trailing full stop and preserves sentence boundaries', () => {
    render(
      <ToastViewport
        toasts={[{
          id: 'detached',
          kind: 'info',
          message: '已停止接收实时输出。后端任务可能仍在继续。',
        }]}
        onDismiss={vi.fn()}
      />,
    )

    expect(screen.getByRole('status')).toHaveTextContent(
      '已停止接收实时输出。后端任务可能仍在继续',
    )
  })

  it('uses the same localized title for every toast kind', () => {
    render(
      <ToastViewport
        toasts={[
          { id: 'success', kind: 'success', message: '已保存' },
          { id: 'info', kind: 'info', message: '正在同步' },
          { id: 'error', kind: 'error', message: '保存失败' },
          { id: 'warning', kind: 'warning', message: '附件仍在上传' },
        ]}
        onDismiss={vi.fn()}
      />,
    )

    expect(screen.getAllByText('提示', { selector: '.toast-card__title' })).toHaveLength(4)
  })

  it('renders warning with the shared toast structure and status semantics', () => {
    render(
      <ToastViewport
        toasts={[{ id: 'warning', kind: 'warning', message: '附件仍在上传' }]}
        onDismiss={vi.fn()}
      />,
    )

    const message = screen.getByRole('status')
    const toast = message.closest('li') as HTMLElement
    expect(toast).toHaveClass('toast-card', 'is-warning')
    expect(toast.querySelector('.toast-card__title')).toHaveTextContent('提示')
    expect(toast.querySelector('.ui-feedback-icon')).toHaveClass('is-warning')
    expect(toast.querySelector('svg')).toBeInTheDocument()
  })
})
