import { useEffect, useRef, useState } from 'react'

import { saveAvatar } from '../../api/auth/client'
import type { AuthUser } from '../../api/auth/types'
import { buildApiUrl } from '../../api/shared/config'
import { useI18n } from '../../i18n'

type Draft = { file: File; url: string } | undefined
interface ProfileOperations {
  attempt: number
  request: AbortController | null
  preview: { image: HTMLImageElement; url: string } | null
}

function cancelSelection(operations: ProfileOperations) {
  operations.attempt++
  if (operations.preview) {
    operations.preview.image.src = ''
    URL.revokeObjectURL(operations.preview.url)
    operations.preview = null
  }
}

/** 头像草稿只在设置内生效；关闭后释放预览并忽略仍在解码或保存的结果 */
export function useUserProfile(user: AuthUser, open: boolean) {
  const { t } = useI18n()
  const [draft, setDraft] = useState<Draft>()
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [selecting, setSelecting] = useState(false)
  const [saving, setSaving] = useState(false)
  const operations = useRef<ProfileOperations>({ attempt: 0, request: null, preview: null })

  useEffect(() => () => { if (draft) URL.revokeObjectURL(draft.url) }, [draft])
  useEffect(() => {
    const owned = operations.current
    setDraft(undefined); setError(''); setNotice(''); setSelecting(false); setSaving(false)
    return () => { cancelSelection(owned); owned.request?.abort(); owned.request = null }
  }, [open, user.user_id])

  const reset = () => {
    if (operations.current.request) return
    cancelSelection(operations.current); setDraft(undefined); setError(''); setNotice(''); setSelecting(false)
  }
  const choose = async (file: File) => {
    const owned = operations.current
    if (owned.request) return
    cancelSelection(owned)
    const ownAttempt = owned.attempt
    setError(''); setNotice('')
    if (!['image/png', 'image/jpeg', 'image/webp', 'image/gif'].includes(file.type)
      || !file.size || file.size > 5 * 1024 * 1024) {
      setSelecting(false); setError(t('请选择不超过 5 MiB 的 PNG、JPEG、WebP 或 GIF 图片')); return
    }
    const url = URL.createObjectURL(file)
    setSelecting(true)
    try {
      const image = new Image()
      owned.preview = { image, url }
      image.src = url
      await image.decode()
      if (owned.attempt !== ownAttempt) return
      owned.preview = null
      setDraft({ file, url })
    } catch {
      if (owned.attempt === ownAttempt) {
        cancelSelection(owned); setSelecting(false); setError(t('图片无法读取，请重新选择'))
      }
    } finally {
      if (owned.attempt === ownAttempt) setSelecting(false)
    }
  }
  const save = async () => {
    const owned = operations.current
    if (draft === undefined || selecting || owned.request) return
    const controller = new AbortController()
    owned.request = controller; setSaving(true); setError(''); setNotice('')
    try {
      const updated = await saveAvatar(draft.file, controller.signal)
      if (controller.signal.aborted) return
      if (updated) { setDraft(undefined); setNotice(t('头像已保存')) }
    } catch {
      if (!controller.signal.aborted) setError(t('保存失败，请重试'))
    } finally {
      if (owned.request === controller) { owned.request = null; setSaving(false) }
    }
  }

  return {
    avatarUrl: draft === undefined ? (user.avatar_url ? buildApiUrl(user.avatar_url) : null) : draft.url,
    dirty: draft !== undefined, error, notice, selecting, saving, choose, reset, save,
  }
}
