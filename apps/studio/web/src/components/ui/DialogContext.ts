import { createContext } from 'react'

/** 让共享选择器识别所属弹窗，Portal 浮层无需业务调用方传递层级 */
export const DialogContext = createContext(false)
