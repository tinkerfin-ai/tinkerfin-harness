import { CornerDownLeft, Download, Info } from 'lucide-react'
import { useContext, useEffect, useId, useRef, useState } from 'react'
import { Button, Dialog, IconButton } from '../../../components/ui'
import { useI18n } from '../../../i18n'
import { MarkdownContent } from '../components/MarkdownContent'
import { attachmentBlob } from './client'
import type { Attachment } from './content'
import { AttachmentReferenceContext } from './context'
import { useAttachmentDownload } from './useAttachmentDownload'
import {
  documentFormat, DocumentPreviewError, DOCUMENT_PREVIEW_BYTES, DOCUMENT_PREVIEW_CHARACTERS,
  type OfficePreview, type PreviewFailure,
} from './documentPreview'
import { readOfficePreview } from './readOfficePreview'
import { sanitizeDocumentHtml } from './sanitizeDocumentHtml'
import { AttachmentFileIcon } from './AttachmentFileIcon'
import { AttachmentViewerTitle } from './AttachmentViewerTitle'
import { formatAttachmentSize } from './attachmentPresentation'

type Content = OfficePreview | { kind: 'markdown'; text: string; truncated: boolean } | { kind: 'pdf'; url: string }
type Preview =
  | { phase: 'loading' }
  | { phase: 'unsupported' }
  | { phase: 'failed'; reason?: PreviewFailure }
  | { phase: 'ready'; content: Content }

export function DocumentAttachmentPreview({
  attachment,
  attachments = [attachment],
  onSelect,
  onClose,
  returnFocus,
}: {
  attachment: Attachment
  attachments?: readonly Attachment[]
  onSelect?: (id: string) => void
  onClose: () => void
  returnFocus: HTMLElement | null
}) {
  const { t } = useI18n()
  const tableLabelId = useId()
  const reference = useContext(AttachmentReferenceContext)
  const download = useAttachmentDownload(attachment)
  const [attempt, setAttempt] = useState(0)
  const [preview, setPreview] = useState<Preview>({ phase: 'loading' })
  const [infoOpen, setInfoOpen] = useState(false)
  const infoButton = useRef<HTMLButtonElement>(null)
  useEffect(() => {
    const controller = new AbortController()
    let objectUrl: string | undefined
    async function load() {
      const format = documentFormat(attachment.mime_type)
      if (!format) {
        setPreview({ phase: 'unsupported' })
        return
      }
      try {
        if (attachment.size_bytes > DOCUMENT_PREVIEW_BYTES) throw new DocumentPreviewError('size')
        const blob = await attachmentBlob(attachment.id, 'original', controller.signal)
        if (controller.signal.aborted) return
        if (blob.size > DOCUMENT_PREVIEW_BYTES) throw new DocumentPreviewError('size')
        const bytes = await blob.arrayBuffer()
        if (controller.signal.aborted) return
        let content: Content
        if (format === 'markdown') {
          const text = new TextDecoder('utf-8', { fatal: true }).decode(bytes)
          if (text.includes('\0')) throw new DocumentPreviewError('invalid')
          content = { kind: 'markdown', text: text.slice(0, DOCUMENT_PREVIEW_CHARACTERS), truncated: text.length > DOCUMENT_PREVIEW_CHARACTERS }
        } else if (format === 'pdf') {
          if (new TextDecoder().decode(bytes.slice(0, 5)) !== '%PDF-') throw new DocumentPreviewError('invalid')
          objectUrl = URL.createObjectURL(new Blob([bytes], { type: 'application/pdf' }))
          content = { kind: 'pdf', url: objectUrl }
        } else {
          content = await readOfficePreview(bytes, format, controller.signal)
          if (controller.signal.aborted) return
          if (content.kind === 'docx') content = { ...content, html: sanitizeDocumentHtml(content.html, tableLabelId) }
        }
        setPreview({ phase: 'ready', content })
      } catch (error) {
        if (!controller.signal.aborted) setPreview({ phase: 'failed', reason: error instanceof DocumentPreviewError ? error.code : undefined })
      }
    }
    void load()
    return () => {
      controller.abort()
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [attachment.id, attachment.mime_type, attachment.size_bytes, attempt, tableLabelId])
  const content = preview.phase === 'ready' ? preview.content : undefined
  const failure = preview.phase === 'failed' && preview.reason === 'size'
    ? t('文档超出预览限制，请下载查看')
    : preview.phase === 'failed' && preview.reason === 'timeout'
      ? t('文档解析超时，请下载查看或重试')
      : t('文档暂时无法打开，请重试')
  const format = documentFormat(attachment.mime_type)
  const formatLabel = format === 'docx'
    ? t('Word 文档')
    : format === 'xlsx'
      ? t('Excel 工作簿')
      : format === 'markdown'
        ? 'Markdown'
        : format === 'pdf'
          ? 'PDF'
          : t('未知文件')
  return (
    <Dialog
      open
      title={<AttachmentViewerTitle attachment={attachment} />}
      className="attachment-document-viewer"
      onClose={onClose}
      restoreFocusTo={returnFocus}
      onKeyDown={(event) => {
        if (event.defaultPrevented) return
        if (event.key === 'Escape' && infoOpen) {
          event.preventDefault()
          setInfoOpen(false)
          infoButton.current?.focus()
        }
      }}
      headerActions={
        <div className="attachment-viewer-actions">
          {reference && <IconButton
            type="button" variant="ghost" label={t('引用附件：{name}', { name: attachment.name })}
            tooltip={t('引用附件')} icon={<CornerDownLeft size={18} />}
            onClick={() => { reference(attachment); onClose() }}
          />}
          <IconButton
            type="button" variant="ghost" label={t('下载附件：{name}', { name: attachment.name })}
            tooltip={t('下载附件')} icon={<Download size={18} />}
            loading={download.pending} onClick={() => void download.download()}
          />
          <IconButton
            ref={infoButton}
            type="button"
            variant="ghost"
            label={t('文件信息')}
            tooltip={t('文件信息')}
            aria-expanded={infoOpen}
            icon={<Info size={18} />}
            onClick={() => setInfoOpen(!infoOpen)}
          />
        </div>
      }
    >
      <div className={`attachment-document-layout${infoOpen ? ' is-info-open' : ''}`}>
        <aside className="attachment-document-rail" aria-label={t('本次交付文件')}>
          <strong>{t('本次交付 · {count} 个文件', { count: attachments.length })}</strong>
          {attachments.map(item => (
            <button
              key={item.id}
              type="button"
              aria-current={item.id === attachment.id ? 'true' : undefined}
              aria-label={t('查看文件：{name}', { name: item.name })}
              onClick={() => onSelect?.(item.id)}
            >
              <AttachmentFileIcon attachment={item} compact />
              <span>{item.name}</span>
            </button>
          ))}
        </aside>
        <div className="attachment-document-body" role="region" aria-label={t('文档预览')} tabIndex={0}>
          {preview.phase === 'loading' && <p role="status">{t('正在加载文档…')}</p>}
          {preview.phase === 'unsupported' && <div className="attachment-document-unavailable">
            <AttachmentFileIcon attachment={attachment} />
            <h3>{t('此格式无法在线预览')}</h3>
            <p>{t('文件本身没有损坏，请下载后使用对应应用打开')}</p>
            <Button type="button" variant="primary" leadingIcon={<Download size={16} />} loading={download.pending} onClick={() => void download.download()}>
              {t('下载文件')}
            </Button>
          </div>}
          {preview.phase === 'failed' && <div className="attachment-error" role="status">
            {failure}
            {preview.reason !== 'size' && <Button type="button" variant="ghost" onClick={() => { setPreview({ phase: 'loading' }); setAttempt(value => value + 1) }}>
              {t('重试预览')}
            </Button>}
          </div>}
          {content?.kind === 'markdown' && <div className="attachment-document-page">
            {content.text.trim() ? <MarkdownContent content={content.text} allowRemoteImages={false} /> : <p>{t('此文档为空')}</p>}
            {content.truncated && <p className="attachment-preview-note">{t('文档较长，仅预览前 10 万字符；下载可查看完整内容')}</p>}
          </div>}
          {content?.kind === 'docx' && <div className="attachment-document-page">
            <span id={tableLabelId} hidden>{t('可横向滚动的表格')}</span>
            <p className="attachment-preview-note">{t('预览保留文字和表格；图片与版式请下载查看')}</p>
            {content.html ? <div className="markdown-content markdown-content--article attachment-document-content" dangerouslySetInnerHTML={{ __html: content.html }} /> : <p>{t('此文档没有可预览的文字')}</p>}
          </div>}
          {content?.kind === 'pdf' && <>
            <iframe className="attachment-document-pdf" title={t('PDF 预览：{name}', { name: attachment.name })}
              src={content.url} referrerPolicy="no-referrer" />
            <p className="attachment-preview-note attachment-preview-note--pdf">{t('PDF 由浏览器显示；若未显示，请下载查看')}</p>
          </>}
          {content?.kind === 'xlsx' && <div className="attachment-document-page attachment-document-page--sheet">
            <p className="attachment-preview-note">{t('工作表：{name}', { name: content.sheet })}</p>
            {content.rows.length ? <div className="attachment-sheet-scroll" role="region" aria-label={t('可横向滚动的表格')} tabIndex={0}>
              <table className="attachment-sheet">
                <thead><tr><th scope="col">{t('行')}</th>{content.columns.map(column => <th scope="col" key={column}>{column}</th>)}</tr></thead>
                <tbody>{content.rows.map((row, index) => <tr key={index}>
                  <th scope="row">{index + 1}</th>{row.map((cell, column) => <td key={column}>{cell}</td>)}
                </tr>)}</tbody>
              </table>
            </div> : <p>{t('此工作表为空')}</p>}
            <p className="attachment-preview-note">{t('仅预览第一个工作表的已保存值，不计算公式')}{content.truncated && ` · ${t('最多显示 100 行、20 列，每格 500 字符；下载可查看完整内容')}`}</p>
          </div>}
          {download.failed && <p className="attachment-error" role="status">{t('下载失败，请重试')}</p>}
        </div>
        {infoOpen && <aside className="attachment-viewer-info" aria-label={t('文件信息')}>
          <h3>{t('文件信息')}</h3>
          <dl>
            <dt>{t('文件名')}</dt>
            <dd>{attachment.name}</dd>
            <dt>{t('文件格式')}</dt>
            <dd>{formatLabel}</dd>
            <dt>{t('文件大小')}</dt>
            <dd>{formatAttachmentSize(attachment.size_bytes)}</dd>
          </dl>
        </aside>}
      </div>
    </Dialog>
  )
}
