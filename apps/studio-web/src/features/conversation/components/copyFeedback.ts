import { useCallback, useEffect, useRef, useState } from 'react'

export const COPY_FEEDBACK_DURATION_MS = 1600

/** 复制反馈归属最新一次操作；离开消息或代码区域后不再提交迟到结果 */
export function useCopyFeedback() {
  const [state, setState] = useState<'idle' | 'copied' | 'failed'>('idle')
  const timer = useRef<number | undefined>(undefined)
  const attempt = useRef(0)
  const mounted = useRef(true)

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
      attempt.current += 1
      window.clearTimeout(timer.current)
    }
  }, [])

  const copy = useCallback(async (content: string) => {
    if (!mounted.current) return
    const id = ++attempt.current
    window.clearTimeout(timer.current)
    let next: 'copied' | 'failed' = 'copied'
    try { await navigator.clipboard.writeText(content) } catch { next = 'failed' }
    if (!mounted.current || id !== attempt.current) return
    setState(next)
    timer.current = window.setTimeout(() => {
      timer.current = undefined
      setState('idle')
    }, COPY_FEEDBACK_DURATION_MS)
  }, [])

  return { state, copy }
}
