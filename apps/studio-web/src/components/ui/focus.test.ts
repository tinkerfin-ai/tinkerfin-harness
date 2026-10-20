import { afterEach, describe, expect, it } from 'vitest'
import { fireEvent } from '@testing-library/react'
import { isRestoringFocus, prepareFocusReturn, restoreFocus } from './focus'

afterEach(() => { document.body.replaceChildren() })

describe('返回入口焦点', () => {
  it('保持入口焦点和同步恢复信号，后续键盘导航恢复可见焦点', () => {
    const button = document.createElement('button')
    document.body.append(button)
    let restoring = false
    button.addEventListener('focus', () => { restoring = isRestoringFocus(button) })
    restoreFocus(button)
    expect(document.activeElement).toBe(button)
    expect(restoring).toBe(true)
    expect(isRestoringFocus(button)).toBe(false)
    expect(button).toHaveAttribute('data-ui-focus-restored')
    for (const key of ['Escape', 'Escape', 'Shift', 'Control', 'c']) {
      fireEvent.keyDown(button, { key })
      expect(button).toHaveAttribute('data-ui-focus-restored')
    }
    fireEvent.keyDown(button, { key: 'Tab' })
    expect(button).not.toHaveAttribute('data-ui-focus-restored')
  })

  it('重复恢复后原地点击不揭露外框，离开入口清理标记', () => {
    const button = document.createElement('button')
    const next = document.createElement('button')
    document.body.append(button, next)
    restoreFocus(button)
    restoreFocus(button)
    next.focus()
    expect(button).not.toHaveAttribute('data-ui-focus-restored')
    restoreFocus(button)
    fireEvent.pointerDown(button)
    expect(button).toHaveAttribute('data-ui-focus-restored')
    next.focus()
    expect(button).not.toHaveAttribute('data-ui-focus-restored')
  })

  it('第三方回焦只标记真实返回，外部焦点和下一次操作取消预期', () => {
    const trigger = document.createElement('button')
    const outside = document.createElement('input')
    document.body.append(trigger, outside)
    outside.focus()
    const cancel = prepareFocusReturn(trigger)
    trigger.focus()
    expect(trigger).toHaveAttribute('data-ui-focus-restored')
    outside.focus()
    cancel()
    prepareFocusReturn(trigger)
    fireEvent.keyDown(outside, { key: 'Tab' })
    trigger.focus()
    expect(trigger).not.toHaveAttribute('data-ui-focus-restored')
    outside.focus()
    const stop = prepareFocusReturn(trigger)
    stop()
    trigger.focus()
    expect(trigger).not.toHaveAttribute('data-ui-focus-restored')
  })

  it('第三方关闭时入口仍有焦点，也立即采用返回样式', () => {
    const trigger = document.createElement('button')
    document.body.append(trigger)
    trigger.focus()
    const cancel = prepareFocusReturn(trigger)
    expect(trigger).toHaveAttribute('data-ui-focus-restored')
    cancel()
  })

  it('关闭回调在 Escape 捕获阶段标记已聚焦入口，同次事件不清除返回样式', () => {
    const trigger = document.createElement('button')
    document.body.append(trigger)
    trigger.focus()
    let cancel = () => {}
    document.addEventListener('keydown', () => { cancel = prepareFocusReturn(trigger) }, { capture: true, once: true })
    fireEvent.keyDown(trigger, { key: 'Escape' })
    expect(trigger).toHaveAttribute('data-ui-focus-restored')
    cancel()
  })

  it('不可聚焦目标不保留返回标记', () => {
    const button = document.createElement('button')
    button.disabled = true
    document.body.append(button)
    restoreFocus(button)
    expect(document.activeElement).not.toBe(button)
    expect(button).not.toHaveAttribute('data-ui-focus-restored')
  })
})
