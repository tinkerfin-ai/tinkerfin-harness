import { createContext } from 'react'

/** 让共享选择器识别所属弹窗，Portal 浮层无需业务调用方传递层级 */
export const DialogContext = createContext(false)

export interface DialogDetailOptions {
  title: string
  onClose: () => void
}

/** 已有弹窗借出同一焦点范围展示详情，原内容保持挂载 */
export const DialogDetailContext = createContext<{
  target: HTMLDivElement | null
  show: (detail: DialogDetailOptions) => () => void
} | null>(null)
