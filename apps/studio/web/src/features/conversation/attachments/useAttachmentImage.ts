import { useCallback, useEffect, useState } from 'react'
import { attachmentBlob } from './client'

/** 为消息、草稿和预览提供受鉴权的图片地址，离开消费区域时取消请求并释放地址 */
export function useAttachmentImage(
  id?: string,
  variant: 'preview' | 'original' = 'preview',
  file?: File,
) {
  const [attempt, setAttempt] = useState(0)
  const [resource, setResource] = useState<{
    id?: string
    file?: File
    url?: string
    failed: boolean
  }>({ failed: false })
  useEffect(() => {
    const controller = new AbortController()
    let url: string | undefined
    setResource({ id, file, failed: false })
    if (id || file) {
      void (
        file
          ? Promise.resolve(file)
          : attachmentBlob(id!, variant, controller.signal)
      )
        .then((blob) => {
          if (controller.signal.aborted) return
          url = URL.createObjectURL(blob)
          setResource({ id, file, url, failed: false })
        })
        .catch(() => {
          if (!controller.signal.aborted)
            setResource({ id, file, failed: true })
        })
    }
    return () => {
      controller.abort()
      if (url) URL.revokeObjectURL(url)
    }
  }, [id, file, variant, attempt])
  const fail = useCallback(
    () => setResource((current) => ({ ...current, failed: true })),
    [],
  )
  const retry = useCallback(() => setAttempt((current) => current + 1), [])
  const current =
    resource.id === id && resource.file === file ? resource : { failed: false }
  return {
    url: current.failed ? undefined : current.url,
    failed: current.failed,
    fail,
    retry,
  }
}
