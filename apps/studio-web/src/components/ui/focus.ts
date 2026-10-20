let restorationTarget: HTMLElement | null = null

/** 恢复交互入口焦点；同步焦点事件可识别恢复来源，避免重新显示已关闭的提示 */
export function restoreFocus(target: HTMLElement | null, options?: FocusOptions): void {
  if (!target) return
  const previous = restorationTarget
  restorationTarget = target
  try {
    target.focus(options)
  } finally {
    restorationTarget = previous
  }
}

/** 只识别当前恢复目标，主动键盘导航和其他控件的焦点保持原有行为 */
export function isRestoringFocus(target: HTMLElement): boolean {
  return restorationTarget === target
}
