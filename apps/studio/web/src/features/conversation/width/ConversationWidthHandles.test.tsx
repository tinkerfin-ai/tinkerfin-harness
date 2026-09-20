import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { LocaleProvider } from '../../../i18n'
import { ConversationWidthHandles } from './ConversationWidthHandles'
import { useConversationWidth } from './useConversationWidth'
import { CONVERSATION_WIDTH_KEY } from './widthPreference'

function Consumer() {
  const control = useConversationWidth(apply => apply())
  return <main ref={control.rootRef} style={{ '--layout-page-gutter': '32px' } as React.CSSProperties}>
    <ConversationWidthHandles control={control} />
    <output aria-label="会话宽度">{control.width}</output>
  </main>
}

afterEach(() => { localStorage.clear(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

it('两侧鼠标对称调宽，取消或无移动不覆盖保存值', () => {
  let frame: FrameRequestCallback | undefined
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => { frame = callback; return 1 })
  vi.stubGlobal('cancelAnimationFrame', () => { frame = undefined })
  vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(1600)
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  render(<LocaleProvider><Consumer /></LocaleProvider>)
  const right = screen.getByRole('separator', { name: '调整会话右侧宽度' })
  const left = screen.getByRole('separator', { name: '调整会话左侧宽度' })
  for (const element of [right, left]) {
    element.setPointerCapture = vi.fn()
    element.releasePointerCapture = vi.fn()
  }
  const pointer = (element: HTMLElement, type: string, x: number) => {
    const event = new Event(type, { bubbles: true, cancelable: true })
    Object.defineProperties(event, { button: { value: 0 }, pointerId: { value: 1 }, clientX: { value: x }, clientY: { value: 100 } })
    fireEvent(element, event)
  }
  pointer(right, 'pointerdown', 100)
  pointer(right, 'pointermove', 125)
  act(() => frame?.(0))
  expect(screen.getByRole('status', { name: '会话宽度' })).toHaveTextContent('970')
  expect(localStorage.getItem(CONVERSATION_WIDTH_KEY)).toBeNull()
  pointer(right, 'pointerup', 125)
  expect(localStorage.getItem(CONVERSATION_WIDTH_KEY)).toBe('970')
  pointer(left, 'pointerdown', 100)
  pointer(left, 'pointermove', 50)
  act(() => frame?.(0))
  expect(screen.getByRole('status', { name: '会话宽度' })).toHaveTextContent('1070')
  pointer(left, 'pointercancel', 50)
  expect(screen.getByRole('status', { name: '会话宽度' })).toHaveTextContent('970')
  pointer(right, 'pointerdown', 100)
  pointer(right, 'pointerup', 100)
  expect(localStorage.getItem(CONVERSATION_WIDTH_KEY)).toBe('970')
  expect(left.tabIndex).toBe(-1)
  expect(right.tabIndex).toBe(-1)
  fireEvent.keyDown(left, { key: 'ArrowLeft' })
  expect(localStorage.getItem(CONVERSATION_WIDTH_KEY)).toBe('970')
  pointer(right, 'pointerdown', 100)
  pointer(right, 'pointerup', -900)
  expect(localStorage.getItem(CONVERSATION_WIDTH_KEY)).toBe('640')
  pointer(right, 'pointerdown', 100)
  pointer(right, 'pointerup', 1100)
  expect(localStorage.getItem(CONVERSATION_WIDTH_KEY)).toBe('1504')
})
