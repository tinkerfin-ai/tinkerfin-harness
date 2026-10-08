import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { useRef } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { InteractionCardResizeHandle } from './InteractionCardResizeHandle'

const cardRect = (height: number) => ({
  x: 0,
  y: 900 - height,
  top: 900 - height,
  right: 780,
  bottom: 900,
  left: 0,
  width: 780,
  height,
  toJSON: () => ({}),
})

function Harness() {
  const cardRef = useRef<HTMLElement>(null)
  return (
    <section ref={cardRef} id="resize-card" className="resize-test-card">
      <InteractionCardResizeHandle cardRef={cardRef} controls="resize-card" />
    </section>
  )
}

const dispatchPointer = (
  target: Element,
  type: string,
  properties: { button?: number; clientY?: number; pointerId: number; pointerType: string },
) => {
  const event = new Event(type, { bubbles: true, cancelable: true })
  Object.defineProperties(event, Object.fromEntries(
    Object.entries(properties).map(([key, value]) => [key, { value }]),
  ))
  fireEvent(target, event)
}

describe('InteractionCardResizeHandle', () => {
  beforeEach(() => {
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function getRect(this: HTMLElement) {
      if (!this.classList.contains('resize-test-card')) return cardRect(0)
      const value = this.style.getPropertyValue('--interaction-card-height')
      const height = value.includes('min-height')
        ? 260
        : value.includes('max-height')
          ? 680
          : Number.parseFloat(value) || 260
      return cardRect(height)
    })
  })

  afterEach(() => {
    vi.restoreAllMocks()
    document.documentElement.classList.remove('is-resizing-interaction-card')
  })

  it('exposes the current range and supports the horizontal separator keyboard contract', async () => {
    render(<Harness />)
    const handle = screen.getByRole('separator', { name: '调整交互卡片高度' })
    const card = document.getElementById('resize-card')!

    await waitFor(() => expect(handle).toHaveAttribute('aria-valuemin', '260'))
    expect(handle).toHaveAttribute('aria-controls', 'resize-card')
    expect(handle).toHaveAttribute('aria-orientation', 'horizontal')
    expect(handle).toHaveAttribute('aria-valuemax', '680')
    expect(handle).toHaveAttribute('aria-valuenow', '260')

    fireEvent.keyDown(handle, { key: 'End' })
    expect(card.style.getPropertyValue('--interaction-card-height')).toBe('680px')
    expect(handle).toHaveAttribute('aria-valuenow', '680')

    fireEvent.keyDown(handle, { key: 'ArrowDown' })
    expect(card.style.getPropertyValue('--interaction-card-height')).toBe('664px')
    fireEvent.keyDown(handle, { key: 'ArrowDown', shiftKey: true })
    expect(card.style.getPropertyValue('--interaction-card-height')).toBe('616px')

    fireEvent.keyDown(handle, { key: 'Home' })
    expect(card.style.getPropertyValue('--interaction-card-height')).toBe('260px')
    expect(handle).toHaveAttribute('aria-valuenow', '260')
  })

  it('stops at the exact pointer release height and restores the start height on cancel', async () => {
    render(<Harness />)
    const handle = screen.getByRole('separator', { name: '调整交互卡片高度' })
    const card = document.getElementById('resize-card')!

    await waitFor(() => expect(handle).toHaveAttribute('aria-valuenow', '260'))
    dispatchPointer(handle, 'pointerdown', { button: 0, pointerId: 7, pointerType: 'mouse', clientY: 500 })
    dispatchPointer(handle, 'pointermove', { pointerId: 7, pointerType: 'mouse', clientY: 290 })

    dispatchPointer(handle, 'pointerup', { pointerId: 7, pointerType: 'mouse', clientY: 280 })

    expect(card.style.getPropertyValue('--interaction-card-height')).toBe('480px')

    dispatchPointer(handle, 'pointerdown', { button: 0, pointerId: 8, pointerType: 'mouse', clientY: 500 })
    dispatchPointer(handle, 'pointermove', { pointerId: 8, pointerType: 'mouse', clientY: 300 })
    dispatchPointer(handle, 'pointercancel', { pointerId: 8, pointerType: 'mouse' })
    expect(card.style.getPropertyValue('--interaction-card-height')).toBe('480px')
  })
})
