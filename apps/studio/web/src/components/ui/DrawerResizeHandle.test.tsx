import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { DrawerResizeHandle } from './DrawerResizeHandle'
import { useDrawerLayout } from './useDrawerLayout'

let hostWidth = 1200
let measure: () => void
function Harness({ minimum = 640 }) {
  const control = useDrawerLayout(minimum)
  return <div ref={control.hostRef}>
    {control.available && <DrawerResizeHandle control={control} label="调整抽屉宽度" controls="details" />}
  </div>
}

beforeEach(() => {
  hostWidth = 1200
  vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockImplementation(() => hostWidth)
  vi.stubGlobal('ResizeObserver', class {
    constructor(callback: () => void) { measure = callback }
    observe() {}
    disconnect() {}
  })
})
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals() })

it.each([640, 520])('为主内容保留 %ipx，收窄、隐藏和恢复不丢失用户宽度', minimum => {
  render(<Harness minimum={minimum} />)
  const handle = () => screen.getByRole('separator', { name: '调整抽屉宽度' })
  expect(handle()).toHaveAttribute('aria-valuenow', '400')
  fireEvent.keyDown(handle(), { key: 'End' })
  expect(handle()).toHaveAttribute('aria-valuenow', '520')
  act(() => { hostWidth = minimum + 350; measure() })
  expect(handle()).toHaveAttribute('aria-valuenow', '350')
  act(() => { hostWidth = minimum + 300; measure() })
  expect(handle()).toHaveAttribute('aria-valuenow', '300')
  act(() => { hostWidth -= 1; measure() })
  expect(screen.queryByRole('separator')).not.toBeInTheDocument()
  act(() => { hostWidth = 1400; measure() })
  expect(handle()).toHaveAttribute('aria-valuenow', '520')
  fireEvent.keyDown(handle(), { key: 'Home' })
  expect(handle()).toHaveAttribute('aria-valuenow', '300')
  fireEvent.keyDown(handle(), { key: 'ArrowRight' })
  expect(handle()).toHaveAttribute('aria-valuenow', '300')
  fireEvent.keyDown(handle(), { key: 'ArrowLeft' })
  expect(handle()).toHaveAttribute('aria-valuenow', '316')
})

it('左边缘拖动限制宽度，取消手势恢复选择，重新挂载恢复默认', () => {
  let frame: FrameRequestCallback | undefined
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => { frame = callback; return 1 })
  vi.stubGlobal('cancelAnimationFrame', () => { frame = undefined })
  const { unmount } = render(<Harness />)
  const handle = screen.getByRole('separator')
  handle.setPointerCapture = vi.fn()
  handle.releasePointerCapture = vi.fn()
  const pointer = (type: string, x: number) => {
    const event = new Event(type, { bubbles: true })
    Object.defineProperties(event, { button: { value: 0 }, pointerId: { value: 1 }, clientX: { value: x } })
    fireEvent(handle, event)
  }
  pointer('pointerdown', 500)
  pointer('pointermove', 300)
  act(() => frame?.(0))
  expect(handle).toHaveAttribute('aria-valuenow', '520')
  pointer('pointercancel', 300)
  expect(handle).toHaveAttribute('aria-valuenow', '400')
  pointer('pointerdown', 500)
  pointer('pointerup', 300)
  expect(handle).toHaveAttribute('aria-valuenow', '520')
  pointer('pointerdown', 500)
  pointer('pointerup', 1000)
  expect(handle).toHaveAttribute('aria-valuenow', '300')
  unmount()
  render(<Harness />)
  expect(screen.getByRole('separator')).toHaveAttribute('aria-valuenow', '400')
})
