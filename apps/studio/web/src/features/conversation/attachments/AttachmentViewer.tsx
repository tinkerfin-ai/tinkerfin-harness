import {
  ChevronLeft,
  ChevronRight,
  CornerDownLeft,
  Download,
  Info,
  Maximize,
  Minus,
  Plus,
} from 'lucide-react'
import { useContext, useEffect, useRef, useState } from 'react'
import { Button, Dialog, IconButton } from '../../../components/ui'
import { useI18n } from '../../../i18n'
import type { Attachment } from './content'
import { AttachmentReferenceContext } from './context'
import { useAttachmentImage } from './useAttachmentImage'
import { useAttachmentDownload } from './useAttachmentDownload'
import { useImageViewport } from './useImageViewport'
import { DocumentAttachmentPreview } from './DocumentAttachmentPreview'
import { documentFormat } from './documentPreview'

function GalleryThumbnail({
  attachment,
  selected,
  onSelect,
}: {
  attachment: Attachment
  selected: boolean
  onSelect: () => void
}) {
  const { t } = useI18n()
  const image = useAttachmentImage(attachment.id)
  return (
    <button
      type="button"
      className="attachment-viewer-thumbnail"
      aria-label={t('查看图片：{name}', { name: attachment.name })}
      aria-pressed={selected}
      onClick={onSelect}
    >
      {image.url ? (
        <img src={image.url} alt="" onError={image.fail} />
      ) : (
        <span>{attachment.name}</span>
      )}
    </button>
  )
}

export function AttachmentViewer({
  attachments,
  initialId,
  initialPreview,
  returnFocus,
  onClose,
}: {
  attachments: readonly Attachment[]
  initialId: string
  initialPreview: string
  returnFocus: HTMLElement | null
  onClose: () => void
}) {
  const [selectedId, setSelectedId] = useState(initialId)
  const index = attachments.findIndex((item) => item.id === selectedId)
  const attachment = attachments[index]
  // 预览只阻止背景应用交互，关闭时恢复调用前状态
  useEffect(() => {
    const root = document.getElementById('root')
    const inert = root?.inert
    const overflow = document.body.style.overflow
    if (root) root.inert = true
    document.body.style.overflow = 'hidden'
    return () => {
      if (root) root.inert = inert ?? false
      document.body.style.overflow = overflow
      returnFocus?.focus()
    }
  }, [returnFocus])
  useEffect(() => {
    if (!attachment) onClose()
  }, [attachment, onClose])
  if (!attachment) return null
  if (documentFormat(attachment.mime_type)) {
    return <DocumentAttachmentPreview key={attachment.id} attachment={attachment} onClose={onClose} returnFocus={returnFocus} />
  }
  return (
    <ViewerImage
      attachment={attachment}
      previewUrl={attachment.id === initialId ? initialPreview : undefined}
      attachments={attachments}
      index={index}
      onSelect={setSelectedId}
      onClose={onClose}
      returnFocus={returnFocus}
    />
  )
}

function ViewerImage({
  attachment,
  previewUrl,
  attachments,
  index,
  onSelect,
  onClose,
  returnFocus,
}: {
  attachment: Attachment
  previewUrl?: string
  attachments: readonly Attachment[]
  index: number
  onSelect: (id: string) => void
  onClose: () => void
  returnFocus: HTMLElement | null
}) {
  const { t } = useI18n()
  const reference = useContext(AttachmentReferenceContext)
  const preview = useAttachmentImage(previewUrl ? undefined : attachment.id)
  const original = useAttachmentImage(attachment.id, 'original')
  const download = useAttachmentDownload(attachment)
  const viewport = useImageViewport(attachment.id)
  const [infoOpen, setInfoOpen] = useState(false)
  const infoButton = useRef<HTMLButtonElement>(null)
  const src = original.url || previewUrl || preview.url
  const previous = () => {
    if (index > 0) onSelect(attachments[index - 1].id)
  }
  const next = () => {
    if (index < attachments.length - 1) onSelect(attachments[index + 1].id)
  }
  return (
    <Dialog
      open
      title={attachment.name}
      className="attachment-viewer"
      restoreFocusTo={returnFocus}
      onClose={onClose}
      onKeyDown={(event) => {
        if (event.defaultPrevented) return
        if (event.key === 'Escape' && infoOpen) {
          event.preventDefault()
          setInfoOpen(false)
          infoButton.current?.focus()
        }
        if (event.key === 'ArrowLeft') {
          event.preventDefault()
          previous()
        }
        if (event.key === 'ArrowRight') {
          event.preventDefault()
          next()
        }
        if (event.key === '+' || event.key === '=') {
          event.preventDefault()
          viewport.zoomIn()
        }
        if (event.key === '-') {
          event.preventDefault()
          viewport.zoomOut()
        }
      }}
      headerActions={
        <div className="attachment-viewer-actions">
          {reference && (
            <IconButton
              type="button"
              variant="ghost"
              label={t('引用附件：{name}', { name: attachment.name })}
              tooltip={t('引用图片')}
              icon={<CornerDownLeft size={18} />}
              onClick={() => {
                reference(attachment)
                onClose()
              }}
            />
          )}
          <IconButton
            type="button"
            variant="ghost"
            label={t('下载附件：{name}', { name: attachment.name })}
            tooltip={t('下载图片')}
            icon={<Download size={18} />}
            loading={download.pending}
            onClick={() => void download.download()}
          />
          <IconButton
            ref={infoButton}
            type="button"
            variant="ghost"
            label={t('图片信息')}
            tooltip={t('图片信息')}
            aria-expanded={infoOpen}
            icon={<Info size={18} />}
            onClick={() => setInfoOpen(!infoOpen)}
          />
        </div>
      }
    >
      <div className="attachment-viewer-body">
        <div
          className="attachment-viewer-stage"
          ref={viewport.stage}
          data-pannable={viewport.canPan}
          onPointerDown={viewport.pointerDown}
          onPointerMove={viewport.pointerMove}
          onPointerUp={viewport.pointerEnd}
          onPointerCancel={viewport.pointerEnd}
          onLostPointerCapture={viewport.pointerEnd}
        >
          {src ? (
            <img
              className="attachment-viewer-image"
              src={src}
              alt={attachment.name}
              style={viewport.imageStyle}
              draggable={false}
              onLoad={(event) =>
                viewport.setImage({
                  width: event.currentTarget.naturalWidth,
                  height: event.currentTarget.naturalHeight,
                })
              }
              onError={() => {
                if (original.url) original.fail()
                else preview.fail()
              }}
            />
          ) : (
            <p className="attachment-viewer-empty" role="status">
              {preview.failed
                ? t('图片暂时无法打开，请重试')
                : t('正在加载图片…')}
            </p>
          )}
        </div>
        {(!original.url || download.failed) && (
          <div className="attachment-viewer-status" role="status">
            {download.failed ? (
              <>
                {t('下载失败，请重试')}
                <Button
                  type="button"
                  variant="ghost"
                  onClick={() => void download.download()}
                  loading={download.pending}
                >
                  {t('重试下载')}
                </Button>
              </>
            ) : original.failed ? (
              <>
                {t('原图暂时无法加载，仍可查看预览')}
                <Button
                  type="button"
                  variant="ghost"
                  onClick={() => {
                    original.retry()
                    if (preview.failed) preview.retry()
                  }}
                >
                  {t('重试原图')}
                </Button>
              </>
            ) : (
              t('正在加载原图…')
            )}
          </div>
        )}
        {infoOpen && (
          <aside className="attachment-viewer-info" aria-label={t('图片信息')}>
            <h3>{t('图片信息')}</h3>
            <dl>
              <dt>{t('文件名')}</dt>
              <dd>{attachment.name}</dd>
              <dt>{t('图片尺寸')}</dt>
              <dd>
                {viewport.image.width
                  ? `${viewport.image.width} × ${viewport.image.height}`
                  : '—'}
              </dd>
              <dt>{t('文件格式')}</dt>
              <dd>{attachment.mime_type}</dd>
              <dt>{t('文件大小')}</dt>
              <dd>{(attachment.size_bytes / 1024).toFixed(1)} KiB</dd>
            </dl>
          </aside>
        )}
      </div>
      <footer className="attachment-viewer-footer">
        {attachments.length > 1 && (
          <div className="attachment-viewer-gallery" aria-label={t('图片列表')}>
            <IconButton
              type="button"
              variant="ghost"
              label={t('上一张')}
              icon={<ChevronLeft size={18} />}
              aria-disabled={index === 0}
              onClick={previous}
            />
            <div className="attachment-viewer-thumbnails">
              {attachments.map((item) => (
                <GalleryThumbnail
                  key={item.id}
                  attachment={item}
                  selected={item.id === attachment.id}
                  onSelect={() => onSelect(item.id)}
                />
              ))}
            </div>
            <IconButton
              type="button"
              variant="ghost"
              label={t('下一张')}
              icon={<ChevronRight size={18} />}
              aria-disabled={index === attachments.length - 1}
              onClick={next}
            />
          </div>
        )}
        <div className="attachment-viewer-zoom" aria-label={t('图片缩放')}>
          <IconButton
            type="button"
            variant="ghost"
            label={t('缩小')}
            tooltip={t('缩小')}
            icon={<Minus size={18} />}
            disabled={!viewport.canZoomOut}
            onClick={viewport.zoomOut}
          />
          <output aria-label={t('缩放比例')}>
            {Math.round(viewport.scale * 100)}%
          </output>
          <IconButton
            type="button"
            variant="ghost"
            label={t('放大')}
            tooltip={t('放大')}
            icon={<Plus size={18} />}
            disabled={!viewport.canZoomIn}
            onClick={viewport.zoomIn}
          />
          <IconButton
            type="button"
            variant="ghost"
            label={t('适应窗口')}
            tooltip={t('适应窗口')}
            icon={<Maximize size={18} />}
            aria-pressed={viewport.zoom === 'fit'}
            onClick={() => viewport.changeZoom('fit')}
          />
          <Button
            type="button"
            variant="ghost"
            aria-label={t('原始尺寸')}
            aria-pressed={viewport.zoom === 1}
            onClick={() => viewport.changeZoom(1)}
          >
            1:1
          </Button>
        </div>
      </footer>
    </Dialog>
  )
}
