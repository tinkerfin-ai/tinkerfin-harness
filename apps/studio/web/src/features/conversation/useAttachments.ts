import { useCallback, useEffect, useRef, useState } from 'react'
import type { Attachment } from './attachments/content'
import { removeDraftAttachment, uploadAttachment } from './attachments/client'

export interface DraftAttachment {
  id: string
  name: string
  size: number
  kind: 'image' | 'document'
  file?: File
  attachment?: Attachment
  state: 'queued' | 'uploading' | 'ready' | 'error'
  progress: number
  error?: string
  reference?: boolean
}

export function useAttachments(onError?: (message: string) => void) {
  const latestErrorHandler = useRef(onError)
  latestErrorHandler.current = onError
  const [attachments, setAttachments] = useState<DraftAttachment[]>([])
  const latest = useRef(attachments)
  const controllers = useRef(new Map<string, AbortController>())
  const alive = useRef(true)
  const draftEpoch = useRef(0)
  const update = useCallback((items: DraftAttachment[]) => {
    latest.current = items
    setAttachments(items)
  }, [])
  const pump = useCallback(
    function startQueued() {
      while (controllers.current.size < 2) {
        const item = latest.current.find(
          (value) => value.state === 'queued' && value.file,
        )
        if (!item?.file) break
        const controller = new AbortController()
        controllers.current.set(item.id, controller)
        update(
          latest.current.map((value) =>
            value.id === item.id ? { ...value, state: 'uploading' } : value,
          ),
        )
        void uploadAttachment(item.file, controller.signal, (progress) => {
          if (alive.current && !controller.signal.aborted)
            update(
              latest.current.map((value) =>
                value.id === item.id ? { ...value, progress } : value,
              ),
            )
        })
          .then((attachment) => {
            if (
              !alive.current ||
              controller.signal.aborted ||
              !latest.current.some((value) => value.id === item.id)
            )
              return
            update(
              latest.current.map((value) =>
                value.id === item.id
                  ? { ...value, attachment, state: 'ready', progress: 100 }
                  : value,
              ),
            )
          })
          .catch((reason) => {
            if (!alive.current || controller.signal.aborted) return
            const message = reason instanceof Error ? reason.message : '上传失败，请重试'
            latestErrorHandler.current?.(message)
            update(
              latest.current.map((value) =>
                value.id === item.id
                  ? {
                      ...value,
                      state: 'error',
                      error:
                        reason instanceof Error
                          ? reason.message
                          : '上传失败，请重试',
                    }
                  : value,
              ),
            )
          })
          .finally(() => {
            controllers.current.delete(item.id)
            if (alive.current) startQueued()
          })
      }
    },
    [update],
  )
  useEffect(() => {
    alive.current = true
    const active = controllers.current
    return () => {
      alive.current = false
      draftEpoch.current += 1
      for (const controller of active.values()) controller.abort()
      active.clear()
    }
  }, [])
  const addFiles = useCallback(
    (files: readonly File[]) => {
      const items = [...latest.current]
      let failure: string | undefined
      for (const file of files) {
        const ext = file.name.split('.').pop()?.toLowerCase() ?? ''
        if (
          ![
            'png',
            'jpg',
            'jpeg',
            'webp',
            'gif',
            'pdf',
            'docx',
            'xlsx',
            'pptx',
            'md',
            'markdown',
          ].includes(ext)
        ) {
          failure = '仅支持图片、Markdown、PDF、DOCX、XLSX 和 PPTX'
          continue
        }
        if (
          items.length >= 5 ||
          file.size > 10 * 1024 ** 2 ||
          items.reduce((sum, item) => sum + item.size, 0) + file.size >
            25 * 1024 ** 2
        ) {
          failure = '最多 5 个附件，单个 10 MiB，合计 25 MiB'
          continue
        }
        items.push({
          id: crypto.randomUUID(),
          name: file.name,
          file,
          size: file.size,
          kind: ['png', 'jpg', 'jpeg', 'webp', 'gif'].includes(ext)
            ? 'image'
            : 'document',
          state: 'queued',
          progress: 0,
        })
      }
      if (failure) latestErrorHandler.current?.(failure)
      update(items)
      pump()
    },
    [pump, update],
  )
  const removeAttachment = useCallback(
    (id: string) => {
      const item = latest.current.find((value) => value.id === id)
      controllers.current.get(id)?.abort()
      update(latest.current.filter((value) => value.id !== id))
      const epoch = draftEpoch.current
      if (item?.attachment && !item.reference)
        void removeDraftAttachment(item.attachment.id).catch(() => {
          if (alive.current && epoch === draftEpoch.current)
            latestErrorHandler.current?.('附件已移出草稿，服务端会清理未发送文件')
        })
    },
    [update],
  )
  const retryAttachment = useCallback(
    (id: string) => {
      update(
        latest.current.map((value) =>
          value.id === id && value.state === 'error'
            ? { ...value, state: 'queued', error: undefined, progress: 0 }
            : value,
        ),
      )
      pump()
    },
    [pump, update],
  )
  const clearAttachments = useCallback(() => {
    draftEpoch.current += 1
    for (const controller of controllers.current.values()) controller.abort()
    update([])
  }, [update])
  const completeSend = useCallback(
    (ids: readonly string[]) => {
      const sent = new Set(ids)
      update(latest.current.filter((item) => !sent.has(item.id)))
    },
    [update],
  )
  const addReference = useCallback(
    (attachment: Attachment) => {
      if (latest.current.some((item) => item.attachment?.id === attachment.id))
        return
      if (
        latest.current.length >= 5 ||
        latest.current.reduce((sum, item) => sum + item.size, 0) +
          attachment.size_bytes >
          25 * 1024 ** 2
      ) {
        latestErrorHandler.current?.('附件数量或总大小超过限制')
        return
      }
      update([
        ...latest.current,
        {
          id: crypto.randomUUID(),
          name: attachment.name,
          size: attachment.size_bytes,
          kind: attachment.mime_type.startsWith('image/')
            ? 'image'
            : 'document',
          state: 'ready',
          progress: 100,
          attachment,
          reference: true,
        },
      ])
    },
    [update],
  )
  return {
    attachments,
    addFiles,
    removeAttachment,
    retryAttachment,
    clearAttachments,
    completeSend,
    addReference,
  }
}
