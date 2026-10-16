import { useCallback, useEffect, useRef, useState } from 'react'
import { attachmentBlob } from './client'
import type { Attachment } from './content'

/** 下载原附件并保留文件名，失败反馈归属发起下载的区域 */
export function useAttachmentDownload(attachment: Attachment) {
  const [pending, setPending] = useState(false)
  const [failed, setFailed] = useState(false)
  const active = useRef<AbortController | null>(null)
  const resources = useRef(new Map<string, ReturnType<typeof setTimeout>>())
  useEffect(() => {
    const urls = resources.current
    setPending(false)
    setFailed(false)
    return () => {
      active.current?.abort()
      active.current = null
      for (const [url, timer] of urls) {
        clearTimeout(timer)
        URL.revokeObjectURL(url)
      }
      urls.clear()
    }
  }, [attachment.id])
  const download = useCallback(async () => {
    if (active.current) return
    const controller = new AbortController()
    active.current = controller
    setPending(true)
    setFailed(false)
    try {
      const blob = await attachmentBlob(
        attachment.id,
        'original',
        controller.signal,
      )
      if (controller.signal.aborted) return
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = attachment.name
      link.click()
      resources.current.set(
        url,
        setTimeout(() => {
          URL.revokeObjectURL(url)
          resources.current.delete(url)
        }, 1000),
      )
    } catch {
      if (!controller.signal.aborted) setFailed(true)
    } finally {
      if (!controller.signal.aborted) {
        active.current = null
        setPending(false)
      }
    }
  }, [attachment.id, attachment.name])
  return { download, pending, failed }
}
