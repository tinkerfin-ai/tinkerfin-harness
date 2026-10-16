import { useEffect, useRef, useState } from 'react'

/** 显示等待反馈后打开文件选择器；选择或取消后恢复入口，卸载时取消尚未执行的打开 */
export function useAttachmentPicker(onError?: () => void) {
  const inputRef = useRef<HTMLInputElement>(null)
  const buttonRef = useRef<HTMLButtonElement>(null)
  const frame = useRef<number | null>(null)
  const waiting = useRef(false)
  const [pending, setPending] = useState(false)

  useEffect(() => {
    const input = inputRef.current
    const finish = () => {
      waiting.current = false
      setPending(false)
      if (frame.current !== null) cancelAnimationFrame(frame.current)
      frame.current = requestAnimationFrame(() => {
        frame.current = null
        buttonRef.current?.focus()
      })
    }
    input?.addEventListener('change', finish)
    input?.addEventListener('cancel', finish)
    return () => {
      input?.removeEventListener('change', finish)
      input?.removeEventListener('cancel', finish)
      if (frame.current !== null) cancelAnimationFrame(frame.current)
    }
  }, [])

  const open = () => {
    if (waiting.current || !inputRef.current) return
    if (frame.current !== null) cancelAnimationFrame(frame.current)
    waiting.current = true
    setPending(true)
    // 先让加载图标完成一帧绘制，再交给浏览器打开文件窗口
    frame.current = requestAnimationFrame(() => {
      frame.current = requestAnimationFrame(() => {
        frame.current = null
        try {
          inputRef.current?.click()
        } catch {
          waiting.current = false
          setPending(false)
          onError?.()
        }
      })
    })
  }

  return { inputRef, buttonRef, open, pending }
}
