import { useCallback, useLayoutEffect, useRef, useState } from 'react'
import type { Attachment } from './attachments/content'
import { removeDraftAttachment, uploadAttachment } from './attachments/client'
import { translateCurrent } from '../../i18n'

export interface DraftAttachment {
  id: string
  name: string
  size: number
  kind: 'image' | 'document'
  file?: File
  /** 本地文件已上传或待上传的项目；历史附件引用不需要重新上传 */
  uploadProjectId?: string
  attachment?: Attachment
  state: 'queued' | 'uploading' | 'ready' | 'error'
  progress: number
  error?: string
  reference?: boolean
}

function attachmentsForProject(items: DraftAttachment[], projectId: string): DraftAttachment[] {
  return items.map(item => item.reference || item.uploadProjectId === projectId ? item : {
    ...item, uploadProjectId: projectId, attachment: undefined, progress: 0,
    state: item.file ? 'queued' : 'error',
    error: item.file ? undefined : translateCurrent('附件的本地文件不可用，请重新选择后再移动会话'),
  })
}

export function useAttachments(projectId: string, onError?: (message: string) => void, retention?: { key: string; store: Map<string, DraftAttachment[]> }) {
  const latestErrorHandler = useRef(onError)
  latestErrorHandler.current = onError
  const [attachments, setAttachments] = useState<DraftAttachment[]>(() => attachmentsForProject(retention?.store.get(retention.key) ?? [], projectId))
  const owner = useRef(retention)
  const currentProject = useRef(projectId)
  const latest = useRef(attachments)
  const controllers = useRef(new Map<string, AbortController>())
  const alive = useRef(true)
  const draftEpoch = useRef(0)
  const update = useCallback((items: DraftAttachment[]) => {
    latest.current = items
    if (owner.current) owner.current.store.set(owner.current.key, items)
    setAttachments(items)
  }, [])
  const pump = useCallback(
    function startQueued() {
      if (!alive.current || currentProject.current !== projectId) return
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
        void uploadAttachment(projectId, item.file, controller.signal, (progress) => {
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
            if (controllers.current.get(item.id) === controller) controllers.current.delete(item.id)
            if (alive.current) startQueued()
          })
      }
    },
    [projectId, update],
  )
  useLayoutEffect(() => {
    alive.current = true
    const active = controllers.current
    return () => {
      alive.current = false
      draftEpoch.current += 1
      for (const controller of active.values()) controller.abort()
      active.clear()
      latest.current = latest.current.map(item => item.state === 'uploading' ? { ...item, state: 'queued', progress: 0 } : item)
      if (owner.current) owner.current.store.set(owner.current.key, latest.current)
    }
  }, [])
  useLayoutEffect(() => {
    if (owner.current?.key !== retention?.key || owner.current?.store !== retention?.store || currentProject.current !== projectId) {
      for (const controller of controllers.current.values()) controller.abort()
      controllers.current.clear()
      if (owner.current) owner.current.store.set(owner.current.key, latest.current.map(item => item.state === 'uploading' ? { ...item, state: 'queued', progress: 0 } : item))
      owner.current = retention
      currentProject.current = projectId
      draftEpoch.current += 1
      update(attachmentsForProject(retention?.store.get(retention.key) ?? [], projectId))
    }
    if (owner.current) owner.current.store.set(owner.current.key, latest.current)
    pump()
  }, [projectId, retention, update, pump])
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
            'zip',
          ].includes(ext)
        ) {
          failure = '仅支持图片、Markdown、PDF、Office 文档和技能 ZIP'
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
          uploadProjectId: projectId,
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
    [projectId, pump, update],
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
      if (owner.current) for (const [key, items] of owner.current.store) owner.current.store.set(key, items.filter(item => !sent.has(item.id)))
      update(latest.current.filter((item) => !sent.has(item.id)))
    },
    [update],
  )
  const moveTo = useCallback((key: string) => {
    if (!owner.current || owner.current.key === key) return
    const { store, key: previous } = owner.current
    store.set(key, latest.current); store.delete(previous)
    owner.current = { store, key }
  }, [])
  const validateProjectMove = useCallback((sourceKey: string) => {
    const items = owner.current?.key === sourceKey ? latest.current : owner.current?.store.get(sourceKey) ?? []
    if (items.some(item => !item.reference && !item.file)) {
      throw new Error(translateCurrent('附件的本地文件不可用，请重新选择后再移动会话'))
    }
  }, [])
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
    moveTo,
    validateProjectMove,
  }
}
