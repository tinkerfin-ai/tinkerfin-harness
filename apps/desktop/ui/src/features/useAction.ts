import { useEffect, useRef, useState } from 'react'
import { send } from '../bridge'
import type { ActionResult, DesktopAction } from '../state'

/** 单个交互区域拥有提交与结算；卸载后不再接纳迟到的界面回调 */
export function useAction() {
  const owned = useRef(false)
  const mounted = useRef(true)
  const [pending, setPending] = useState(false)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  async function run(action: DesktopAction, settle?: (result: ActionResult) => void) {
    if (owned.current) return
    owned.current = true
    setPending(true)
    try {
      const result = await send(action)
      if (mounted.current) settle?.(result)
    } finally {
      owned.current = false
      if (mounted.current) setPending(false)
    }
  }
  return { pending, run }
}
