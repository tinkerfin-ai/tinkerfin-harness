import { useEffect, useRef, useState } from 'react'
import type { ToastHandler } from '../../components/ui/ToastViewport'
import { isTranslationKey, useI18n } from '../../i18n'
import { deleteMemory, readMemory, saveMemory, type MemoryDetail, type MemoryItem } from './api'

type Editor = { original: MemoryDetail | null; path: string; content: string; trigger: HTMLElement | null }

/** 保留未保存内容与冲突对照；新的编辑意图取消旧读取，离开页面释放请求 */
export function useMemoryEditor(projectId: string, refresh: () => void, onToast: ToastHandler) {
  const { t } = useI18n()
  const [editor, setEditor] = useState<Editor | null>(null)
  const [removal, setRemoval] = useState<MemoryItem | null>(null)
  const [loading, setLoading] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [conflict, setConflict] = useState<MemoryDetail | null>(null)
  const [discarding, setDiscarding] = useState(false)
  const reading = useRef<AbortController | null>(null)
  const request = useRef<AbortController | null>(null)
  useEffect(() => () => { reading.current?.abort(); request.current?.abort() }, [])
  const cancelReading = () => {
    reading.current?.abort()
    reading.current = null
    setLoading(null)
  }
  const open = async (item: MemoryItem, trigger: HTMLElement) => {
    cancelReading()
    const controller = new AbortController(); reading.current = controller
    setLoading(item.path); setError(''); setConflict(null); setDiscarding(false)
    try {
      const value = await readMemory(projectId, item.path, controller.signal)
      if (!controller.signal.aborted && reading.current === controller) setEditor({ original: value, path: value.path.slice(1), content: value.content, trigger })
    } catch (reason) { if (!controller.signal.aborted) onToast('error', reason instanceof Error ? isTranslationKey(reason.message) ? t(reason.message) : reason.message : t('记忆读取失败，请重试')) }
    finally { if (reading.current === controller) { reading.current = null; setLoading(null) } }
  }
  const closeEditor = () => {
    if (saving) return
    if (editor && (editor.content !== (editor.original?.content ?? '') || (!editor.original && editor.path))) { setDiscarding(true); return }
    setEditor(null)
  }
  const save = async () => {
    if (!editor || saving) return
    const path = editor.path.trim().startsWith('/') ? editor.path.trim() : `/${editor.path.trim()}`
    if (path === '/') { setError(t('请输入记忆名称')); return }
    const controller = new AbortController(); request.current = controller
    setSaving(true); setError('')
    try {
      await saveMemory(projectId, path, editor.content, conflict?.etag ?? editor.original?.etag ?? null, controller.signal)
      if (controller.signal.aborted) return
      setEditor(null); setConflict(null); refresh(); onToast('success', t('记忆已保存'))
    } catch (reason) {
      if (controller.signal.aborted) return
      setError(reason instanceof Error ? isTranslationKey(reason.message) ? t(reason.message) : reason.message : t('记忆未能保存，请重试'))
      if (editor.original) {
        try {
          const latest = await readMemory(projectId, path, controller.signal)
          if (!controller.signal.aborted && latest.etag !== editor.original.etag) setConflict(latest)
        } catch { /* 保存失败时保留本地内容，页面继续提供重试 */ }
      }
    } finally { if (!controller.signal.aborted) setSaving(false) }
  }
  const remove = async () => {
    if (!removal || saving) return
    const controller = new AbortController(); request.current = controller
    setSaving(true); setError('')
    try {
      await deleteMemory(projectId, removal, controller.signal)
      if (!controller.signal.aborted) { setRemoval(null); refresh(); onToast('success', t('记忆已删除')) }
    } catch (reason) { if (!controller.signal.aborted) setError(reason instanceof Error ? isTranslationKey(reason.message) ? t(reason.message) : reason.message : t('删除失败，请刷新后重试')) }
    finally { if (!controller.signal.aborted) setSaving(false) }
  }
  return {
    editor, removal, loading, saving, error, conflict, discarding, open, closeEditor, save, remove,
    setEditor, setRemoval, setDiscarding,
    create: (trigger: HTMLElement) => {
      cancelReading(); setError(''); setConflict(null); setDiscarding(false)
      setEditor({ original: null, path: '', content: '', trigger })
    },
    requestRemoval: (item: MemoryItem) => { cancelReading(); setError(''); setRemoval(item) },
    loadConflict: () => {
      if (!editor || !conflict) return
      setEditor({ ...editor, original: conflict, content: conflict.content }); setConflict(null); setError('')
    },
  }
}
