import { Copy, File } from 'lucide-react'
import { useLayoutEffect, useRef, useState } from 'react'
import { Button, FeedbackState } from '../../components/ui'
import { CodeText } from '../../components/ui/CodeText'
import { useI18n } from '../../i18n'
import type { WorkspaceFilesState } from './useWorkspaceFiles'

export function WorkspaceFilePreview({ state, open, onToast }: {
  state: WorkspaceFilesState
  open: boolean
  onToast: (kind: 'success' | 'error', message: string) => void
}) {
  const { t } = useI18n()
  const selected = state.selected
  const copyAttempt = useRef(0)
  const [copying, setCopying] = useState(false)
  const text = state.preview?.kind === 'text' ? state.preview.text : undefined
  useLayoutEffect(() => {
    copyAttempt.current += 1
    return () => { copyAttempt.current += 1 }
  }, [open, selected?.path, text])
  const copyContent = async () => {
    if (!open || text === undefined || copying) return
    const attempt = ++copyAttempt.current
    setCopying(true)
    try {
      await navigator.clipboard.writeText(text)
      if (attempt === copyAttempt.current) onToast('success', t('已复制内容'))
    } catch {
      if (attempt === copyAttempt.current) onToast('error', t('复制失败，请选择正文手动复制'))
    } finally {
      if (attempt === copyAttempt.current) setCopying(false)
    }
  }
  const previewText = text?.replace(/\n$/, '')
  if (!selected) return null
  return <section className="workspace-file-preview" aria-label={t('源码预览')}>
    {state.updated && <div className="workspace-file-updated" role="status"><span>{t('文件已变化')}</span><Button type="button" variant="text" size="xs" onClick={state.refreshPreview}>{t('刷新预览')}</Button></div>}
    {state.previewPhase === 'loading' && !state.preview && <FeedbackState kind="loading" title={t('正在读取文件')} compact />}
    {state.previewPhase === 'missing' && <div className="workspace-files-empty"><File size={32} aria-hidden="true" /><p>{t('文件已被移除')}</p><Button type="button" variant="text" onClick={state.refresh}>{t('刷新文件')}</Button></div>}
    {state.previewPhase === 'error' && <FeedbackState kind="error" title={t('文件预览失败')} appearance="retry" onRetry={state.refreshPreview} compact />}
    {text !== undefined && <>
      <div className="workspace-file-content-tools"><Button type="button" className="workspace-file-copy" variant="ghost" size="sm" aria-label={state.preview?.kind === 'text' && state.preview.truncated ? t('复制当前预览') : t('复制内容')} leadingIcon={<Copy size={15} />} loading={copying} onClick={() => void copyContent()}>{t('复制')}</Button></div>
      {previewText ? <div className="workspace-file-source"><span className="workspace-file-line-numbers" aria-hidden="true">{previewText.split('\n').map((_, index) => <span key={index}>{index + 1}</span>)}</span><pre><CodeText language={selected.name.split('.').at(-1)}>{previewText}</CodeText></pre></div> : <p className="workspace-file-note">{t('此文件为空')}</p>}
      {state.preview?.kind === 'text' && state.preview.truncated && <p className="workspace-file-note" role="status">{t('仅预览前 200 行或 100 KiB')}</p>}
    </>}
    {state.preview?.kind === 'unsupported' && <div className="workspace-files-empty"><File size={32} aria-hidden="true" /><p>{t('此格式仅显示文件信息')}</p></div>}
  </section>
}
