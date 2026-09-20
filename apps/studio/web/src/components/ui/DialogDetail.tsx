import { useContext, useLayoutEffect } from 'react'
import type { ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { DialogDetailContext } from './DialogContext'

/** 详情使用宿主弹窗的全屏区域；关闭时恢复宿主内容和入口焦点 */
export function DialogDetail({ title, onClose, children }: {
  title: string; onClose: () => void; children: ReactNode
}) {
  const host = useContext(DialogDetailContext)
  useLayoutEffect(() => host?.show({ title, onClose }), [host, title, onClose])
  return host?.target ? createPortal(children, host.target) : null
}
