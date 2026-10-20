import './focus.css'

let restorationTarget: HTMLElement | null = null
const clearRestoredFocus = new WeakMap<HTMLElement, () => void>()

/** 程序返回入口不展示键盘外框；Tab 导航或离开目标后恢复正常焦点样式 */
export function markRestoredFocus(target: HTMLElement): void {
  clearRestoredFocus.get(target)?.()
  target.setAttribute('data-ui-focus-restored', '')
  const clear = () => {
    target.removeAttribute('data-ui-focus-restored')
    target.removeEventListener('blur', clear)
    target.removeEventListener('keydown', navigate, true)
    clearRestoredFocus.delete(target)
  }
  const navigate = (event: KeyboardEvent) => {
    if (event.key === 'Tab') clear()
  }
  clearRestoredFocus.set(target, clear)
  target.addEventListener('blur', clear)
  target.addEventListener('keydown', navigate, true)
}

/** 第三方控件自行回焦时只标记真实返回，不抢走外部点击目标；调用方在卸载时取消监听 */
export function prepareFocusReturn(target: HTMLElement | null): () => void {
  if (!target) return () => {}
  const document = target.ownerDocument
  if (document.activeElement === target) markRestoredFocus(target)
  const cancel = () => {
    document.removeEventListener('focusin', focused, true)
    document.removeEventListener('keydown', cancel, true)
    document.removeEventListener('pointerdown', cancel, true)
  }
  const focused = (event: FocusEvent) => {
    cancel()
    if (event.target === target) markRestoredFocus(target)
  }
  document.addEventListener('focusin', focused, true)
  document.addEventListener('keydown', cancel, true)
  document.addEventListener('pointerdown', cancel, true)
  return cancel
}

/** 恢复入口或定位页面阅读区，不将程序聚焦当作键盘导航；控件初始聚焦和方向键导航使用普通 focus */
export function restoreFocus(target: HTMLElement | null, options?: FocusOptions): void {
  if (!target) return
  const previous = restorationTarget
  restorationTarget = target
  markRestoredFocus(target)
  try {
    target.focus(options)
    if (target.ownerDocument.activeElement !== target) clearRestoredFocus.get(target)?.()
  } finally {
    restorationTarget = previous
  }
}

/** 只识别当前恢复目标，主动键盘导航和其他控件的焦点保持原有行为 */
export function isRestoringFocus(target: HTMLElement): boolean {
  return restorationTarget === target
}
