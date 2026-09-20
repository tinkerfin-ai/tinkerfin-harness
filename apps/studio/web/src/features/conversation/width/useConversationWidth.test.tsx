import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { useConversationWidth } from './useConversationWidth'
import { CONVERSATION_WIDTH_KEY } from './widthPreference'

function Consumer() {
  const width = useConversationWidth(apply => apply())
  return <main ref={width.rootRef} style={{ '--layout-conversation-gutter': '64px' } as React.CSSProperties}>
    <output>{width.width}</output>
    <button onClick={() => width.commit(1200)}>选择</button>
    <button onClick={() => width.previewWidth(1000)}>预览</button>
    <button onClick={width.cancel}>取消</button>
  </main>
}

afterEach(() => { localStorage.clear(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

it('响应容器变化、取消预览和其他标签页选择', () => {
  let column = 1600
  let resize = () => {}
  const disconnect = vi.fn()
  vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockImplementation(() => column)
  vi.stubGlobal('ResizeObserver', class {
    constructor(callback: () => void) { resize = callback }
    observe() {}
    disconnect = disconnect
  })
  const view = render(<Consumer />)
  expect(screen.getByRole('status')).toHaveTextContent('920')
  fireEvent.click(screen.getByText('选择'))
  expect(screen.getByRole('status')).toHaveTextContent('1200')
  column = 900
  act(() => resize())
  expect(screen.getByRole('status')).toHaveTextContent('740')
  expect(localStorage.getItem(CONVERSATION_WIDTH_KEY)).toBe('1200')
  column = 1600
  act(() => resize())
  fireEvent.click(screen.getByText('预览'))
  expect(screen.getByRole('status')).toHaveTextContent('1000')
  fireEvent.click(screen.getByText('取消'))
  expect(screen.getByRole('status')).toHaveTextContent('1200')
  act(() => window.dispatchEvent(new StorageEvent('storage', { key: CONVERSATION_WIDTH_KEY, newValue: '880' })))
  expect(screen.getByRole('status')).toHaveTextContent('880')
  act(() => window.dispatchEvent(new StorageEvent('storage', { key: CONVERSATION_WIDTH_KEY, newValue: null })))
  expect(screen.getByRole('status')).toHaveTextContent('920')
  view.unmount()
  expect(disconnect).toHaveBeenCalledOnce()
})
