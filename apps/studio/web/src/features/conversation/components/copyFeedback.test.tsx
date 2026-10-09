import { act, cleanup, fireEvent, render, renderHook, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { StrictMode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { CopyCodeButton } from './CopyCodeButton'
import { MessageBlock } from './MessageBlock'
import { COPY_FEEDBACK_DURATION_MS, useCopyFeedback } from './copyFeedback'

function pendingCopy() {
  let resolve!: () => void
  let reject!: (reason: Error) => void
  const promise = new Promise<void>((accept, fail) => { resolve = accept; reject = fail })
  return { promise, resolve, reject }
}

describe('复制反馈的操作归属', () => {
  beforeEach(() => {
    userEvent.setup()
    vi.useFakeTimers()
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
    vi.useRealTimers()
  })

  it.each([false, true])('旧操作逆序完成不覆盖新操作结果（新操作失败：%s）', async (latestFails) => {
    const older = pendingCopy()
    const latest = pendingCopy()
    const write = vi.spyOn(navigator.clipboard, 'writeText')
      .mockReturnValueOnce(older.promise).mockReturnValueOnce(latest.promise)
    const { result } = renderHook(useCopyFeedback, { wrapper: StrictMode })
    let first!: Promise<void>
    let second!: Promise<void>
    act(() => { first = result.current.copy('旧正文'); second = result.current.copy('新正文') })
    await act(async () => {
      if (latestFails) latest.reject(new Error('复制失败'))
      else latest.resolve()
      await second
    })
    const expected = latestFails ? 'failed' : 'copied'
    expect(result.current.state).toBe(expected)
    act(() => { vi.advanceTimersByTime(COPY_FEEDBACK_DURATION_MS / 2) })
    await act(async () => {
      if (latestFails) older.resolve()
      else older.reject(new Error('旧请求失败'))
      await first
    })
    expect(result.current.state).toBe(expected)
    act(() => { vi.advanceTimersByTime(COPY_FEEDBACK_DURATION_MS / 2) })
    expect(result.current.state).toBe('idle')
    expect(write.mock.calls).toEqual([['旧正文'], ['新正文']])
  })

  it('重试取消之前的反馈期限，新结果保留完整反馈时间', async () => {
    const retry = pendingCopy()
    vi.spyOn(navigator.clipboard, 'writeText')
      .mockRejectedValueOnce(new Error('权限暂不可用')).mockReturnValueOnce(retry.promise)
    const { result } = renderHook(useCopyFeedback)
    await act(async () => { await result.current.copy('正文') })
    expect(result.current.state).toBe('failed')
    let retried!: Promise<void>
    act(() => { retried = result.current.copy('正文') })
    act(() => { vi.advanceTimersByTime(COPY_FEEDBACK_DURATION_MS) })
    expect(result.current.state).toBe('failed')
    await act(async () => { retry.resolve(); await retried })
    expect(result.current.state).toBe('copied')
    act(() => { vi.advanceTimersByTime(COPY_FEEDBACK_DURATION_MS - 1) })
    expect(result.current.state).toBe('copied')
    act(() => { vi.advanceTimersByTime(1) })
    expect(result.current.state).toBe('idle')
  })

  it.each([false, true])('卸载后迟到结果不创建反馈任务（复制失败：%s）', async (fails) => {
    const pending = pendingCopy()
    const write = vi.spyOn(navigator.clipboard, 'writeText').mockReturnValue(pending.promise)
    const { result, unmount } = renderHook(useCopyFeedback)
    const copy = result.current.copy
    let completed!: Promise<void>
    act(() => { completed = copy('正文') })
    unmount()
    await act(async () => {
      if (fails) pending.reject(new Error('权限已撤销'))
      else pending.resolve()
      await completed
    })
    expect(vi.getTimerCount()).toBe(0)
    await copy('离开后的正文')
    expect(write).toHaveBeenCalledOnce()
  })

  it('卸载清理尚未到期的反馈任务', async () => {
    vi.spyOn(navigator.clipboard, 'writeText').mockResolvedValue(undefined)
    const { result, unmount } = renderHook(useCopyFeedback)
    await act(async () => { await result.current.copy('正文') })
    expect(result.current.state).toBe('copied')
    unmount()
    expect(vi.getTimerCount()).toBe(0)
  })

  it.each([
    { kind: '用户消息', render: () => <MessageBlock message={{ id: 'user', role: 'user', content: '复制正文', createdAt: '' }} />, idle: '复制消息', copied: '消息已复制' },
    { kind: '回答', render: () => <MessageBlock message={{ id: 'answer', role: 'assistant', content: '复制正文', createdAt: '' }} />, idle: '复制回答', copied: '回答已复制' },
    { kind: '代码', render: () => <CopyCodeButton source="复制正文" />, idle: '复制', copied: '已复制' },
    { kind: '图表源码', render: () => <CopyCodeButton source="复制正文" diagram />, idle: '复制源码', copied: '已复制' },
  ])('$kind 的按钮反馈由最新点击控制', async ({ render: renderControl, idle, copied }) => {
    const older = pendingCopy()
    const latest = pendingCopy()
    const write = vi.spyOn(navigator.clipboard, 'writeText')
      .mockReturnValueOnce(older.promise).mockReturnValueOnce(latest.promise)
    render(renderControl())
    const button = screen.getByRole('button', { name: idle })
    fireEvent.click(button)
    fireEvent.click(button)
    await act(async () => { latest.resolve(); await latest.promise })
    expect(button).toHaveAccessibleName(copied)
    await act(async () => { older.reject(new Error('旧请求失败')); await older.promise.catch(() => {}) })
    expect(button).toHaveAccessibleName(copied)
    expect(write.mock.calls).toEqual([['复制正文'], ['复制正文']])
    act(() => { vi.advanceTimersByTime(COPY_FEEDBACK_DURATION_MS) })
    expect(button).toHaveAccessibleName(idle)
  })
})
