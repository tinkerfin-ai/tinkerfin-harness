import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ErrorBoundary } from './ErrorBoundary'

function Broken({ shouldThrow }: { shouldThrow: boolean }) {
  if (shouldThrow) throw new Error('渲染失败')
  return <p>区域已恢复</p>
}

describe('ErrorBoundary', () => {
  beforeEach(() => {
    vi.spyOn(console, 'error').mockImplementation(() => undefined)
    vi.spyOn(console, 'warn').mockImplementation(() => undefined)
  })
  afterEach(() => vi.restoreAllMocks())

  it('isolates a render failure and exposes an explicit reset action', () => {
    let shouldThrow = true
    const MutableBroken = () => <Broken shouldThrow={shouldThrow} />
    render(
      <ErrorBoundary fallback={({ reset }) => (
        <button type="button" onClick={() => { shouldThrow = false; reset() }}>重试区域</button>
      )}>
        <MutableBroken />
      </ErrorBoundary>,
      { onCaughtError: () => undefined },
    )

    expect(screen.getByRole('button', { name: '重试区域' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '重试区域' }))
    expect(screen.getByText('区域已恢复')).toBeInTheDocument()
  })

  it('resets automatically when its stable region key changes', () => {
    const { rerender } = render(
      <ErrorBoundary resetKey="thread-a" fallback={() => <p>会话区域失败</p>}>
        <Broken shouldThrow />
      </ErrorBoundary>,
      { onCaughtError: () => undefined },
    )
    expect(screen.getByText('会话区域失败')).toBeInTheDocument()

    rerender(
      <ErrorBoundary resetKey="thread-b" fallback={() => <p>会话区域失败</p>}>
        <Broken shouldThrow={false} />
      </ErrorBoundary>,
    )
    expect(screen.getByText('区域已恢复')).toBeInTheDocument()
  })

  it('reports the captured error through the optional observer', () => {
    const onError = vi.fn()
    render(
      <ErrorBoundary onError={onError} fallback={() => <p>已隔离</p>}>
        <Broken shouldThrow />
      </ErrorBoundary>,
      { onCaughtError: () => undefined },
    )
    expect(onError).toHaveBeenCalledOnce()
    expect(onError.mock.calls[0][0]).toEqual(expect.objectContaining({ message: '渲染失败' }))
  })
})
