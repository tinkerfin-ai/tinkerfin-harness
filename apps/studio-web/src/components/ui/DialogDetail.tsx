import { useContext, useLayoutEffect, useRef } from 'react'
import type { ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { DialogDetailContext } from './DialogContext'
import type { DialogDetailOptions } from './DialogContext'

/** 详情使用宿主弹窗的全屏区域；关闭时恢复宿主内容和入口焦点 */
export function DialogDetail({ title, labelledBy, closeDisabled, onClose, children }: DialogDetailOptions & { children: ReactNode }) {
  const host = useContext(DialogDetailContext)
  const show = host?.show
  const latest = useRef({ title, labelledBy, closeDisabled, onClose })
  latest.current = { title, labelledBy, closeDisabled, onClose }
  const display = useRef<ReturnType<NonNullable<typeof show>> | null>(null)
  useLayoutEffect(() => {
    const current = show?.(latest.current)
    display.current = current ?? null
    return () => { current?.close(); display.current = null }
  }, [show])
  useLayoutEffect(() => { display.current?.update(latest.current) }, [title, labelledBy, closeDisabled, onClose])
  return host?.target ? createPortal(children, host.target) : null
}
